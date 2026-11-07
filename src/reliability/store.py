"""Persistence for DQ runs and incidents (the ``ops`` schema equivalent).

In production these tables live in a dedicated ``ops`` schema next to the marts
(see ``sql/greenplum/01_mart_ddl.sql``). The demo keeps them in the same SQLite
file, which is enough to reproduce the full incident lifecycle locally.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Sequence

from .checks import CheckResult
from .db import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS ops_dq_result (
    run_id        TEXT    NOT NULL,
    run_ts        TEXT    NOT NULL,
    check_id      TEXT    NOT NULL,
    entity        TEXT    NOT NULL,
    severity      TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    metric_value  REAL,
    unit          TEXT,
    message       TEXT,
    rows_examined INTEGER,
    duration_ms   INTEGER,
    samples_json  TEXT,
    PRIMARY KEY (run_id, check_id)
);

CREATE TABLE IF NOT EXISTS ops_dq_incident (
    incident_id     TEXT PRIMARY KEY,
    dedup_key       TEXT NOT NULL,
    check_id        TEXT NOT NULL,
    entity          TEXT NOT NULL,
    severity        TEXT NOT NULL,
    status          TEXT NOT NULL,
    title           TEXT NOT NULL,
    owner           TEXT,
    opened_at       TEXT NOT NULL,
    acked_at        TEXT,
    resolved_at     TEXT,
    last_seen_at    TEXT NOT NULL,
    occurrences     INTEGER NOT NULL DEFAULT 1,
    latest_run_id   TEXT,
    metric_value    REAL,
    bounds_json     TEXT,
    samples_json    TEXT,
    resolution_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_incident_dedup
    ON ops_dq_incident (dedup_key, status);
CREATE INDEX IF NOT EXISTS idx_result_run
    ON ops_dq_result (run_id, status);
"""


def ensure_schema(db: Database) -> None:
    for statement in SCHEMA.strip().split(";"):
        chunk = statement.strip()
        if chunk:
            db.execute(chunk)


def _result_row(run_id: str, run_ts: str, result: CheckResult) -> tuple:
    return (
        run_id,
        run_ts,
        result.check_id,
        result.entity,
        result.severity,
        result.status,
        result.metric_value,
        result.unit,
        result.message,
        result.rows_examined,
        result.duration_ms,
        json.dumps(list(result.samples), ensure_ascii=False, default=str),
    )


def save_results(
    db: Database, run_id: str, run_ts: str, results: Iterable[CheckResult]
) -> int:
    payload = [_result_row(run_id, run_ts, result) for result in results]
    written = 0
    if payload:
        written = db.executemany(
            """
        INSERT INTO ops_dq_result
            (run_id, run_ts, check_id, entity, severity, status, metric_value,
             unit, message, rows_examined, duration_ms, samples_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
    return written


def latest_run_id(db: Database) -> str | None:
    return db.scalar("SELECT run_id FROM ops_dq_result ORDER BY run_ts DESC LIMIT 1")


def results_for_run(db: Database, run_id: str) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM ops_dq_result WHERE run_id = :run_id ORDER BY severity, check_id",
        {"run_id": run_id},
    )
    return rows


def runs_history(db: Database, limit: int = 10) -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT run_id,
               run_ts,
               COUNT(*)                                        AS checks,
               SUM(CASE WHEN status = 'FAIL' THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN status = 'WARN' THEN 1 ELSE 0 END) AS warned,
               SUM(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) AS errored
        FROM ops_dq_result
        GROUP BY run_id, run_ts
        ORDER BY run_ts DESC
        LIMIT :limit
        """,
        {"limit": limit},
    )


def trend_for_check(db: Database, check_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT run_ts, status, metric_value
        FROM ops_dq_result
        WHERE check_id = :check_id
        ORDER BY run_ts DESC
        LIMIT :limit
        """,
        {"check_id": check_id, "limit": limit},
    )


def open_incident_count(db: Database) -> int:
    return int(db.scalar(
        "SELECT COUNT(*) FROM ops_dq_incident WHERE status <> 'RESOLVED'", default=0
    ))


def incidents_by_status(db: Database) -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT status, severity, COUNT(*) AS total
        FROM ops_dq_incident
        GROUP BY status, severity
        ORDER BY severity, status
        """
    )
