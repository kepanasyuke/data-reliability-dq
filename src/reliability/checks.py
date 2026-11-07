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


_OPERATORS: dict[str, Callable[[float, float], bool]] = {
    "max": lambda value, threshold: value > threshold,
    "min": lambda value, threshold: value < threshold,
    "eq": lambda value, threshold: value != threshold,
}


def _violates(bound: Mapping[str, float], value: float) -> bool:
    """True when any operator of the bound is breached by ``value``."""
    return any(
        _OPERATORS[operator](value, float(threshold))
        for operator, threshold in bound.items()
        if operator in _OPERATORS
    )


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


def _require_check_id(raw: Mapping[str, Any]) -> str:
    check_id = raw.get("check_id")
    if not check_id:
        raise CheckConfigError(f"check without check_id: {raw!r}")
    return check_id


def _require_bounds(check_id: str, raw: Mapping[str, Any]) -> Mapping[str, Mapping[str, float]]:
    bounds = raw.get("bounds") or {}
    if "fail" not in bounds:
        raise CheckConfigError(f"{check_id}: 'bounds.fail' is required")
    return bounds


def _require_source(check_id: str, raw: Mapping[str, Any]) -> None:
    validator = raw.get("validator")
    if not raw.get("sql") and not validator:
        raise CheckConfigError(f"{check_id}: either 'sql' or 'validator' is required")
    if validator and validator not in VALIDATORS:
        raise CheckConfigError(f"{check_id}: unknown validator {validator!r}")


def _require_severity(check_id: str, raw: Mapping[str, Any]) -> str:
    severity = raw.get("severity", "P2")
    if severity not in SEVERITY_ORDER:
        raise CheckConfigError(f"{check_id}: unknown severity {severity!r}")
    return severity


def _spec_from_raw(raw: Mapping[str, Any]) -> CheckSpec:
    check_id = _require_check_id(raw)
    _require_source(check_id, raw)
    return CheckSpec(
        check_id=check_id,
        title=raw.get("title", check_id),
        entity=raw.get("entity", "unknown"),
        severity=_require_severity(check_id, raw),
        owner=raw.get("owner", ""),
        bounds=_require_bounds(check_id, raw),
        sql=raw.get("sql"),
        validator=raw.get("validator"),
        sample_sql=raw.get("sample_sql"),
        params=raw.get("params") or {},
        unit=raw.get("unit", "count"),
        description=raw.get("description", ""),
        tags=tuple(raw.get("tags") or ()),
    )


def _require_unique(specs: Sequence[CheckSpec]) -> list[CheckSpec]:
    seen: set[str] = set()
    for spec in specs:
        if spec.check_id in seen:
            raise CheckConfigError(f"duplicate check_id: {spec.check_id}")
        seen.add(spec.check_id)
    return list(specs)


def load_checks(path: str | Path) -> list[CheckSpec]:
    """Parse the catalogue and return validated specs."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = payload.get("checks", payload) if isinstance(payload, dict) else payload
    return _require_unique([_spec_from_raw(raw) for raw in entries])


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

    def _common(self, spec: CheckSpec, started: datetime) -> dict[str, Any]:
        return {
            "check_id": spec.check_id,
            "title": spec.title,
            "entity": spec.entity,
            "severity": spec.severity,
            "owner": spec.owner,
            "unit": spec.unit,
            "bounds": spec.bounds,
            "started_at": started.isoformat(timespec="seconds"),
        }

    def _outcome(self, spec: CheckSpec, row: Mapping[str, Any]) -> tuple[str, float | None, str, Any]:
        raw_value = row.get("metric_value")
        message = row.get("message") or ""
        if raw_value is None:
            status: str = FAIL
            value: float | None = None
            message = message or "metric is null - source table looks empty or stale"
        else:
            value = float(raw_value)
            status = evaluate(spec, value)
            message = message or (
                f"{spec.unit}: {value:g} (threshold {_describe_bound(spec.bounds['fail'])})"
            )
        return status, value, message, row.get("rows_examined")

    def _execution_error(
        self, spec: CheckSpec, started: datetime, duration_ms: int, exc: Exception
    ) -> CheckResult:
        return CheckResult(
            status=ERROR,
            metric_value=None,
            message=f"check failed to execute: {exc}",
            duration_ms=duration_ms,
            **self._common(spec, started),
        )

    def _result_from_row(
        self,
        spec: CheckSpec,
        params: Mapping[str, Any],
        started: datetime,
        duration_ms: int,
        row: Mapping[str, Any],
    ) -> CheckResult:
        status, value, message, rows_examined = self._outcome(spec, row)
        return CheckResult(
            status=status,
            metric_value=value,
            message=message,
            rows_examined=int(rows_examined) if rows_examined is not None else None,
            duration_ms=duration_ms,
            samples=self._samples(spec, params) if status != PASS else [],
            **self._common(spec, started),
        )

    def run_check(self, spec: CheckSpec) -> CheckResult:
        params = {**self.runtime_params, **spec.params}
        started = datetime.now(timezone.utc)
        clock = time.perf_counter()
        try:
            row = self._execute(spec, params)
        except Exception as exc:
            duration_ms = int((time.perf_counter() - clock) * 1000)
            result = self._execution_error(spec, started, duration_ms, exc)
        else:
            duration_ms = int((time.perf_counter() - clock) * 1000)
            result = self._result_from_row(spec, params, started, duration_ms, row)
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
