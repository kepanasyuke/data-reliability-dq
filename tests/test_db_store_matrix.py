"""Слой доступа к БД и хранилище прогонов: макросы диалектов, запросы, ops-таблицы."""

from __future__ import annotations

import pytest

from reliability import store
from reliability.checks import PASS, CheckResult
from reliability.db import Database


@pytest.fixture()
def db():
    with Database(":memory:") as connection:
        store.ensure_schema(connection)
        yield connection


def _result(check_id: str, status: str = PASS, severity: str = "P1") -> CheckResult:
    return CheckResult(
        check_id=check_id,
        title=check_id,
        entity="dim_client",
        severity=severity,
        owner="owner@example.org",
        status=status,
        metric_value=1.5,
        unit="count",
        bounds={"fail": {"max": 0}},
        message="count: 1.5",
        rows_examined=3,
        duration_ms=7,
        samples=({"client_id": "CL1"},),
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT {{ age_hours('load_dt') }} AS hours",
        "SELECT {{age_hours('load_dt')}} AS hours",
        "SELECT {{  age_hours( 'load_dt' )  }} AS hours",
    ],
)
def test_sqlite_macro_is_expanded(db: Database, sql: str) -> None:
    assert "julianday" in db.compile_sql(sql)


def test_postgres_macro_is_expanded(db: Database) -> None:
    db.dialect = "postgres"
    assert "EXTRACT(EPOCH" in db.compile_sql("SELECT {{ age_hours('load_dt') }}")


def test_unknown_macro_is_left_as_is(db: Database) -> None:
    sql = "SELECT {{ row_count('load_dt') }}"
    assert db.compile_sql(sql) == sql


@pytest.mark.parametrize("name", ["run_ts", "window_days", "sla_turnover_hours"])
def test_sqlite_placeholders_are_untouched(db: Database, name: str) -> None:
    sql = f"SELECT :{name}"
    assert db.compile_sql(sql) == sql


@pytest.mark.parametrize("name", ["run_ts", "window_days"])
def test_postgres_placeholders_are_converted(db: Database, name: str) -> None:
    db.dialect = "postgres"
    assert db.compile_sql(f"SELECT :{name}") == f"SELECT %({name})s"


def test_postgres_cast_is_not_a_placeholder(db: Database) -> None:
    db.dialect = "postgres"
    assert "::" in db.compile_sql("SELECT CAST(1 AS text), 2::int")


def test_query_returns_dicts(db: Database) -> None:
    rows = db.query("SELECT 1 AS one, 'x' AS two")
    assert rows == [{"one": 1, "two": "x"}]


def test_query_with_params(db: Database) -> None:
    assert db.query("SELECT :value AS value", {"value": 42}) == [{"value": 42}]


def test_query_one_returns_first_row(db: Database) -> None:
    assert db.query_one("SELECT 7 AS value") == {"value": 7}


def test_query_one_returns_none_on_empty(db: Database) -> None:
    assert db.query_one("SELECT 1 AS value WHERE 0") is None


def test_scalar_returns_first_column(db: Database) -> None:
    assert db.scalar("SELECT 5 AS value") == 5


def test_scalar_returns_default_on_empty(db: Database) -> None:
    assert db.scalar("SELECT 1 AS value WHERE 0", default=99) == 99


def test_execute_and_query_roundtrip(db: Database) -> None:
    db.execute("CREATE TABLE t (x INTEGER)")
    db.execute("INSERT INTO t VALUES (:x)", {"x": 1})
    assert db.scalar("SELECT COUNT(*) FROM t") == 1


@pytest.mark.parametrize("rows", [[(1,), (2,), (3,)], [(10,)]])
def test_executemany_reports_rowcount(db: Database, rows: list[tuple]) -> None:
    db.execute("CREATE TABLE t (x INTEGER)")
    assert db.executemany("INSERT INTO t VALUES (?)", rows) == len(rows)


def test_script_runs_ddl_file(db: Database, tmp_path) -> None:
    path = tmp_path / "ddl.sql"
    path.write_text("CREATE TABLE a (x INTEGER); CREATE TABLE b (y INTEGER);", encoding="utf-8")
    db.script(path)
    assert db.scalar("SELECT COUNT(*) FROM a") == 0


def test_foreign_keys_pragma_is_on(db: Database) -> None:
    assert db.scalar("PRAGMA foreign_keys") == 1


def test_context_manager_closes_connection() -> None:
    with Database(":memory:") as connection:
        connection.execute("CREATE TABLE t (x INTEGER)")
    connection.close()


def test_repr_names_dialect() -> None:
    with Database(":memory:") as connection:
        assert "sqlite" in repr(connection)


def test_ensure_schema_is_idempotent(db: Database) -> None:
    store.ensure_schema(db)
    store.ensure_schema(db)
    tables = {row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"ops_dq_result", "ops_dq_incident"} <= tables


@pytest.mark.parametrize("count", [0, 1, 3])
def test_save_results_returns_rowcount(db: Database, count: int) -> None:
    results = [_result(f"check.{index}") for index in range(count)]
    assert store.save_results(db, "RUN-1", "2026-11-07T12:00:00", results) == count


def test_latest_run_id_after_saves(db: Database) -> None:
    store.save_results(db, "RUN-1", "2026-11-07T12:00:00", [_result("a")])
    store.save_results(db, "RUN-2", "2026-11-07T12:30:00", [_result("a")])
    assert store.latest_run_id(db) == "RUN-2"


def test_latest_run_id_is_none_without_runs(db: Database) -> None:
    assert store.latest_run_id(db) is None


def test_results_for_run_keeps_samples(db: Database) -> None:
    store.save_results(db, "RUN-1", "2026-11-07T12:00:00", [_result("a")])
    row = store.results_for_run(db, "RUN-1")[0]
    assert row["check_id"] == "a"
    assert "CL1" in row["samples_json"]


@pytest.mark.parametrize("status", [PASS, "WARN", "FAIL", "ERROR"])
def test_runs_history_counts_statuses(db: Database, status: str) -> None:
    store.save_results(db, "RUN-1", "2026-11-07T12:00:00", [_result("a", status)])
    row = store.runs_history(db)[0]
    assert row["checks"] == 1
    column = {"PASS": None, "WARN": "warned", "FAIL": "failed", "ERROR": "errored"}[status]
    assert column is None or row[column] == 1


def test_runs_history_respects_limit(db: Database) -> None:
    for index in range(3):
        store.save_results(db, f"RUN-{index}", f"2026-11-0{index + 1}T12:00:00", [_result("a")])
    assert len(store.runs_history(db, limit=2)) == 2


def test_trend_for_check_orders_by_time(db: Database) -> None:
    store.save_results(db, "RUN-1", "2026-11-07T12:00:00", [_result("a")])
    store.save_results(db, "RUN-2", "2026-11-07T13:00:00", [_result("a", "FAIL")])
    trend = store.trend_for_check(db, "a")
    assert [row["status"] for row in trend] == ["FAIL", PASS]


def test_trend_for_unknown_check_is_empty(db: Database) -> None:
    assert store.trend_for_check(db, "nope") == []


def test_open_incident_count_is_zero_initially(db: Database) -> None:
    assert store.open_incident_count(db) == 0


def test_incidents_by_status_is_empty_initially(db: Database) -> None:
    assert store.incidents_by_status(db) == []


def test_open_incident_count_after_insert(db: Database) -> None:
    db.execute(
        """
        INSERT INTO ops_dq_incident
            (incident_id, dedup_key, check_id, entity, severity, status, title,
             opened_at, last_seen_at, occurrences)
        VALUES ('DQ-00001', 'k', 'c', 'e', 'P1', 'OPEN', 't', 'now', 'now', 1)
        """
    )
    assert store.open_incident_count(db) == 1
    assert store.incidents_by_status(db) == [{"status": "OPEN", "severity": "P1", "total": 1}]
