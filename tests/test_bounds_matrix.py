"""Матрица порогов: операторы ``max``/``min``/``eq``, warn-зоны, exit-коды.

Проверки — это числа, поэтому вся арифметика порогов проверяется таблицей:
одна строка таблицы = один тест, ожидание выписано вручную.
"""

from __future__ import annotations

import pytest

from reliability.checks import (
    ERROR,
    FAIL,
    PASS,
    WARN,
    CheckResult,
    CheckSpec,
    evaluate,
    exit_code,
    worst_status,
)
from reliability.checks import _violates

OPERATORS = ("max", "min", "eq")
THRESHOLDS = (-1.0, 0.0, 5.0)
VALUES = (-2.0, -1.0, 0.0, 1.0, 5.0, 10.0)


def _reference(operator: str, threshold: float, value: float) -> bool:
    if operator == "max":
        return value > threshold
    if operator == "min":
        return value < threshold
    return value != threshold


def _spec(bounds: dict) -> CheckSpec:
    return CheckSpec(
        check_id="matrix.check",
        title="matrix",
        entity="dim_client",
        severity="P1",
        owner="owner@example.org",
        bounds=bounds,
        sql="SELECT 0 AS metric_value",
    )


def _result(status: str, severity: str = "P1") -> CheckResult:
    return CheckResult(
        check_id=f"matrix.{status.lower()}",
        title="matrix",
        entity="dim_client",
        severity=severity,
        owner="owner@example.org",
        status=status,
        metric_value=1.0,
        unit="count",
        bounds={"fail": {"max": 0}},
        message="",
    )


@pytest.mark.parametrize("operator", OPERATORS)
@pytest.mark.parametrize("threshold", THRESHOLDS)
@pytest.mark.parametrize("value", VALUES)
def test_single_operator_matrix(operator: str, threshold: float, value: float) -> None:
    bound = {operator: threshold}
    assert _violates(bound, value) is _reference(operator, threshold, value)


def test_unknown_operator_never_violates() -> None:
    assert _violates({"between": 10}, 999.0) is False


def test_empty_bound_never_violates() -> None:
    assert _violates({}, 1.0) is False


@pytest.mark.parametrize(
    "bound",
    [
        {"max": 0, "min": 10},
        {"max": 0, "eq": 0},
        {"min": 0, "eq": 0},
        {"max": 5, "min": 3, "eq": 4},
    ],
)
@pytest.mark.parametrize("value", VALUES)
def test_multi_operator_bound_is_any_of(bound: dict, value: float) -> None:
    expected = any(_reference(op, float(threshold), value) for op, threshold in bound.items())
    assert _violates(bound, value) is expected


@pytest.mark.parametrize(
    "bounds,value,expected",
    [
        ({"fail": {"max": 0}}, 0.0, PASS),
        ({"fail": {"max": 0}}, 1.0, FAIL),
        ({"fail": {"max": 0}, "warn": {"max": -2}}, 0.0, WARN),
        ({"fail": {"max": 0}, "warn": {"max": -2}}, -1.0, WARN),
        ({"fail": {"max": 0}, "warn": {"max": -2}}, 1.0, FAIL),
        ({"fail": {"min": 0.7}}, 1.0, PASS),
        ({"fail": {"min": 0.7}}, 0.42, FAIL),
        ({"fail": {"min": 0.7}, "warn": {"min": 0.9}}, 0.95, PASS),
        ({"fail": {"min": 0.7}, "warn": {"min": 0.9}}, 0.8, WARN),
        ({"fail": {"min": 0.7}, "warn": {"min": 0.9}}, 0.5, FAIL),
        ({"fail": {"eq": 0}}, 0.0, PASS),
        ({"fail": {"eq": 0}}, 3.0, FAIL),
        ({"fail": {"max": 2, "min": 1}}, 1.5, PASS),
        ({"fail": {"max": 2, "min": 1}}, 2.5, FAIL),
        ({"fail": {"max": 2, "min": 1}}, 0.5, FAIL),
    ],
)
def test_evaluate_maps_value_to_status(bounds: dict, value: float, expected: str) -> None:
    assert evaluate(_spec(bounds), value) == expected


@pytest.mark.parametrize("first", [PASS, WARN, FAIL, ERROR])
@pytest.mark.parametrize("second", [PASS, WARN, FAIL, ERROR])
def test_worst_status_is_the_maximum(first: str, second: str) -> None:
    order = {PASS: 0, WARN: 1, FAIL: 2, ERROR: 3}
    expected = first if order[first] >= order[second] else second
    assert worst_status([_result(first), _result(second)]) == expected


def test_worst_status_of_empty_run_is_pass() -> None:
    assert worst_status([]) == PASS


@pytest.mark.parametrize("fail_on", ["P1", "P2", "P3", "none"])
@pytest.mark.parametrize("severity", ["P1", "P2", "P3"])
def test_exit_code_for_single_failure(fail_on: str, severity: str) -> None:
    order = {"P1": 0, "P2": 1, "P3": 2}
    floor = order.get(fail_on, 0)
    expected = order[severity] + 1 if order[severity] <= floor else 0
    assert exit_code([_result(FAIL, severity)], fail_on) == expected


@pytest.mark.parametrize("fail_on", ["P1", "P2", "P3"])
@pytest.mark.parametrize("severity", ["P1", "P2", "P3"])
def test_exit_code_for_error_matches_failure(fail_on: str, severity: str) -> None:
    order = {"P1": 0, "P2": 1, "P3": 2}
    floor = order[fail_on]
    expected = order[severity] + 1 if order[severity] <= floor else 0
    assert exit_code([_result(ERROR, severity)], fail_on) == expected


@pytest.mark.parametrize("fail_on", ["P1", "P2", "P3", "none"])
def test_exit_code_is_zero_without_failures(fail_on: str) -> None:
    assert exit_code([_result(PASS), _result(WARN)], fail_on) == 0


@pytest.mark.parametrize("fail_on", ["P1", "P2", "P3"])
def test_exit_code_takes_worst_breach(fail_on: str) -> None:
    results = [_result(FAIL, "P1"), _result(FAIL, "P3")]
    expected = {"P1": 1, "P2": 1, "P3": 1}[fail_on]
    assert exit_code(results, fail_on) == expected
