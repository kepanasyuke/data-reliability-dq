"""Инварианты каталога проверок и прогон каждой проверки на демо-хранилище.

Каталог — это контракт между SQL, порогами и отчётом, поэтому каждая запись
проверяется отдельно: одна строка ``checks/dq_checks.json`` = один набор тестов.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reliability import store
from reliability.checks import (
    FAIL,
    PASS,
    WARN,
    CheckRunner,
    load_checks,
)
from reliability.db import Database
from reliability.synth import build_demo
from reliability.validators import VALIDATORS

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOGUE_PATH = REPO_ROOT / "checks" / "dq_checks.json"
KNOWN_OPERATORS = {"max", "min", "eq"}
KNOWN_UNITS = {"count", "ratio", "hours", "rows"}
KNOWN_SEVERITIES = {"P1", "P2", "P3"}

SPECS = load_checks(CATALOGUE_PATH)
PAYLOAD = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
RAW_CHECKS = PAYLOAD["checks"]


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory: pytest.TempPathFactory):
    path = tmp_path_factory.mktemp("catalogue") / "warehouse.sqlite3"
    with Database(str(path)) as db:
        store.ensure_schema(db)
        build_demo(db, REPO_ROOT, inject_defects=True, seed=3)
        yield db


@pytest.mark.parametrize("spec", SPECS, ids=[spec.check_id for spec in SPECS])
def test_check_id_is_namespaced(spec) -> None:
    assert "." in spec.check_id
    assert spec.check_id == spec.check_id.strip()


@pytest.mark.parametrize("spec", SPECS, ids=[spec.check_id for spec in SPECS])
def test_entity_is_filled(spec) -> None:
    assert spec.entity
    assert spec.entity == spec.entity.strip()


@pytest.mark.parametrize("spec", SPECS, ids=[spec.check_id for spec in SPECS])
def test_severity_is_known(spec) -> None:
    assert spec.severity in KNOWN_SEVERITIES


@pytest.mark.parametrize("spec", SPECS, ids=[spec.check_id for spec in SPECS])
def test_unit_is_known(spec) -> None:
    assert spec.unit in KNOWN_UNITS


@pytest.mark.parametrize("spec", SPECS, ids=[spec.check_id for spec in SPECS])
def test_fail_bound_uses_known_operator(spec) -> None:
    assert "fail" in spec.bounds
    assert set(spec.bounds["fail"]) <= KNOWN_OPERATORS
    assert spec.bounds["fail"]


@pytest.mark.parametrize("spec", SPECS, ids=[spec.check_id for spec in SPECS])
def test_warn_bound_uses_known_operator(spec) -> None:
    warn = spec.bounds.get("warn")
    assert warn is None or set(warn) <= KNOWN_OPERATORS


@pytest.mark.parametrize("spec", SPECS, ids=[spec.check_id for spec in SPECS])
def test_source_is_sql_or_validator(spec) -> None:
    assert bool(spec.sql) is not bool(spec.validator)


@pytest.mark.parametrize("spec", SPECS, ids=[spec.check_id for spec in SPECS])
def test_validator_is_registered(spec) -> None:
    assert spec.validator is None or spec.validator in VALIDATORS


@pytest.mark.parametrize("spec", SPECS, ids=[spec.check_id for spec in SPECS])
def test_sample_sql_is_a_select(spec) -> None:
    statement = (spec.sample_sql or "SELECT").lstrip()
    assert statement.startswith(("SELECT", "WITH"))


@pytest.mark.parametrize("spec", SPECS, ids=[spec.check_id for spec in SPECS])
def test_title_and_description_are_readable(spec) -> None:
    assert len(spec.title) > 3
    assert len(spec.description) > 10


@pytest.mark.parametrize("spec", SPECS, ids=[spec.check_id for spec in SPECS])
def test_tags_are_a_tuple(spec) -> None:
    assert isinstance(spec.tags, tuple)
    assert all(isinstance(tag, str) and tag for tag in spec.tags)


@pytest.mark.parametrize("raw", RAW_CHECKS, ids=[raw["check_id"] for raw in RAW_CHECKS])
def test_raw_entry_has_owner_or_default(raw: dict) -> None:
    owner = raw.get("owner") or PAYLOAD.get("owner_default")
    assert owner and "@" in owner


@pytest.mark.parametrize("raw", RAW_CHECKS, ids=[raw["check_id"] for raw in RAW_CHECKS])
def test_raw_bounds_are_numbers(raw: dict) -> None:
    for level in raw["bounds"].values():
        for threshold in level.values():
            assert isinstance(threshold, (int, float))


def test_catalogue_version_is_integer() -> None:
    assert isinstance(PAYLOAD["version"], int)
    assert PAYLOAD["version"] >= 1


def test_catalogue_has_expected_size() -> None:
    assert len(SPECS) == 12


def test_catalogue_ids_are_unique() -> None:
    ids = [spec.check_id for spec in SPECS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("spec", SPECS, ids=[spec.check_id for spec in SPECS])
def test_each_check_runs_and_reports_known_status(demo_db: Database, spec) -> None:
    runner = CheckRunner(demo_db, [spec], {"window_days": 30, "window_start": "2026-01-01"})
    result = runner.run_check(spec)
    assert result.status in {PASS, WARN, FAIL}
    assert result.duration_ms >= 0


@pytest.mark.parametrize("spec", SPECS, ids=[spec.check_id for spec in SPECS])
def test_each_check_returns_number_or_null(demo_db: Database, spec) -> None:
    runner = CheckRunner(demo_db, [spec], {"window_days": 30, "window_start": "2026-01-01"})
    result = runner.run_check(spec)
    assert result.metric_value is None or isinstance(result.metric_value, float)


@pytest.mark.parametrize("spec", SPECS, ids=[spec.check_id for spec in SPECS])
def test_result_keeps_spec_metadata(demo_db: Database, spec) -> None:
    runner = CheckRunner(demo_db, [spec], {"window_days": 30, "window_start": "2026-01-01"})
    result = runner.run_check(spec)
    assert result.check_id == spec.check_id
    assert result.entity == spec.entity
    assert result.severity == spec.severity
    assert result.bounds == spec.bounds


@pytest.mark.parametrize("spec", SPECS, ids=[spec.check_id for spec in SPECS])
def test_samples_only_for_non_pass(demo_db: Database, spec) -> None:
    runner = CheckRunner(demo_db, [spec], {"window_days": 30, "window_start": "2026-01-01"})
    result = runner.run_check(spec)
    expected = spec.sample_sql is not None and result.status != PASS
    assert bool(result.samples) == expected


@pytest.mark.parametrize("spec", SPECS, ids=[spec.check_id for spec in SPECS])
def test_as_dict_is_json_ready(demo_db: Database, spec) -> None:
    runner = CheckRunner(demo_db, [spec], {"window_days": 30, "window_start": "2026-01-01"})
    payload = runner.run_check(spec).as_dict()
    assert set(payload) == {"check_id", "status", "severity", "metric_value"}
    assert json.loads(json.dumps(payload))["check_id"] == spec.check_id
