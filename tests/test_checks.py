"""Логика порогов, каталога и exit-кодов — то, что должно быть
предсказуемым для CI и для Airflow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reliability.checks import (
    ERROR,
    FAIL,
    PASS,
    WARN,
    CheckConfigError,
    CheckResult,
    CheckSpec,
    evaluate,
    exit_code,
    load_checks,
    worst_status,
)


def make_spec(**overrides) -> CheckSpec:
    defaults = dict(
        check_id="demo.check",
        title="demo",
        entity="dim_client",
        severity="P1",
        owner="owner@example.org",
        bounds={"fail": {"max": 0}},
        sql="SELECT 0 AS metric_value",
    )
    defaults.update(overrides)
    return CheckSpec(**defaults)


def make_result(status: str, severity: str = "P1") -> CheckResult:
    return CheckResult(
        check_id="demo.check",
        title="demo",
        entity="dim_client",
        severity=severity,
        owner="owner@example.org",
        status=status,
        metric_value=1.0,
        unit="count",
        bounds={"fail": {"max": 0}},
        message="",
    )


def test_evaluate_max_bound() -> None:
    spec = make_spec(bounds={"fail": {"max": 0}})
    assert evaluate(spec, 0) == PASS
    assert evaluate(spec, 1) == FAIL


def test_evaluate_min_bound() -> None:
    spec = make_spec(bounds={"fail": {"min": 0.7}})
    assert evaluate(spec, 1.0) == PASS
    assert evaluate(spec, 0.42) == FAIL


def test_evaluate_warn_zone() -> None:
    spec = make_spec(bounds={"fail": {"max": 0.05}, "warn": {"max": 0.02}})
    assert evaluate(spec, 0.01) == PASS
    assert evaluate(spec, 0.03) == WARN
    assert evaluate(spec, 0.2) == FAIL


def test_worst_status_and_exit_code() -> None:
    results = [make_result(PASS), make_result(FAIL, "P2"), make_result(WARN, "P1")]
    assert worst_status(results) == FAIL
    # fail_on=P1: P2 не валит сборку
    assert exit_code(results, "P1") == 0
    # fail_on=P2: валит, код по худшей затронутой severity
    assert exit_code(results, "P2") == 2
    # fail_on=P3: тоже P2
    assert exit_code(results, "P3") == 2


def test_error_result_breaks_build() -> None:
    assert exit_code([make_result(ERROR, "P1")], "P1") == 1
    assert exit_code([make_result(ERROR, "P3")], "P1") == 0


def test_load_checks_reads_catalogue(repo_root: Path) -> None:
    specs = load_checks(repo_root / "checks" / "dq_checks.json")
    ids = {spec.check_id for spec in specs}
    assert len(specs) >= 10
    assert {"turnover.freshness", "turnover.balance_continuity", "batch.rowcount_match"} <= ids
    assert all("fail" in spec.bounds for spec in specs)


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "checks.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "check, fragment",
    [
        ({"check_id": "x", "sql": "SELECT 1", "bounds": {}}, "bounds.fail"),
        ({"check_id": "x", "sql": "SELECT 1", "bounds": {"fail": {"max": 0}}}, None)
    ],
)
def test_load_checks_validation(tmp_path: Path, check: dict, fragment: str | None) -> None:
    if fragment is None:
        assert load_checks(_write(tmp_path, {"checks": [check]}))[0].check_id == "x"
    else:
        with pytest.raises(CheckConfigError, match=fragment.replace(".", r"\.")):
            load_checks(_write(tmp_path, {"checks": [check]}))


def test_unknown_validator_is_rejected(tmp_path: Path) -> None:
    payload = {"checks": [{"check_id": "x", "validator": "nope", "bounds": {"fail": {"max": 0}}}]}
    with pytest.raises(CheckConfigError, match="unknown validator"):
        load_checks(_write(tmp_path, payload))


def test_duplicate_check_id_is_rejected(tmp_path: Path) -> None:
    payload = {
        "checks": [
            {"check_id": "x", "sql": "SELECT 1 AS metric_value", "bounds": {"fail": {"max": 0}}},
            {"check_id": "x", "sql": "SELECT 1 AS metric_value", "bounds": {"fail": {"max": 0}}},
        ]
    }
    with pytest.raises(CheckConfigError, match="duplicate"):
        load_checks(_write(tmp_path, payload))
