"""Configuration loading (JSON, no external dependency)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = "config/demo.json"


@dataclass(frozen=True)
class Config:
    root: Path
    env: str
    dsn: str
    checks_path: Path
    window_days: int
    fail_on: str
    owner: str
    sla_hours: dict[str, float] = field(default_factory=dict)
    metrics_path: Path = Path("artifacts/metrics/dq.prom")
    report_path: Path = Path("artifacts/dq_report.md")
    notify: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def database_path(self) -> str:
        """DSN usable by ``Database``; relative SQLite paths resolve to root."""
        if self.dsn.startswith(("postgres://", "postgresql://", ":memory:")):
            return self.dsn
        candidate = Path(self.dsn)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return str(candidate)

    def sla_for(self, entity: str, default: float = 24.0) -> float:
        return float(self.sla_hours.get(entity, default))


def load_config(path: str | Path | None = None) -> Config:
    config_path = Path(path or DEFAULT_CONFIG_PATH)
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    root = config_path.parent.parent

    def resolve(value: str) -> Path:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else root / candidate

    return Config(
        root=root,
        env=raw.get("env", "demo"),
        dsn=raw.get("dsn", "artifacts/warehouse.sqlite3"),
        checks_path=resolve(raw.get("checks", "checks/dq_checks.json")),
        window_days=int(raw.get("window_days", 30)),
        fail_on=str(raw.get("fail_on", "P1")),
        owner=raw.get("owner", "data-reliability@example.org"),
        sla_hours={k: float(v) for k, v in (raw.get("sla_hours") or {}).items()},
        metrics_path=resolve(raw.get("artifacts", {}).get("metrics", "artifacts/metrics/dq.prom")),
        report_path=resolve(raw.get("artifacts", {}).get("report", "artifacts/dq_report.md")),
        notify=raw.get("notify") or {},
        params=raw.get("params") or {},
    )
