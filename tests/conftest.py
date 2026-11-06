from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from reliability.config import Config  # noqa: E402


@pytest.fixture()
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture()
def settings(tmp_path: Path) -> Config:
    """Config that keeps every artifact inside the pytest tmp dir."""
    return Config(
        root=tmp_path,
        env="test",
        dsn=str(tmp_path / "warehouse.sqlite3"),
        checks_path=REPO_ROOT / "checks" / "dq_checks.json",
        window_days=30,
        fail_on="P1",
        owner="test@example.org",
        sla_hours={"fct_account_turnover_daily": 26, "fct_loan_portfolio_monthly": 24},
        metrics_path=tmp_path / "metrics" / "dq.prom",
        report_path=tmp_path / "dq_report.md",
        notify={"channel": "#test", "dry_run": True},
        params={},
    )
