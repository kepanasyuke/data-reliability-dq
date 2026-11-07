"""Markdown report: the artifact a release manager or the on-call actually reads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .checks import ERROR, FAIL, PASS, WARN, CheckResult

STATUS_ICON = {PASS: "OK", WARN: "WARN", FAIL: "FAIL", ERROR: "ERROR"}


@dataclass
class ReportContext:
    env: str
    run_id: str
    run_ts: datetime
    results: Sequence[CheckResult]
    incidents: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    events: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    mttr: Mapping[str, Any] = field(default_factory=dict)
    sla_breaches: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    history: Sequence[Mapping[str, Any]] = field(default_factory=tuple)


def _fmt(value: float | None, unit: str) -> str:
    if value is None:
        text = "-"
    elif unit == "ratio":
        text = f"{value:.4f}"
    elif unit == "hours":
        text = f"{value:.2f} h"
    elif unit == "row_pct":
        text = f"{value:.2f}"
    else:
        text = f"{value:g}"
    return text


def _bound(bounds: Mapping[str, Mapping[str, float]]) -> str:
    return ", ".join(
        f"{level}: " + " ".join(f"{op} {val:g}" for op, val in spec.items())
        for level, spec in bounds.items()
    )


def _status_counts(results: Sequence[CheckResult]) -> dict[str, int]:
    counts = {status: 0 for status in (PASS, WARN, FAIL, ERROR)}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


def _header_lines(context: ReportContext, counts: Mapping[str, int]) -> list[str]:
    total_ms = sum(result.duration_ms for result in context.results)
    return [
        f"# Отчёт Data Quality — {context.env}",
        "",
        f"- Прогон: `{context.run_id}`",
        f"- Время (UTC): {context.run_ts.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Проверок: {len(context.results)} · PASS {counts[PASS]} · "
        f"WARN {counts[WARN]} · FAIL {counts[FAIL]} · ERROR {counts[ERROR]}",
        f"- Суммарное время проверок: {total_ms / 1000:.2f} c",
        "",
    ]


def _blocking_lines(results: Sequence[CheckResult]) -> list[str]:
    failing = [r for r in results if r.status in (FAIL, ERROR)]
    entries = [
        f"- **{r.severity} · {r.check_id}** — {r.title} (`{r.entity}`): {r.message}"
        for r in sorted(failing, key=lambda r: (r.severity, r.check_id))
    ]
    return ["## Блокирующие проверки", "", *entries, ""] if entries else []


def _table_row(result: CheckResult) -> str:
    return (
        f"| `{result.check_id}` | {result.entity} | {result.severity} | "
        f"{STATUS_ICON[result.status]} | {_fmt(result.metric_value, result.unit)} | "
        f"{_bound(result.bounds)} | "
        f"{result.rows_examined if result.rows_examined is not None else '-'} | "
        f"{result.duration_ms} |"
    )


def _results_lines(results: Sequence[CheckResult]) -> list[str]:
    return [
        "## Результаты проверок",
        "",
        "| Проверка | Сущность | Sev | Статус | Значение | Порог | Строк | ms |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        *[_table_row(result) for result in results],
        "",
    ]


def _samples_block(result: CheckResult) -> list[str]:
    keys = list(result.samples[0].keys())
    return [
        f"<details><summary>{result.check_id} — примеры строк</summary>",
        "",
        "| " + " | ".join(keys) + " |",
        "| " + " | ".join("---" for _ in keys) + " |",
        *["| " + " | ".join(str(row.get(key, "")) for key in keys) + " |" for row in result.samples],
        "",
        "</details>",
        "",
    ]


def _samples_lines(results: Sequence[CheckResult]) -> list[str]:
    blocks = [line for result in results for line in _samples_block(result)]
    return ["## Примеры нарушений", "", *blocks] if blocks else []


def _events_lines(events: Sequence[Mapping[str, Any]]) -> list[str]:
    rows = [
        f"| {event['action']} | {event['incident_id']} | {event['check_id']} | "
        f"{event['severity']} | {event['occurrences']} |"
        for event in events
    ]
    return [
        "## Действия по инцидентам",
        "",
        "| Действие | Инцидент | Проверка | Sev | Повторов |",
        "| --- | --- | --- | --- | --- |",
        *rows,
        "",
    ] if rows else []


def _incident_lines(incidents: Sequence[Mapping[str, Any]]) -> list[str]:
    rows = [
        f"| {incident['incident_id']} | {incident['severity']} | "
        f"{incident['status']} | `{incident['check_id']}` | "
        f"{incident['opened_at']} | {incident['occurrences']} |"
        for incident in incidents
    ]
    return [
        "## Открытые инциденты",
        "",
        "| Инцидент | Sev | Статус | Проверка | Открыт (UTC) | Повторов |",
        "| --- | --- | --- | --- | --- | --- |",
        *rows,
        "",
    ] if rows else []


def _mttr_lines(mttr: Mapping[str, Any]) -> list[str]:
    overall = (mttr or {}).get("overall", {})
    severity_lines = [
        f"  - {severity}: {stats['avg_minutes']} мин "
        f"(закрыто {stats['resolved']}, максимум {stats['max_minutes']} мин)"
        for severity, stats in ((mttr or {}).get("by_severity") or {}).items()
    ]
    return [
        "## MTTR",
        "",
        f"- Среднее время восстановления: **{overall['avg_minutes']} мин** "
        f"по {overall['resolved']} закрытым инцидентам",
        *severity_lines,
        "",
    ] if overall.get("resolved") else []


def _sla_lines(breaches: Sequence[Mapping[str, Any]]) -> list[str]:
    rows = [
        f"- {breach['incident_id']} ({breach['severity']}): "
        f"{breach['age_minutes']} мин против бюджета {breach['budget_minutes']} мин"
        for breach in breaches
    ]
    return ["## Нарушение SLA реакции", "", *rows, ""] if rows else []


def _history_lines(history: Sequence[Mapping[str, Any]]) -> list[str]:
    rows = [
        f"| `{run['run_id']}` | {run['run_ts']} | {run['checks']} | "
        f"{run['failed']} | {run['warned']} | {run['errored']} |"
        for run in history
    ]
    return [
        "## История прогонов",
        "",
        "| Run | Время (UTC) | Проверок | FAIL | WARN | ERROR |",
        "| --- | --- | --- | --- | --- | --- |",
        *rows,
        "",
    ] if rows else []


def _footer_lines(context: ReportContext) -> list[str]:
    return [
        "---",
        f"_Сгенерировано {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC "
        f"· reliability-dq · {len(context.results)} проверок_",
    ]


def render(context: ReportContext) -> str:
    """Full markdown report: one section per block, blocks are independent."""
    results = context.results
    lines = [
        *_header_lines(context, _status_counts(results)),
        *_blocking_lines(results),
        *_results_lines(results),
        *_samples_lines([r for r in results if r.samples]),
        *_events_lines(context.events),
        *_incident_lines(context.incidents),
        *_mttr_lines(context.mttr),
        *_sla_lines(context.sla_breaches),
        *_history_lines(context.history),
        *_footer_lines(context),
    ]
    return "\n".join(lines) + "\n"


def write(path, content: str):
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target
