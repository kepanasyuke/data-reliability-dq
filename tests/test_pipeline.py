"""End-to-end: дефекты -> падающие проверки -> инциденты -> чистая перезаливка ->
автозакрытие -> MTTR. Ровно тот сценарий, который показывает `python run.py demo`."""

from __future__ import annotations

from pathlib import Path

from reliability import incidents, store
from reliability.checks import PASS, load_checks
from reliability.cli import _runtime_params, execute_run
from reliability.config import Config
from reliability.db import Database
from reliability.metrics import render
from reliability.synth import build_demo

EXPECTED_FAILURES = {
    "client.inn.valid",
    "client.ogrn.valid",
    "client.segment.completeness",
    "turnover.duplicate_grain",
    "turnover.orphan_client",
    "turnover.balance_non_negative",
    "turnover.balance_continuity",
    "turnover.volume_anomaly",
    "loans.freshness",
    "loans.debt_within_limit",
    "batch.rowcount_match",
}


def _broken_run(settings: Config, repo_root: Path) -> tuple[set[str], list[dict], list[dict]]:
    """Seed the defective warehouse and run the catalogue over it."""
    with Database(settings.database_path) as db:
        store.ensure_schema(db)
        build_demo(db, repo_root, inject_defects=True, seed=7)
        broken = execute_run(settings, db, verbose=False)
        failed = {result.check_id for result in broken["results"] if result.failed}
        opened = incidents.open_incidents(db)
    return failed, opened


def _healthy_run(settings: Config, repo_root: Path) -> tuple[dict, list[dict], dict]:
    """Reload the same warehouse cleanly and run the catalogue again."""
    with Database(settings.database_path) as db:
        store.ensure_schema(db)
        build_demo(db, repo_root, inject_defects=False, seed=7)
        healthy = execute_run(settings, db, verbose=False)
        remaining = incidents.open_incidents(db)
        stats = incidents.mttr(db)
    return healthy, remaining, stats


def test_defects_are_detected_and_then_resolved(settings: Config, repo_root: Path) -> None:
    failed, opened = _broken_run(settings, repo_root)

    # каждый внесённый дефект должен быть пойман своей проверкой
    assert EXPECTED_FAILURES <= failed
    # свежесть оборотов не должна страдать: данные приехали вовремя
    assert "turnover.freshness" not in failed

    assert len(opened) == len(failed)
    assert {row["severity"] for row in opened} <= {"P1", "P2", "P3"}

    healthy, remaining, stats = _healthy_run(settings, repo_root)

    assert all(result.status == PASS for result in healthy["results"]), [
        (r.check_id, r.status, r.message) for r in healthy["results"] if r.status != PASS
    ]
    assert remaining == []
    assert stats["overall"]["resolved"] == len(failed)
    assert stats["overall"]["avg_minutes"] >= 0

    assert settings.report_path.exists()
    assert "Отчёт Data Quality" in settings.report_path.read_text(encoding="utf-8")
    assert "dq_check_status" in settings.metrics_path.read_text(encoding="utf-8")


def test_metrics_export_contains_all_checks(settings: Config, repo_root: Path) -> None:
    specs = load_checks(settings.checks_path)
    with Database(settings.database_path) as db:
        store.ensure_schema(db)
        build_demo(db, repo_root, inject_defects=True, seed=11)
        outcome = execute_run(settings, db, verbose=False)

    text = render(outcome["results"], open_incidents={"P1": 3})
    assert text.count('dq_check_status{') == len(specs)
    assert 'dq_incident_open{severity="P1"} 3' in text
    assert "dq_run_timestamp_seconds" in text
    labels = text.splitlines()
    assert any(line.startswith("# TYPE dq_check_status gauge") for line in labels)


def test_runtime_params_use_window_from_config(settings: Config) -> None:
    from datetime import datetime, timezone

    params = _runtime_params(settings, datetime(2026, 5, 20, 12, tzinfo=timezone.utc))
    assert params["window_start"] == "2026-04-20"
    assert params["sla_turnover_hours"] == 26
    assert params["sla_loans_hours"] == 24
