"""Banking-domain validators that are awkward to express in pure SQL.

Each validator receives the open connection and the merged check params and
returns a dict with ``metric_value`` plus optional ``rows_examined``/``message``
- the same contract as a SQL-based check.

Registry functions are referenced from the check catalogue by name
(``"validator": "inn_valid_ratio"``), which is how new rule types are added
without touching the runner.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .db import Database

INN_WEIGHTS_10 = (2, 4, 10, 3, 5, 9, 4, 6, 8)
INN_WEIGHTS_11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
INN_WEIGHTS_12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)


def _inn_checksum(digits: str, weights: tuple[int, ...]) -> int:
    total = sum(int(d) * w for d, w in zip(digits, weights))
    return total % 11 % 10


def inn_valid(inn: str | None) -> bool:
    """Validate a Russian INN: 10 digits for legal entities, 12 for sole traders."""
    value = str(inn).strip() if inn else ""
    valid = False
    if value.isdigit():
        if len(value) == 10:
            valid = _inn_checksum(value[:9], INN_WEIGHTS_10) == int(value[9])
        elif len(value) == 12:
            valid = (
                _inn_checksum(value[:10], INN_WEIGHTS_11) == int(value[10])
                and _inn_checksum(value[:11], INN_WEIGHTS_12) == int(value[11])
            )
    return valid


def ogrn_valid(ogrn: str | None) -> bool:
    """Validate a Russian OGRN (13 digits) / OGRNIP (15 digits) control digit."""
    value = str(ogrn).strip() if ogrn else ""
    valid = False
    if value.isdigit():
        if len(value) == 13:
            valid = int(value[:12]) % 11 % 10 == int(value[12])
        elif len(value) == 15:
            valid = int(value[:14]) % 13 % 10 == int(value[14])
    return valid


def _ratio_report(values: list[str], predicate: Callable[[str], bool]) -> dict[str, Any]:
    if values:
        valid = sum(1 for value in values if predicate(value))
        report = {
            "metric_value": round(valid / len(values), 6),
            "rows_examined": len(values),
            "message": f"{len(values) - valid} of {len(values)} values failed the checksum",
        }
    else:
        report = {
            "metric_value": 0.0,
            "rows_examined": 0,
            "message": "no reference data to validate",
        }
    return report


def inn_valid_ratio(db: Database, params: Mapping[str, Any]) -> dict[str, Any]:
    rows = db.query(
        """
        SELECT DISTINCT inn
        FROM dim_client
        WHERE inn IS NOT NULL
        """
    )
    return _ratio_report([row["inn"] for row in rows], inn_valid)


def ogrn_valid_ratio(db: Database, params: Mapping[str, Any]) -> dict[str, Any]:
    rows = db.query(
        """
        SELECT DISTINCT ogrn
        FROM dim_client
        WHERE ogrn IS NOT NULL
        """
    )
    return _ratio_report([row["ogrn"] for row in rows], ogrn_valid)


VALIDATORS: dict[str, Callable[[Database, Mapping[str, Any]], dict[str, Any]]] = {
    "inn_valid_ratio": inn_valid_ratio,
    "ogrn_valid_ratio": ogrn_valid_ratio,
}
