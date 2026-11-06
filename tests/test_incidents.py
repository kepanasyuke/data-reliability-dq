"""Инциденты: дедупликация, автозакрытие, MTTR и SLA реакции."""

from __future__ import annotations

from reliability import incidents, store
from reliability.checks import FAIL, PASS, CheckResult
from reliability.db import Database


def _result(status: str, check_id: str = "turnover.orphan_client") -> CheckResult:
    return CheckResult(
        check_id=check_id,
        title="Сироты по справочнику клиентов",
        entity="fct_account_turnover_daily",
        severity="P1",
        owner="owner@example.org",
        status=status,
        metric_value=6.0,
        unit="count",
        bounds={"fail": {"max": 0}},
        message="count: 6 (threshold <= 0)",
        samples=[{"account_id": "AC00000001", "client_id": "CL999999"}],
    )


def test_repeated_failure_reuses_incident(tmp_path) -> None:
    with Database(str(tmp_path / "ops.sqlite3")) as db:
        store.ensure_schema(db)
        first = incidents.sync_incidents(db, "RUN-1", [_result(FAIL)])
        second = incidents.sync_incidents(db, "RUN-2", [_result(FAIL)])

        assert first[0]["action"] == "OPENED"
        assert second[0]["action"] == "REPEATED"

        opened = incidents.open_incidents(db)
        assert len(opened) == 1
        assert opened[0]["occurrences"] == 2
        assert opened[0]["latest_run_id"] == "RUN-2"
        assert opened[0]["incident_id"] == "DQ-00001"


def test_pass_auto_resolves_and_mttr_is_measured(tmp_path) -> None:
    with Database(str(tmp_path / "ops.sqlite3")) as db:
        store.ensure_schema(db)
        incidents.sync_incidents(db, "RUN-1", [_result(FAIL)])
        events = incidents.sync_incidents(db, "RUN-2", [_result(PASS)])

        assert events[0]["action"] == "AUTO_RESOLVED"
        assert incidents.open_incidents(db) == []

        stats = incidents.mttr(db)
        assert stats["overall"]["resolved"] == 1
        assert stats["overall"]["avg_minutes"] is not None
        assert "P1" in stats["by_severity"]


def test_warn_does_not_close_incident(tmp_path) -> None:
    with Database(str(tmp_path / "ops.sqlite3")) as db:
        store.ensure_schema(db)
        incidents.sync_incidents(db, "RUN-1", [_result(FAIL)])
        incidents.sync_incidents(db, "RUN-2", [_result("WARN")])
        assert len(incidents.open_incidents(db)) == 1


def test_manual_ack_and_resolve(tmp_path) -> None:
    with Database(str(tmp_path / "ops.sqlite3")) as db:
        store.ensure_schema(db)
        events = incidents.sync_incidents(db, "RUN-1", [_result(FAIL)])
        incident_id = events[0]["incident_id"]

        incidents.ack(db, incident_id, note="взял в работу")
        assert incidents.open_incidents(db)[0]["status"] == incidents.ACK

        incidents.resolve(db, incident_id, note="перезалили партицию")
        row = db.query_one(
            "SELECT * FROM ops_dq_incident WHERE incident_id = :id", {"id": incident_id}
        )
        assert row["status"] == incidents.RESOLVED
        assert row["resolution_note"] == "перезалили партицию"


def test_sla_breach_for_fresh_incident_is_empty(tmp_path) -> None:
    with Database(str(tmp_path / "ops.sqlite3")) as db:
        store.ensure_schema(db)
        incidents.sync_incidents(db, "RUN-1", [_result(FAIL)])
        assert incidents.sla_breaches(db) == []


def test_notification_payload_summarises_events(tmp_path) -> None:
    with Database(str(tmp_path / "ops.sqlite3")) as db:
        store.ensure_schema(db)
        events = incidents.sync_incidents(db, "RUN-1", [_result(FAIL)])
        payload = incidents.notification_payload(events, "RUN-1", {"channel": "#dq"})

    assert payload["summary"] == {"opened": 1, "repeated": 0, "resolved": 0}
    assert payload["events"][0]["severity"] == "P1"
