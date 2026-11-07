"""Incident management on top of DQ results.

Lifecycle: OPEN -> ACK -> RESOLVED, with automatic deduplication.

Deduplication matters more than it looks: a broken ETL job fails the same check
on every 15-minute run. Without a dedup key (``check_id|entity``) the on-call
gets 96 tickets per day for one broken partition and stops reading them. Here a
repeating failure only bumps ``occurrences`` and ``last_seen_at`` on the
existing incident, and a passing run resolves it automatically with the run id
recorded in ``resolution_note`` for the postmortem.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .checks import ERROR, FAIL, PASS, CheckResult
from .db import Database

OPEN = "OPEN"
ACK = "ACK"
RESOLVED = "RESOLVED"

SEVERITY_SLA_MINUTES = {"P1": 15, "P2": 60, "P3": 240}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def dedup_key(check_id: str, entity: str) -> str:
    return f"{check_id}|{entity}"


def _next_incident_id(db: Database) -> str:
    current = db.scalar(
        "SELECT MAX(CAST(SUBSTR(incident_id, 4) AS INTEGER)) FROM ops_dq_incident",
        default=0,
    )
    return f"DQ-{(int(current or 0) + 1):05d}"


def open_incidents(db: Database, severity: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    clause = "status <> 'RESOLVED'"
    if severity:
        clause += " AND severity = :severity"
        params["severity"] = severity
    return db.query(
        f"""
        SELECT * FROM ops_dq_incident
        WHERE {clause}
        ORDER BY severity, opened_at
        """,
        params,
    )


def _find_open(db: Database, key: str) -> dict[str, Any] | None:
    return db.query_one(
        """
        SELECT * FROM ops_dq_incident
        WHERE dedup_key = :key AND status <> 'RESOLVED'
        ORDER BY opened_at DESC
        LIMIT 1
        """,
        {"key": key},
    )


def _failure_json(result: CheckResult) -> tuple[str, str]:
    samples_json = json.dumps(list(result.samples), ensure_ascii=False, default=str)
    bounds_json = json.dumps(dict(result.bounds), ensure_ascii=False)
    return samples_json, bounds_json


_BUMP_SQL = """
UPDATE ops_dq_incident
SET last_seen_at = :now,
    occurrences = occurrences + 1,
    latest_run_id = :run_id,
    metric_value = :metric_value,
    samples_json = :samples_json
WHERE incident_id = :incident_id
"""

_INSERT_SQL = """
INSERT INTO ops_dq_incident
    (incident_id, dedup_key, check_id, entity, severity, status,
     title, owner, opened_at, last_seen_at, occurrences,
     latest_run_id, metric_value, bounds_json, samples_json)
VALUES
    (:incident_id, :dedup_key, :check_id, :entity, :severity, :status,
     :title, :owner, :now, :now, 1,
     :run_id, :metric_value, :bounds_json, :samples_json)
"""

_RESOLVE_SQL = """
UPDATE ops_dq_incident
SET status = :status,
    resolved_at = :now,
    last_seen_at = :now,
    latest_run_id = :run_id,
    resolution_note = :note
WHERE incident_id = :incident_id
"""


def _bump_incident(
    db: Database, run_id: str, result: CheckResult, existing: Mapping[str, Any], samples_json: str
) -> dict[str, Any]:
    db.execute(
        _BUMP_SQL,
        {
            "now": _iso(_now()),
            "run_id": run_id,
            "metric_value": result.metric_value,
            "samples_json": samples_json,
            "incident_id": existing["incident_id"],
        },
    )
    return {
        "action": "REPEATED",
        "incident_id": existing["incident_id"],
        "check_id": result.check_id,
        "severity": existing["severity"],
        "occurrences": int(existing["occurrences"]) + 1,
    }


def _incident_params(
    incident_id: str,
    key: str,
    run_id: str,
    result: CheckResult,
    default_owner: str,
    bounds_json: str,
    samples_json: str,
) -> dict[str, Any]:
    return {
        "incident_id": incident_id,
        "dedup_key": key,
        "check_id": result.check_id,
        "entity": result.entity,
        "severity": result.severity,
        "status": OPEN,
        "title": result.title,
        "owner": result.owner or default_owner,
        "now": _iso(_now()),
        "run_id": run_id,
        "metric_value": result.metric_value,
        "bounds_json": bounds_json,
        "samples_json": samples_json,
    }


def _create_incident(
    db: Database,
    run_id: str,
    result: CheckResult,
    key: str,
    default_owner: str,
    bounds_json: str,
    samples_json: str,
) -> dict[str, Any]:
    incident_id = _next_incident_id(db)
    db.execute(
        _INSERT_SQL,
        _incident_params(
            incident_id, key, run_id, result, default_owner, bounds_json, samples_json
        ),
    )
    return {
        "action": "OPENED",
        "incident_id": incident_id,
        "check_id": result.check_id,
        "severity": result.severity,
        "occurrences": 1,
    }


def _register_failure(
    db: Database,
    run_id: str,
    result: CheckResult,
    key: str,
    existing: Mapping[str, Any] | None,
    default_owner: str,
) -> dict[str, Any]:
    samples_json, bounds_json = _failure_json(result)
    if existing:
        event = _bump_incident(db, run_id, result, existing, samples_json)
    else:
        event = _create_incident(
            db, run_id, result, key, default_owner, bounds_json, samples_json
        )
    return event


def _auto_resolve(
    db: Database, run_id: str, result: CheckResult, existing: Mapping[str, Any]
) -> dict[str, Any]:
    db.execute(
        _RESOLVE_SQL,
        {
            "status": RESOLVED,
            "now": _iso(_now()),
            "run_id": run_id,
            "note": f"auto-resolved: check passed in run {run_id}",
            "incident_id": existing["incident_id"],
        },
    )
    return {
        "action": "AUTO_RESOLVED",
        "incident_id": existing["incident_id"],
        "check_id": result.check_id,
        "severity": existing["severity"],
        "occurrences": int(existing["occurrences"]),
    }


def _sync_one(
    db: Database,
    run_id: str,
    result: CheckResult,
    key: str,
    existing: Mapping[str, Any] | None,
    default_owner: str,
) -> dict[str, Any] | None:
    """One event for one check result: opened, repeated, auto-resolved or none."""
    event: dict[str, Any] | None = None
    if result.status in (FAIL, ERROR):
        event = _register_failure(db, run_id, result, key, existing, default_owner)
    elif result.status == PASS and existing:
        event = _auto_resolve(db, run_id, result, existing)
    return event


def sync_incidents(
    db: Database,
    run_id: str,
    results: Iterable[CheckResult],
    default_owner: str = "",
) -> list[dict[str, Any]]:
    """Open/bump incidents for failures, auto-resolve them on PASS."""
    events: list[dict[str, Any]] = []
    for result in results:
        key = dedup_key(result.check_id, result.entity)
        existing = _find_open(db, key)
        event = _sync_one(db, run_id, result, key, existing, default_owner)
        if event is not None:
            events.append(event)
    return events


def ack(db: Database, incident_id: str, note: str = "") -> dict[str, Any]:
    incident = _require(db, incident_id)
    if incident["status"] == RESOLVED:
        raise ValueError(f"{incident_id} is already resolved")
    db.execute(
        """
        UPDATE ops_dq_incident
        SET status = :status, acked_at = :now
        WHERE incident_id = :incident_id
        """,
        {"status": ACK, "now": _iso(_now()), "incident_id": incident_id},
    )
    return {**incident, "status": ACK, "note": note}


def resolve(db: Database, incident_id: str, note: str = "") -> dict[str, Any]:
    incident = _require(db, incident_id)
    db.execute(
        """
        UPDATE ops_dq_incident
        SET status = :status,
            resolved_at = :now,
            last_seen_at = :now,
            resolution_note = :note
        WHERE incident_id = :incident_id
        """,
        {
            "status": RESOLVED,
            "now": _iso(_now()),
            "note": note or "resolved manually",
            "incident_id": incident_id,
        },
    )
    return {**incident, "status": RESOLVED, "resolution_note": note}


def _require(db: Database, incident_id: str) -> dict[str, Any]:
    incident = db.query_one(
        "SELECT * FROM ops_dq_incident WHERE incident_id = :id", {"id": incident_id}
    )
    if not incident:
        raise KeyError(f"unknown incident: {incident_id}")
    return incident


def age_minutes(incident: Mapping[str, Any], now: datetime | None = None) -> float:
    opened = _parse(incident.get("opened_at"))
    minutes = 0.0
    if opened:
        end = _parse(incident.get("resolved_at")) or (now or _now())
        minutes = round((end - opened).total_seconds() / 60.0, 1)
    return minutes


def _summarize(values: list[float]) -> dict[str, Any]:
    if values:
        summary = {
            "resolved": len(values),
            "avg_minutes": round(sum(values) / len(values), 1),
            "max_minutes": round(max(values), 1),
        }
    else:
        summary = {"resolved": 0, "avg_minutes": None, "max_minutes": None}
    return summary


def mttr(db: Database) -> dict[str, Any]:
    """Mean time to resolve, overall and per severity."""
    rows = db.query(
        """
        SELECT incident_id, severity, opened_at, resolved_at
        FROM ops_dq_incident
        WHERE status = 'RESOLVED' AND resolved_at IS NOT NULL
        """
    )
    durations = [age_minutes(row) for row in rows]
    per_severity: dict[str, list[float]] = {}
    for row, minutes in zip(rows, durations):
        per_severity.setdefault(row["severity"], []).append(minutes)
    return {
        "overall": _summarize(durations),
        "by_severity": {sev: _summarize(vals) for sev, vals in sorted(per_severity.items())},
    }


def sla_breaches(db: Database) -> list[dict[str, Any]]:
    """Open incidents that have exceeded the response SLA for their severity."""
    breaches = []
    now = _now()
    for incident in open_incidents(db):
        budget = SEVERITY_SLA_MINUTES.get(incident["severity"], 240)
        waited = age_minutes(incident, now)
        if waited > budget:
            breaches.append(
                {
                    "incident_id": incident["incident_id"],
                    "severity": incident["severity"],
                    "age_minutes": waited,
                    "budget_minutes": budget,
                }
            )
    return breaches


def notification_payload(
    events: Iterable[Mapping[str, Any]],
    run_id: str,
    notify: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Body for a chat/Jira webhook. The demo writes it to an artifact file."""
    notify = notify or {}
    return {
        "source": "reliability-dq",
        "channel": notify.get("channel", "#dq-alerts"),
        "jira_project": notify.get("jira_project"),
        "run_id": run_id,
        "generated_at": _iso(_now()),
        "events": [dict(event) for event in events],
        "summary": {
            "opened": sum(1 for e in events if e["action"] == "OPENED"),
            "repeated": sum(1 for e in events if e["action"] == "REPEATED"),
            "resolved": sum(1 for e in events if e["action"] == "AUTO_RESOLVED"),
        },
    }
