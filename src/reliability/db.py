"""Database access layer with a thin SQL dialect shim.

Why a dialect shim exists
-------------------------
The production target is Greenplum, which speaks PostgreSQL. Local runs and CI
use SQLite so the whole pipeline is reproducible without a cluster. Check SQL
therefore stays ANSI-ish, and the few genuinely dialect-specific expressions
are written as macros:

    {{ age_hours('load_dt') }}   ->  hours between now() and load_dt

The shim also converts ``:named`` placeholders to psycopg2's ``%(named)s``.
Check SQL must not use PostgreSQL ``::`` casts for that reason - use
``CAST(x AS type)`` instead.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

MACRO_RE = re.compile(r"\{\{\s*age_hours\(\s*'([A-Za-z_][A-Za-z0-9_.]*)'\s*\)\s*\}\}")

_MACROS: dict[str, dict[str, str]] = {
    "sqlite": {"age_hours": "(julianday('now') - julianday({col})) * 24.0"},
    "postgres": {"age_hours": "(EXTRACT(EPOCH FROM (now() - {col})) / 3600.0)"},
}

_POSTGRES_PREFIXES = ("postgres://", "postgresql://")


class DatabaseError(RuntimeError):
    """Raised when a connection cannot be established or a script fails."""


class Database:
    """Small connection wrapper: query(), execute(), script().

    ``query`` always returns a list of plain dicts, which keeps check specs and
    reports independent from the driver.
    """

    def __init__(self, dsn: str):
        self.dsn = dsn
        if dsn.startswith(_POSTGRES_PREFIXES):
            self.dialect = "postgres"
            try:
                import psycopg2  # type: ignore
                from psycopg2.extras import RealDictCursor  # type: ignore
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise DatabaseError(
                    "psycopg2 is required for PostgreSQL/Greenplum connections: "
                    "pip install 'reliability-dq[postgres]'"
                ) from exc
            self._cursor_factory = RealDictCursor
            self._conn = psycopg2.connect(dsn)
        else:
            self.dialect = "sqlite"
            self._cursor_factory = None
            path = dsn[len("sqlite:///"):] if dsn.startswith("sqlite:///") else dsn
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")

    # -- SQL preparation ---------------------------------------------------
    def compile_sql(self, sql: str) -> str:
        macros = _MACROS[self.dialect]

        def replace(match: re.Match[str]) -> str:
            return macros["age_hours"].format(col=match.group(1))

        sql = MACRO_RE.sub(replace, sql)
        if self.dialect == "postgres":
            sql = re.sub(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)", r"%(\1)s", sql)
        return sql

    # -- querying ----------------------------------------------------------
    def query(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        statement = self.compile_sql(sql)
        values = dict(params or {})
        if self.dialect == "postgres":  # pragma: no cover - needs a cluster
            cursor = self._conn.cursor(cursor_factory=self._cursor_factory)
            try:
                cursor.execute(statement, values)
                rows = [dict(row) for row in cursor.fetchall()]
            finally:
                cursor.close()
        else:
            rows = [dict(row) for row in self._conn.execute(statement, values).fetchall()]
        return rows

    def query_one(
        self, sql: str, params: Mapping[str, Any] | None = None
    ) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(
        self, sql: str, params: Mapping[str, Any] | None = None, default: Any = None
    ) -> Any:
        row = self.query_one(sql, params)
        return next(iter(row.values())) if row else default

    # -- writing -----------------------------------------------------------
    def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> None:
        statement = self.compile_sql(sql)
        values = dict(params or {})
        if self.dialect == "postgres":  # pragma: no cover - needs a cluster
            cursor = self._conn.cursor()
            try:
                cursor.execute(statement, values)
                self._conn.commit()
            finally:
                cursor.close()
            return
        self._conn.execute(statement, values)
        self._conn.commit()

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        statement = self.compile_sql(sql)
        if self.dialect == "postgres":  # pragma: no cover - needs a cluster
            cursor = self._conn.cursor()
            try:
                cursor.executemany(statement, list(rows))
                written = cursor.rowcount
            finally:
                cursor.close()
            self._conn.commit()
        else:
            written = self._conn.executemany(statement, rows).rowcount
            self._conn.commit()
        return written

    def script(self, path: str | Path) -> None:
        """Run a .sql file (DDL). SQLite uses executescript, PG splits on ';'."""
        text = Path(path).read_text(encoding="utf-8")
        if self.dialect == "postgres":  # pragma: no cover - needs a cluster
            cursor = self._conn.cursor()
            try:
                cursor.execute(text)
                self._conn.commit()
            finally:
                cursor.close()
            return
        self._conn.executescript(text)
        self._conn.commit()

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - best effort
            pass

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        target = self.dsn if self.dialect == "sqlite" else "postgresql"
        return f"<Database {self.dialect} {target}>"
