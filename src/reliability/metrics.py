"""Prometheus textfile export.

Every DQ run writes ``artifacts/metrics/dq.prom``; node_exporter's textfile
collector (or Airflow's StatsD sidecar) picks the file up, so checks appear in
Grafana next to latency and availability metrics - one dashboard for "is the
mart usable", not a separate DQ tab nobody opens.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .checks import CheckResult

STATUS_VALUE = {"PASS": 0, "WARN": 1, "FAIL": 2, "ERROR": 3}

HELP = {
    "dq_check_status": ("gauge", "0=PASS 1=WARN 2=FAIL 3=ERROR"),
    "dq_check_duration_seconds": ("gauge", "Wall-clock duration of the check query"),
    "dq_check_rows_examined": ("gauge", "Rows scanned by the check"),
    "dq_metric_value": ("gauge", "Raw metric value produced by the check"),
    "dq_rows_loaded": ("gauge", "Rows visible in the monitored entity"),
    "dq_freshness_hours": ("gauge", "Hours since the last successful load"),
    "dq_incident_open": ("gauge", "Open incidents by severity"),
    "dq_run_timestamp_seconds": ("gauge", "Unix time of the DQ run"),
    "dq_checks_total": ("counter", "Checks executed per status"),
}


def _escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _labels(**labels: object) -> str:
    """Пустой набор меток не даёт фигурных скобок - иначе Prometheus ругается."""
    inner = ",".join(f'{key}="{_escape(val)}"' for key, val in labels.items())
    return "{" + inner + "}" if inner else ""


def _header_lines() -> list[str]:
    lines: list[str] = []
    for name, (metric_type, description) in HELP.items():
        lines.append(f"# HELP {name} {description}")
        lines.append(f"# TYPE {name} {metric_type}")
    return lines


def _emit(name: str, labels: Mapping[str, object], value: float) -> str:
    return f"{name}{_labels(**labels)} {value}"


def _result_lines(result: CheckResult) -> list[str]:
    labels = {
        "check_id": result.check_id,
        "entity": result.entity,
        "severity": result.severity,
    }
    value = result.metric_value
    lines = [
        _emit("dq_check_status", labels, STATUS_VALUE.get(result.status, 3)),
        _emit("dq_check_duration_seconds", labels, result.duration_ms / 1000.0),
    ]
    if result.rows_examined is not None:
        lines.append(_emit("dq_check_rows_examined", labels, result.rows_examined))
    if value is not None:
        lines.append(_emit("dq_metric_value", labels, value))
    if result.unit == "hours" and value is not None:
        lines.append(_emit("dq_freshness_hours", {"entity": result.entity}, value))
    if result.unit == "rows" and value is not None:
        lines.append(_emit("dq_rows_loaded", {"entity": result.entity}, value))
    lines.append(_emit("dq_checks_total", {"status": result.status}, 1))
    return lines


def render(
    results: Iterable[CheckResult],
    run_ts: datetime | None = None,
    open_incidents: Mapping[str, int] | None = None,
) -> str:
    """Prometheus textfile body: HELP/TYPE header, per-check series, run stamp."""
    results = list(results)
    moment = run_ts or datetime.now(timezone.utc)
    stamp = int(moment.timestamp())
    lines = [
        *_header_lines(),
        *[line for result in results for line in _result_lines(result)],
        *[
            _emit("dq_incident_open", {"severity": severity}, count)
            for severity, count in sorted((open_incidents or {}).items())
        ],
        _emit("dq_run_timestamp_seconds", {}, stamp),
    ]
    return "\n".join(lines) + "\n"


def write(path: str | Path, content: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target
