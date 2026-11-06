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
        return "-"
    if unit == "ratio":
        return f"{value:.4f}"
    if unit == "hours":
        return f"{value:.2f} h"
    if unit == "row_pct":
        return f"{value:.2f}"
    return f"{value:g}"


def _bound(bounds: Mapping[str, Mapping[str, float]]) -> str:
    return ", ".join(
        f"{level}: " + " ".join(f"{op} {val:g}" for op, val in spec.items())
        for level, spec in bounds.items()
    )


def render(context: ReportContext) -> str:
    counts = {status: 0 for status in (PASS, WARN, FAIL, ERROR)}
    for result in context.results:
        counts[result.status] = counts.get(result.status, 0) + 1

    lines: list[str] = []
    lines.append(f"# Отчёт Data Quality — {context.env}")
    lines.append("")
    lines.append(f"- Прогон: `{context.run_id}`")
    lines.append(f"- Время (UTC): {context.run_ts.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(
        f"- Проверок: {len(context.results)} · PASS {counts[PASS]} · "
        f"WARN {counts[WARN]} · FAIL {counts[FAIL]} · ERROR {counts[ERROR]}"
    )
    total_ms = sum(result.duration_ms for result in context.results)
    lines.append(f"- Суммарное время проверок: {total_ms / 1000:.2f} c")
    lines.append("")

    failing = [r for r in context.results if r.status in (FAIL, ERROR)]
    if failing:
        lines.append("## Блокирующие проверки")
        lines.append("")
        for result in sorted(failing, key=lambda r: (r.severity, r.check_id)):
            lines.append(
                f"- **{result.severity} · {result.check_id}** — {result.title} "
                f"(`{result.entity}`): {result.message}"
            )
        lines.append("")

    lines.append("## Результаты проверок")
    lines.append("")
    lines.append("| Проверка | Сущность | Sev | Статус | Значение | Порог | Строк | ms |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for result in context.results:
        lines.append(
            f"| `{result.check_id}` | {result.entity} | {result.severity} | "
            f"{STATUS_ICON[result.status]} | {_fmt(result.metric_value, result.unit)} | "
            f"{_bound(result.bounds)} | "
            f"{result.rows_examined if result.rows_examined is not None else '-'} | "
            f"{result.duration_ms} |"
        )
    lines.append("")

    samples = [r for r in context.results if r.samples]
    if samples:
        lines.append("## Примеры нарушений")
        lines.append("")
        for result in samples:
            lines.append(f"<details><summary>{result.check_id} — примеры строк</summary>")
            lines.append("")
            keys = list(result.samples[0].keys())
            lines.append("| " + " | ".join(keys) + " |")
            lines.append("| " + " | ".join("---" for _ in keys) + " |")
            for row in result.samples:
                lines.append(
                    "| " + " | ".join(str(row.get(key, "")) for key in keys) + " |"
                )
            lines.append("")
            lines.append("</details>")
            lines.append("")

    if context.events:
        lines.append("## Действия по инцидентам")
        lines.append("")
        lines.append("| Действие | Инцидент | Проверка | Sev | Повторов |")
        lines.append("| --- | --- | --- | --- | --- |")
        for event in context.events:
            lines.append(
                f"| {event['action']} | {event['incident_id']} | {event['check_id']} | "
                f"{event['severity']} | {event['occurrences']} |"
            )
        lines.append("")

    if context.incidents:
        lines.append("## Открытые инциденты")
        lines.append("")
        lines.append("| Инцидент | Sev | Статус | Проверка | Открыт (UTC) | Повторов |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for incident in context.incidents:
            lines.append(
                f"| {incident['incident_id']} | {incident['severity']} | "
                f"{incident['status']} | `{incident['check_id']}` | "
                f"{incident['opened_at']} | {incident['occurrences']} |"
            )
        lines.append("")

    overall = (context.mttr or {}).get("overall", {})
    if overall.get("resolved"):
        lines.append("## MTTR")
        lines.append("")
        lines.append(
            f"- Среднее время восстановления: **{overall['avg_minutes']} мин** "
            f"по {overall['resolved']} закрытым инцидентам"
        )
        for severity, stats in (context.mttr.get("by_severity") or {}).items():
            lines.append(
                f"  - {severity}: {stats['avg_minutes']} мин "
                f"(закрыто {stats['resolved']}, максимум {stats['max_minutes']} мин)"
            )
        lines.append("")

    if context.sla_breaches:
        lines.append("## Нарушение SLA реакции")
        lines.append("")
        for breach in context.sla_breaches:
            lines.append(
                f"- {breach['incident_id']} ({breach['severity']}): "
                f"{breach['age_minutes']} мин против бюджета {breach['budget_minutes']} мин"
            )
        lines.append("")

    if context.history:
        lines.append("## История прогонов")
        lines.append("")
        lines.append("| Run | Время (UTC) | Проверок | FAIL | WARN | ERROR |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for run in context.history:
            lines.append(
                f"| `{run['run_id']}` | {run['run_ts']} | {run['checks']} | "
                f"{run['failed']} | {run['warned']} | {run['errored']} |"
            )
        lines.append("")

    lines.append("---")
    lines.append(
        f"_Сгенерировано {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC "
        f"· reliability-dq · {len(context.results)} проверок_"
    )
    return "\n".join(lines) + "\n"


def write(path, content: str):
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target
