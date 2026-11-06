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
    if not inn:
        return False
    value = str(inn).strip()
    if not value.isdigit():
        return False
    if len(value) == 10:
        return _inn_checksum(value[:9], INN_WEIGHTS_10) == int(value[9])
    if len(value) == 12:
        first = _inn_checksum(value[:10], INN_WEIGHTS_11) == int(value[10])
        second = _inn_checksum(value[:11], INN_WEIGHTS_12) == int(value[11])
        return first and second
    return False


def ogrn_valid(ogrn: str | None) -> bool:
    """Validate a Russian OGRN (13 digits) / OGRNIP (15 digits) control digit."""
    if not ogrn:
        return False
    value = str(ogrn).strip()
    if not value.isdigit():
        return False
    if len(value) == 13:
        return int(value[:12]) % 11 % 10 == int(value[12])
    if len(value) == 15:
        return int(value[:14]) % 13 % 10 == int(value[14])
    return False


def _ratio_report(values: list[str], predicate: Callable[[str], bool]) -> dict[str, Any]:
    if not values:
        return {
            "metric_value": 0.0,
            "rows_examined": 0,
            "message": "no reference data to validate",
        }
    valid = sum(1 for value in values if predicate(value))
    return {
        "metric_value": round(valid / len(values), 6),
        "rows_examined": len(values),
        "message": f"{len(values) - valid} of {len(values)} values failed the checksum",
    }


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
