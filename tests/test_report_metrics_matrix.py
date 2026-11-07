"""Отчёт и Prometheus-метрики: форматирование значений, секции, экранирование меток."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from reliability.checks import ERROR, FAIL, PASS, WARN, CheckResult
from reliability.metrics import (
    STATUS_VALUE,
    _escape,
    _labels,
    render as render_metrics,
)
from reliability.report import ReportContext, _bound, _fmt, render as render_report

MOMENT = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def _result(**overrides) -> CheckResult:
    payload = {
        "check_id": "demo.check",
        "title": "Демо-проверка",
        "entity": "dim_client",
        "severity": "P1",
        "owner": "owner@example.org",
        "status": PASS,
        "metric_value": 1.0,
        "unit": "count",
        "bounds": {"fail": {"max": 0}},
        "message": "count: 1 (threshold <= 0)",
        "rows_examined": 10,
        "duration_ms": 12,
        "samples": (),
    }
    payload.update(overrides)
    return CheckResult(**payload)


def _context(results, **overrides) -> ReportContext:
    payload = {
        "env": "test",
        "run_id": "RUN-1",
        "run_ts": MOMENT,
        "results": results,
    }
    payload.update(overrides)
    return ReportContext(**payload)


@pytest.mark.parametrize(
    "value,unit,expected",
    [
        (None, "count", "-"),
        (0.0, "count", "0"),
        (7.0, "count", "7"),
        (1.23456, "ratio", "1.2346"),
        (0.4023, "ratio", "0.4023"),
        (26.0, "hours", "26.00 h"),
        (30.5, "hours", "30.50 h"),
        (12.3456, "row_pct", "12.35"),
        (3.0, "rows", "3"),
        (-1.0, "count", "-1"),
    ],
)
def test_fmt_matrix(value: float | None, unit: str, expected: str) -> None:
    assert _fmt(value, unit) == expected


@pytest.mark.parametrize(
    "bounds,expected",
    [
        ({"fail": {"max": 0}}, "fail: max 0"),
        ({"fail": {"max": 0.05}}, "fail: max 0.05"),
        ({"fail": {"max": 0}, "warn": {"max": -1}}, "fail: max 0, warn: max -1"),
        ({"fail": {"min": 0.7}}, "fail: min 0.7"),
        ({"fail": {"eq": 0, "min": -1}}, "fail: eq 0 min -1"),
        ({}, ""),
    ],
)
def test_bound_text_matrix(bounds: dict, expected: str) -> None:
    assert _bound(bounds) == expected


@pytest.mark.parametrize("status", [PASS, WARN, FAIL, ERROR])
def test_report_counts_every_status(status: str) -> None:
    text = render_report(_context([_result(status=status)]))
    assert f"{status} " in text
    assert "Проверок: 1" in text


@pytest.mark.parametrize("status", [FAIL, ERROR])
def test_report_lists_blocking_checks(status: str) -> None:
    text = render_report(_context([_result(status=status)]))
    assert "## Блокирующие проверки" in text
    assert "demo.check" in text


@pytest.mark.parametrize("status", [PASS, WARN])
def test_report_has_no_blocking_section_without_failures(status: str) -> None:
    text = render_report(_context([_result(status=status)]))
    assert "## Блокирующие проверки" not in text


@pytest.mark.parametrize("unit", ["count", "ratio", "hours", "row_pct", "rows"])
def test_report_contains_unit_value(unit: str) -> None:
    text = render_report(_context([_result(unit=unit, metric_value=2.0)]))
    assert _fmt(2.0, unit) in text


def test_report_has_incident_sections_when_present() -> None:
    text = render_report(
        _context(
            [_result(status=FAIL)],
            events=(
                {
                    "action": "OPENED",
                    "incident_id": "DQ-00001",
                    "check_id": "demo.check",
                    "severity": "P1",
                    "occurrences": 1,
                },
            ),
            incidents=(
                {
                    "incident_id": "DQ-00001",
                    "severity": "P1",
                    "status": "OPEN",
                    "check_id": "demo.check",
                    "opened_at": "2026-11-07T12:00:00+00:00",
                    "occurrences": 2,
                },
            ),
            mttr={"overall": {"resolved": 1, "avg_minutes": 1.8, "max_minutes": 3.0},
                  "by_severity": {"P1": {"resolved": 1, "avg_minutes": 1.8, "max_minutes": 3.0}}},
            sla_breaches=(
                {"incident_id": "DQ-00001", "severity": "P1",
                 "age_minutes": 30, "budget_minutes": 15},
            ),
            history=({"run_id": "RUN-1", "run_ts": "2026-11-07T12:00:00",
                      "checks": 12, "failed": 1, "warned": 0, "errored": 0},),
        )
    )
    for section in (
        "## Действия по инцидентам",
        "## Открытые инциденты",
        "## MTTR",
        "## Нарушение SLA реакции",
        "## История прогонов",
    ):
        assert section in text


def test_report_skips_empty_sections() -> None:
    text = render_report(_context([_result()]))
    for section in (
        "## Действия по инцидентам",
        "## Открытые инциденты",
        "## MTTR",
        "## Нарушение SLA реакции",
        "## История прогонов",
        "## Примеры нарушений",
    ):
        assert section not in text


def test_report_includes_samples_block() -> None:
    text = render_report(_context([_result(status=FAIL, samples=({"client_id": "CL1"},))]))
    assert "## Примеры нарушений" in text
    assert "client_id" in text


def test_report_ends_with_newline() -> None:
    assert render_report(_context([_result()])).endswith("\n")


@pytest.mark.parametrize("value,expected", [
    ('a"b', 'a\\"b'),
    ("a\\b", "a\\\\b"),
    ("a\nb", "a b"),
    ("plain", "plain"),
    (7, "7"),
])
def test_metric_label_escaping(value, expected: str) -> None:
    assert _escape(value) == expected


@pytest.mark.parametrize("labels", [{}, {"severity": "P1"}, {"a": 1, "b": "x"}])
def test_labels_render_braces_only_when_set(labels: dict) -> None:
    text = _labels(**labels)
    assert bool(text.startswith("{")) is bool(labels)
    assert text.count("{") == text.count("}")


@pytest.mark.parametrize("status", [PASS, WARN, FAIL, ERROR])
def test_metrics_status_value_mapping(status: str) -> None:
    text = render_metrics([_result(status=status)])
    assert f'dq_check_status{{check_id="demo.check"' in text
    assert f"}} {STATUS_VALUE[status]}" in text


def test_metrics_has_help_and_type_for_every_series() -> None:
    text = render_metrics([_result()])
    assert text.count("# HELP") == text.count("# TYPE")


def test_metrics_emits_duration_seconds() -> None:
    assert "dq_check_duration_seconds{" in render_metrics([_result(duration_ms=1500)])


def test_metrics_emits_rows_examined() -> None:
    assert "dq_check_rows_examined{" in render_metrics([_result(rows_examined=10)])


def test_metrics_skips_rows_examined_when_null() -> None:
    assert "dq_check_rows_examined{" not in render_metrics([_result(rows_examined=None)])


def test_metrics_skips_metric_value_when_null() -> None:
    assert "dq_metric_value{" not in render_metrics([_result(metric_value=None)])


def test_metrics_emits_freshness_hours() -> None:
    text = render_metrics([_result(unit="hours", metric_value=26.0)])
    assert 'dq_freshness_hours{entity="dim_client"} 26.0' in text


def test_metrics_emits_rows_loaded() -> None:
    text = render_metrics([_result(unit="rows", metric_value=959.0)])
    assert 'dq_rows_loaded{entity="dim_client"} 959.0' in text


def test_metrics_no_freshness_for_count_unit() -> None:
    assert "dq_freshness_hours{" not in render_metrics([_result(unit="count")])


@pytest.mark.parametrize("count", [0, 1, 5])
def test_metrics_incident_open_line(count: int) -> None:
    text = render_metrics([_result()], open_incidents={"P1": count})
    assert f'dq_incident_open{{severity="P1"}} {count}' in text


def test_metrics_incident_severities_are_sorted() -> None:
    text = render_metrics([_result()], open_incidents={"P2": 2, "P1": 1, "P3": 3})
    positions = [text.index('severity="P1"'), text.index('severity="P2"'), text.index('severity="P3"')]
    assert positions == sorted(positions)


def test_metrics_run_timestamp_uses_run_ts() -> None:
    text = render_metrics([_result()], run_ts=MOMENT)
    assert f"dq_run_timestamp_seconds {int(MOMENT.timestamp())}" in text


def test_metrics_empty_results_still_valid() -> None:
    text = render_metrics([])
    assert "dq_run_timestamp_seconds" in text
    assert "dq_check_status{" not in text


def test_metrics_ends_with_newline() -> None:
    assert render_metrics([_result()]).endswith("\n")
