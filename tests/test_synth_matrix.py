"""Генератор демо-хранилища: детерминизм, объёмы и внесённые дефекты."""

from __future__ import annotations

from pathlib import Path

import pytest

from reliability import store
from reliability.db import Database
from reliability.synth import build_demo
from reliability.validators import inn_valid, ogrn_valid

REPO_ROOT = Path(__file__).resolve().parents[1]
SEEDS = [1, 2, 3, 4, 5]


@pytest.fixture()
def warehouse(tmp_path: Path):
    opened: list[Database] = []

    def build(seed: int = 1, inject: bool = True, n_clients: int = 6, days: int = 3):
        path = tmp_path / f"w-{seed}-{int(inject)}-{n_clients}-{days}.sqlite3"
        db = Database(str(path))
        store.ensure_schema(db)
        opened.append(db)
        stats = build_demo(
            db,
            REPO_ROOT,
            inject_defects=inject,
            seed=seed,
            days=days,
            n_clients=n_clients,
        )
        return db, stats

    yield build
    for db in opened:
        db.close()


@pytest.mark.parametrize("seed", SEEDS)
def test_stats_have_all_keys(warehouse, seed: int) -> None:
    _, stats = warehouse(seed=seed, inject=False)
    assert set(stats) == {
        "clients",
        "accounts",
        "turnover_rows",
        "loan_rows",
        "days",
        "injected_defects",
    }


@pytest.mark.parametrize("seed", SEEDS)
def test_client_count_matches_request(warehouse, seed: int) -> None:
    db, stats = warehouse(seed=seed, inject=False, n_clients=7)
    assert stats["clients"] == 7
    assert db.scalar("SELECT COUNT(*) FROM dim_client") == 7


@pytest.mark.parametrize("seed", SEEDS)
def test_turnover_rows_equal_accounts_times_days(warehouse, seed: int) -> None:
    db, stats = warehouse(seed=seed, inject=False, days=4)
    assert stats["turnover_rows"] == stats["accounts"] * 4
    assert db.scalar("SELECT COUNT(*) FROM fct_account_turnover_daily") == stats["turnover_rows"]


@pytest.mark.parametrize("seed", SEEDS)
def test_products_are_reference_data(warehouse, seed: int) -> None:
    db, _ = warehouse(seed=seed, inject=False)
    assert db.scalar("SELECT COUNT(*) FROM dim_product") == 6
    assert db.scalar("SELECT COUNT(*) FROM dim_product WHERE product_family = 'credit'") == 3


@pytest.mark.parametrize("seed", SEEDS)
def test_clean_warehouse_has_valid_inn(warehouse, seed: int) -> None:
    db, _ = warehouse(seed=seed, inject=False)
    inns = [row["inn"] for row in db.query("SELECT inn FROM dim_client")]
    assert all(inn_valid(value) for value in inns)


@pytest.mark.parametrize("seed", SEEDS)
def test_clean_warehouse_has_valid_ogrn(warehouse, seed: int) -> None:
    db, _ = warehouse(seed=seed, inject=False)
    ogrns = [row["ogrn"] for row in db.query("SELECT ogrn FROM dim_client")]
    assert all(ogrn_valid(value) for value in ogrns)


@pytest.mark.parametrize("seed", SEEDS)
def test_clean_warehouse_has_no_orphans(warehouse, seed: int) -> None:
    db, _ = warehouse(seed=seed, inject=False)
    orphans = db.scalar(
        """
        SELECT COUNT(*) FROM fct_account_turnover_daily t
        LEFT JOIN dim_client c ON c.client_id = t.client_id
        WHERE c.client_id IS NULL
        """
    )
    assert orphans == 0


@pytest.mark.parametrize("seed", SEEDS)
def test_defective_warehouse_reports_every_defect(warehouse, seed: int) -> None:
    _, stats = warehouse(seed=seed, inject=True)
    assert len(stats["injected_defects"]) == 11


@pytest.mark.parametrize("seed", SEEDS)
def test_clean_warehouse_reports_no_defects(warehouse, seed: int) -> None:
    _, stats = warehouse(seed=seed, inject=False)
    assert stats["injected_defects"] == []


@pytest.mark.parametrize("seed", SEEDS)
def test_defect_mode_adds_orphan_client(warehouse, seed: int) -> None:
    db, _ = warehouse(seed=seed, inject=True, days=15)
    assert db.scalar(
        "SELECT COUNT(*) FROM fct_account_turnover_daily WHERE client_id = 'CL999999'"
    ) == 6


@pytest.mark.parametrize("seed", SEEDS)
def test_defect_mode_breaks_three_inns(warehouse, seed: int) -> None:
    db, _ = warehouse(seed=seed, inject=True)
    inns = [row["inn"] for row in db.query("SELECT inn FROM dim_client")]
    assert sum(1 for value in inns if not inn_valid(value)) == 3


@pytest.mark.parametrize("seed", SEEDS)
def test_defect_mode_has_negative_balances(warehouse, seed: int) -> None:
    db, _ = warehouse(seed=seed, inject=True)
    assert db.scalar(
        "SELECT COUNT(*) FROM fct_account_turnover_daily WHERE balance_eod < 0"
    ) == 4


@pytest.mark.parametrize("seed", SEEDS)
def test_batch_metadata_has_two_loads(warehouse, seed: int) -> None:
    db, _ = warehouse(seed=seed, inject=False)
    assert db.scalar("SELECT COUNT(*) FROM etl_batch") == 2


@pytest.mark.parametrize("seed", SEEDS)
def test_defect_mode_lies_about_rowcount(warehouse, seed: int) -> None:
    db, _ = warehouse(seed=seed, inject=True)
    actual = db.scalar("SELECT COUNT(*) FROM fct_account_turnover_daily")
    loaded = db.scalar(
        "SELECT rows_loaded FROM etl_batch WHERE entity = 'fct_account_turnover_daily'"
    )
    assert loaded == actual + 1234


@pytest.mark.parametrize("seed", SEEDS)
def test_same_seed_is_deterministic(warehouse, seed: int) -> None:
    _, first = warehouse(seed=seed, inject=True)
    _, second = warehouse(seed=seed, inject=True)
    assert first == second


def test_different_seeds_differ(warehouse) -> None:
    _, first = warehouse(seed=1, inject=False)
    _, second = warehouse(seed=2, inject=False)
    assert first != second


@pytest.mark.parametrize("seed", SEEDS)
def test_account_ids_are_padded(warehouse, seed: int) -> None:
    db, _ = warehouse(seed=seed, inject=False)
    account_ids = [
        row["account_id"]
        for row in db.query("SELECT DISTINCT account_id FROM fct_account_turnover_daily")
    ]
    assert all(len(value) == 10 and value.startswith("AC") for value in account_ids)


@pytest.mark.parametrize("seed", SEEDS)
def test_client_ids_are_padded(warehouse, seed: int) -> None:
    db, _ = warehouse(seed=seed, inject=False)
    ids = [row["client_id"] for row in db.query("SELECT client_id FROM dim_client")]
    assert all(len(value) == 8 and value.startswith("CL") for value in ids)
