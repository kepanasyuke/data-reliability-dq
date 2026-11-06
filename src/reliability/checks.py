"""Check catalogue and runner.

Design decision
---------------
A check is not a boolean. Every check returns a *number* (``metric_value``)
that is compared against ``fail`` / ``warn`` bounds. Numbers give you trends,
SLOs and thresholds that can be tuned without touching SQL - the same reason
monitoring systems are metric based. Row-level samples are an optional
``sample_sql`` that only runs when a check is not PASS, so the hot path stays
cheap on a 100M-row MPP table.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .db import Database
from .validators import VALIDATORS

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
ERROR = "ERROR"

STATUS_ORDER = {PASS: 0, WARN: 1, FAIL: 2, ERROR: 3}
SEVERITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}


class CheckConfigError(ValueError):
    """Raised for a malformed entry in the check catalogue."""


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    title: str
    entity: str
    severity: str
    owner: str
    bounds: Mapping[str, Mapping[str, float]]
    sql: str | None = None
    validator: str | None = None
    sample_sql: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    unit: str = "count"
    description: str = ""
    tags: tuple[str, ...] = ()

    @property
    def fail_bound(self) -> Mapping[str, float]:
        return self.bounds["fail"]


@dataclass
class CheckResult:
    check_id: str
    title: str
    entity: str
    severity: str
    owner: str
    status: str
    metric_value: float | None
    unit: str
    bounds: Mapping[str, Mapping[str, float]]
    message: str
    rows_examined: int | None = None
    duration_ms: int = 0
    started_at: str = ""
    samples: Sequence[Mapping[str, Any]] = ()

    @property
    def failed(self) -> bool:
        return self.status in (FAIL, ERROR)

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "status": self.status,
            "severity": self.severity,
            "metric_value": self.metric_value,
        }


def _violates(bound: Mapping[str, float], value: float) -> bool:
    def exceeded(operator: str, threshold: float) -> bool:
        comparisons = {
            "max": value > threshold,
            "min": value < threshold,
            "eq": value != threshold,
        }
        return comparisons.get(operator, False)

    return any(exceeded(operator, float(threshold)) for operator, threshold in bound.items())


def evaluate(spec: CheckSpec, value: float) -> str:
    """Map a metric value to PASS/WARN/FAIL using the spec bounds."""
    warn_bound = spec.bounds.get("warn")
    if _violates(spec.fail_bound, value):
        status = FAIL
    elif warn_bound and _violates(warn_bound, value):
        status = WARN
    else:
        status = PASS
    return status


def _describe_bound(bound: Mapping[str, float]) -> str:
    parts = []
    for operator, threshold in bound.items():
        symbol = {"max": "<=", "min": ">=", "eq": "=="}.get(operator, operator)
        parts.append(f"{symbol} {threshold:g}")
    return " and ".join(parts) or "n/a"


def load_checks(path: str | Path) -> list[CheckSpec]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = payload.get("checks", payload) if isinstance(payload, dict) else payload
    specs: list[CheckSpec] = []
    seen: set[str] = set()
    for raw in entries:
        check_id = raw.get("check_id")
        if not check_id:
            raise CheckConfigError(f"check without check_id: {raw!r}")
        if check_id in seen:
            raise CheckConfigError(f"duplicate check_id: {check_id}")
        seen.add(check_id)
        bounds = raw.get("bounds") or {}
        if "fail" not in bounds:
            raise CheckConfigError(f"{check_id}: 'bounds.fail' is required")
        if not raw.get("sql") and not raw.get("validator"):
            raise CheckConfigError(f"{check_id}: either 'sql' or 'validator' is required")
        if raw.get("validator") and raw["validator"] not in VALIDATORS:
            raise CheckConfigError(f"{check_id}: unknown validator {raw['validator']!r}")
        severity = raw.get("severity", "P2")
        if severity not in SEVERITY_ORDER:
            raise CheckConfigError(f"{check_id}: unknown severity {severity!r}")
        specs.append(
            CheckSpec(
                check_id=check_id,
                title=raw.get("title", check_id),
                entity=raw.get("entity", "unknown"),
                severity=severity,
                owner=raw.get("owner", ""),
                bounds=bounds,
                sql=raw.get("sql"),
                validator=raw.get("validator"),
                sample_sql=raw.get("sample_sql"),
                params=raw.get("params") or {},
                unit=raw.get("unit", "count"),
                description=raw.get("description", ""),
                tags=tuple(raw.get("tags") or ()),
            )
        )
    return specs


class CheckRunner:
    def __init__(
        self,
        db: Database,
        specs: Iterable[CheckSpec],
        runtime_params: Mapping[str, Any] | None = None,
        sample_limit: int = 5,
    ):
        self.db = db
        self.specs = list(specs)
        self.runtime_params = dict(runtime_params or {})
        self.sample_limit = sample_limit

    def _execute(self, spec: CheckSpec, params: Mapping[str, Any]) -> dict[str, Any]:
        if spec.validator:
            handler: Callable[[Database, Mapping[str, Any]], dict[str, Any]] = VALIDATORS[spec.validator]
            row = handler(self.db, params)
        else:
            rows = self.db.query(spec.sql or "", params)
            row = rows[0] if rows else {"metric_value": None, "message": "query returned no rows"}
        return row

    def _samples(self, spec: CheckSpec, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        samples: list[dict[str, Any]] = []
        if spec.sample_sql:
            try:
                samples = self.db.query(spec.sample_sql, params)[: self.sample_limit]
            except Exception as exc:  # samples must never break a run
                samples = [{"sample_error": str(exc)}]
        return samples

    def run_check(self, spec: CheckSpec) -> CheckResult:
        params = {**self.runtime_params, **spec.params}
        started = datetime.now(timezone.utc)
        clock = time.perf_counter()
        common = {
            "check_id": spec.check_id,
            "title": spec.title,
            "entity": spec.entity,
            "severity": spec.severity,
            "owner": spec.owner,
            "unit": spec.unit,
            "bounds": spec.bounds,
            "started_at": started.isoformat(timespec="seconds"),
        }
        try:
            row = self._execute(spec, params)
        except Exception as exc:
            result = CheckResult(
                status=ERROR,
                metric_value=None,
                message=f"check failed to execute: {exc}",
                duration_ms=int((time.perf_counter() - clock) * 1000),
                **common,
            )
        else:
            raw_value = row.get("metric_value")
            message = row.get("message") or ""
            rows_examined = row.get("rows_examined")
            if raw_value is None:
                status = FAIL
                value: float | None = None
                message = message or "metric is null - source table looks empty or stale"
            else:
                value = float(raw_value)
                status = evaluate(spec, value)
                message = message or (
                    f"{spec.unit}: {value:g} (threshold {_describe_bound(spec.bounds['fail'])})"
                )
            result = CheckResult(
                status=status,
                metric_value=value,
                message=message,
                rows_examined=int(rows_examined) if rows_examined is not None else None,
                duration_ms=int((time.perf_counter() - clock) * 1000),
                samples=self._samples(spec, params) if status != PASS else [],
                **common,
            )
        return result

    def run(self) -> list[CheckResult]:
        """Run every check; the worst status wins for the run overall."""
        return [self.run_check(spec) for spec in self.specs]


def worst_status(results: Iterable[CheckResult]) -> str:
    status = PASS
    for result in results:
        if STATUS_ORDER[result.status] > STATUS_ORDER[status]:
            status = result.status
    return status


def exit_code(results: Iterable[CheckResult], fail_on: str = "P1") -> int:
    """0 - clean, 1 - P1 breach, 2 - P2 breach, 3 - P3 breach only.

    ``fail_on`` is the *lightest* severity that should break the build:
    "P1" means only P1s matter, "P3" means any severity does.
    """
    results = list(results)
    floor = SEVERITY_ORDER.get(fail_on, 0)
    breached = [
        r
        for r in results
        if r.failed and SEVERITY_ORDER[r.severity] <= floor
    ]
    code = 0
    if breached:
        code = min(SEVERITY_ORDER[r.severity] for r in breached) + 1
    return code
