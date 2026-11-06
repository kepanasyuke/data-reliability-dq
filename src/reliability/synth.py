"""Deterministic demo warehouse generator with injectable defects.

The point of the generator is not "nice data" but a *reproducible incident*:
``inject_defects=True`` inserts exactly the failures the check catalogue is
written to catch (broken INN checksum, duplicate grain, orphan keys, partial
load, stale loan portfolio, debt above limit, batch rowcount mismatch).
``inject_defects=False`` rebuilds the same warehouse cleanly, which is how the
demo shows an incident being resolved and MTTR being computed.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .db import Database
from .validators import INN_WEIGHTS_10, INN_WEIGHTS_11, INN_WEIGHTS_12, _inn_checksum

SCHEMA_PATH = Path("sql") / "demo" / "01_schema.sql"

PRODUCTS: list[tuple[str, str, str, int]] = [
    ("RKO", "Расчётно-кассовое обслуживание", "transactional", 0),
    ("ACQUIRING", "Эквайринг", "transactional", 0),
    ("DEPOSIT", "Депозит", "deposit", 0),
    ("OVERDRAFT", "Овердрафт", "credit", 1),
    ("CREDIT_LINE", "Кредитная линия", "credit", 1),
    ("TRADE_FIN", "Торговое финансирование", "credit", 1),
]
SEGMENTS = ["МСБ", "Средний бизнес", "Крупный бизнес", "Госсектор"]
NAME_PARTS = [
    "Строй", "Логистик", "Агро", "Мед", "Техно", "Трейд", "Энерго", "Пром",
    "Финанс", "Групп", "Ресурс", "Инвест",
]
CURRENCIES = ["RUB"] * 8 + ["USD", "EUR"]
SRC_SYSTEMS = ["ABS_CORE", "ABS_CORE", "ABS_CORE", "CRM", "DWH_STAGE"]


def make_inn(rng: random.Random, legal_entity: bool = True) -> str:
    if legal_entity:
        prefix = "".join(str(rng.randint(0, 9)) for _ in range(9))
        inn = prefix + str(_inn_checksum(prefix, INN_WEIGHTS_10))
    else:
        prefix = "".join(str(rng.randint(0, 9)) for _ in range(10))
        d11 = _inn_checksum(prefix, INN_WEIGHTS_11)
        d12 = _inn_checksum(prefix + str(d11), INN_WEIGHTS_12)
        inn = prefix + str(d11) + str(d12)
    return inn


def make_ogrn(rng: random.Random, legal_entity: bool = True) -> str:
    if legal_entity:
        prefix = "1" + "".join(str(rng.randint(0, 9)) for _ in range(11))
        ogrn = prefix + str(int(prefix) % 11 % 10)
    else:
        prefix = "3" + "".join(str(rng.randint(0, 9)) for _ in range(13))
        ogrn = prefix + str(int(prefix) % 13 % 10)
    return ogrn


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def build_demo(
    db: Database,
    root: Path,
    inject_defects: bool = True,
    seed: int = 42,
    days: int = 60,
    n_clients: int = 320,
) -> dict[str, Any]:
    rng = random.Random(seed)
    now = _now()
    today = now.date()
    db.script(root / SCHEMA_PATH)

    # -- dimensions --------------------------------------------------------
    db.executemany(
        "INSERT INTO dim_product (product_code, product_name, product_family, is_credit)"
        " VALUES (?, ?, ?, ?)",
        PRODUCTS,
    )

    clients: list[tuple] = []
    for index in range(n_clients):
        client_id = f"CL{index:06d}"
        is_legal = rng.random() > 0.15
        clients.append(
            (
                client_id,
                make_inn(rng, is_legal),
                make_ogrn(rng, is_legal),
                f"ООО «{rng.choice(NAME_PARTS)}{rng.choice(NAME_PARTS)}»"
                if is_legal
                else f"ИП {rng.choice(NAME_PARTS)}-{index}",
                rng.choice(SEGMENTS),
                f"MGR{rng.randint(1, 24):03d}",
                (today - timedelta(days=rng.randint(120, 3600))).isoformat(),
                None,
            )
        )
    db.executemany(
        "INSERT INTO dim_client (client_id, inn, ogrn, client_name, segment,"
        " manager_id, opened_dt, closed_dt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        clients,
    )

    # -- accounts ----------------------------------------------------------
    accounts: list[tuple[str, str, str, float]] = []
    for client in clients:
        for _ in range(rng.randint(2, 4)):
            account = f"AC{len(accounts):08d}"
            accounts.append((account, client[0], rng.choice(CURRENCIES), rng.uniform(50_000, 20_000_000)))

    # -- daily turnover with a continuous balance (running balance) --------
    load_dt = _iso(now - timedelta(hours=2))
    dates = [today - timedelta(days=offset) for offset in range(days, 0, -1)]
    turnover: list[tuple] = []
    for account_id, client_id, currency, base in accounts:
        balance_kop = int(round(base * 100))
        for day in dates:
            delta_kop = int(round(rng.gauss(0, max(base * 0.03, 1.0)) * 100))
            new_balance_kop = max(0, balance_kop + delta_kop)
            actual_delta_kop = new_balance_kop - balance_kop
            credit_kop = int(round(rng.uniform(0, max(base * 0.05, 1.0)) * 100))
            debit_kop = credit_kop - actual_delta_kop
            if debit_kop < 0:
                debit_kop = 0
                credit_kop = max(actual_delta_kop, 0)
            turnover.append(
                (
                    account_id,
                    day.isoformat(),
                    currency,
                    client_id,
                    debit_kop / 100,
                    credit_kop / 100,
                    new_balance_kop / 100,
                    rng.choice(SRC_SYSTEMS),
                    load_dt,
                )
            )
            balance_kop = new_balance_kop
    db.executemany(
        "INSERT INTO fct_account_turnover_daily (account_id, dt, currency, client_id,"
        " debit_turnover, credit_turnover, balance_eod, src_system, load_dt)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        turnover,
    )

    # -- monthly loan portfolio (12 months) --------------------------------
    loans: list[tuple] = []
    loan_codes = [code for code, _, _, is_credit in PRODUCTS if is_credit]
    loan_load_dt = _iso(now - timedelta(hours=5))
    for client in clients:
        if rng.random() > 0.45:
            continue
        for code in rng.sample(loan_codes, k=rng.randint(1, len(loan_codes))):
            limit_amt = round(rng.uniform(1_000_000, 250_000_000), 2)
            debt = round(limit_amt * rng.uniform(0.05, 0.92), 2)
            for months_ago in range(11, -1, -1):
                month = (today.replace(day=1) - timedelta(days=months_ago * 30)).strftime("%Y-%m")
                loans.append(
                    (
                        client[0],
                        code,
                        month,
                        limit_amt,
                        round(debt * rng.uniform(0.9, 1.02), 2),
                        round(debt * rng.choice([0.0, 0.0, 0.0, 0.004, 0.02]), 2),
                        round(rng.uniform(9.5, 24.5), 2),
                        loan_load_dt,
                    )
                )
    db.executemany(
        "INSERT INTO fct_loan_portfolio_monthly (client_id, product_code, month,"
        " limit_amt, debt_amt, overdue_amt, interest_rate, load_dt)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        loans,
    )

    _refresh_batch_metadata(db, now)

    stats: dict[str, Any] = {
        "clients": len(clients),
        "accounts": len(accounts),
        "turnover_rows": len(turnover),
        "loan_rows": len(loans),
        "days": days,
        "injected_defects": [],
    }
    if inject_defects:
        stats["injected_defects"] = _inject_defects(db, rng, today, accounts)
    return stats


def _refresh_batch_metadata(db: Database, now: datetime) -> None:
    turnover_rows = int(db.scalar("SELECT COUNT(*) FROM fct_account_turnover_daily", default=0))
    loan_rows = int(db.scalar("SELECT COUNT(*) FROM fct_loan_portfolio_monthly", default=0))
    db.execute("DELETE FROM etl_batch")
    db.executemany(
        "INSERT INTO etl_batch (batch_id, dag_id, entity, load_dt, rows_loaded, status)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                "BT-0001",
                "mart_turnover_daily",
                "fct_account_turnover_daily",
                _iso(now - timedelta(hours=2)),
                turnover_rows,
                "SUCCESS",
            ),
            (
                "BT-0002",
                "mart_loan_portfolio_monthly",
                "fct_loan_portfolio_monthly",
                _iso(now - timedelta(hours=5)),
                loan_rows,
                "SUCCESS",
            ),
        ],
    )


def _inject_defects(
    db: Database, rng: random.Random, today: date, accounts: list[tuple[str, str, str, float]]
) -> list[str]:
    injected: list[str] = []

    # 1. Broken INN checksum on 3 clients (data-quality, P1).
    broken_inn = db.query("SELECT client_id, inn FROM dim_client ORDER BY client_id LIMIT 3")
    for row in broken_inn:
        inn = row["inn"]
        wrong = inn[:-1] + str((int(inn[-1]) + 1) % 10)
        db.execute(
            "UPDATE dim_client SET inn = :inn WHERE client_id = :client_id",
            {"inn": wrong, "client_id": row["client_id"]},
        )
    injected.append("3 clients with an invalid INN checksum")

    # 2. Broken OGRN checksum on 2 clients (P2).
    for row in db.query("SELECT client_id, ogrn FROM dim_client ORDER BY client_id DESC LIMIT 2"):
        ogrn = row["ogrn"]
        wrong = ogrn[:-1] + str((int(ogrn[-1]) + 1) % 10)
        db.execute(
            "UPDATE dim_client SET ogrn = :ogrn WHERE client_id = :client_id",
            {"ogrn": wrong, "client_id": row["client_id"]},
        )
    injected.append("2 clients with an invalid OGRN control digit")

    # 3. Segment completeness dropped to ~12% nulls (P2).
    db.execute(
        """
        UPDATE dim_client
        SET segment = NULL
        WHERE (CAST(SUBSTR(client_id, 3) AS INTEGER) % 8) = 0
        """
    )
    injected.append("about 12% of clients lost the segment attribute")

    # 4. Duplicate grain: same account, same day twice (P2).
    duplicate_accounts = [account[0] for account in accounts[:2]]
    last_days = [(today - timedelta(days=offset)).isoformat() for offset in (1, 2, 3)]
    for account_id in duplicate_accounts:
        for day in last_days:
            db.execute(
                """
                INSERT INTO fct_account_turnover_daily
                    (account_id, dt, currency, client_id, debit_turnover,
                     credit_turnover, balance_eod, src_system, load_dt)
                SELECT account_id, dt, currency, client_id, debit_turnover,
                       credit_turnover, balance_eod, src_system, load_dt
                FROM fct_account_turnover_daily
                WHERE account_id = :account_id AND dt = :dt
                """,
                {"account_id": account_id, "dt": day},
            )
    injected.append("duplicate (account_id, dt, currency) rows for 2 accounts x 3 days")

    # 5. Orphan client reference (P1).
    for account_id, _, currency, _ in accounts[-6:]:
        db.execute(
            """
            INSERT INTO fct_account_turnover_daily
                (account_id, dt, currency, client_id, debit_turnover,
                 credit_turnover, balance_eod, src_system, load_dt)
            SELECT account_id, dt, currency, 'CL999999', debit_turnover,
                   credit_turnover, balance_eod, 'CRM', load_dt
            FROM fct_account_turnover_daily
            WHERE account_id = :account_id AND dt = :dt
            """,
            {"account_id": account_id, "dt": (today - timedelta(days=10)).isoformat()},
        )
    injected.append(
        "6 turnover rows referencing a non-existent client CL999999 "
        "(они же дают 6 дублей гранулярности)"
    )

    # 6. Negative closing balance (P2) - also breaks balance continuity.
    negative_day = (today - timedelta(days=3)).isoformat()
    for account_id, _, _, _ in accounts[10:14]:
        db.execute(
            """
            UPDATE fct_account_turnover_daily
            SET balance_eod = -1500.00
            WHERE account_id = :account_id AND dt = :dt
            """,
            {"account_id": account_id, "dt": negative_day},
        )
    injected.append("4 accounts with a negative closing balance")

    # 7. Silent balance drift: continuity broken without a negative value (P2).
    drift_day = (today - timedelta(days=12)).isoformat()
    for account_id, _, _, _ in accounts[20:25]:
        db.execute(
            """
            UPDATE fct_account_turnover_daily
            SET balance_eod = balance_eod + 1000.00
            WHERE account_id = :account_id AND dt = :dt
            """,
            {"account_id": account_id, "dt": drift_day},
        )
    injected.append("5 rows with a silent +1000 balance drift")

    # 8. Partial load of the last day: only 40% of accounts arrived (P1).
    yesterday = (today - timedelta(days=1)).isoformat()
    db.execute(
        """
        DELETE FROM fct_account_turnover_daily
        WHERE dt = :dt
          AND SUBSTR(account_id, -1) NOT IN ('0', '1', '2', '3')
        """,
        {"dt": yesterday},
    )
    injected.append("partial load: the last day has only ~40% of accounts")

    # 9. Stale loan portfolio: last load 30 hours ago, SLA is 24h (P2).
    db.execute(
        "UPDATE fct_loan_portfolio_monthly SET load_dt = :load_dt",
        {"load_dt": _iso(_now() - timedelta(hours=30))},
    )
    injected.append("loan portfolio load is 30 hours old (SLA 24h)")

    # 10. Debt above the approved limit (P2).
    for row in db.query(
        """
        SELECT client_id, product_code, month, limit_amt
        FROM fct_loan_portfolio_monthly
        ORDER BY limit_amt DESC
        LIMIT 5
        """
    ):
        db.execute(
            """
            UPDATE fct_loan_portfolio_monthly
            SET debt_amt = :debt
            WHERE client_id = :client_id AND product_code = :product_code AND month = :month
            """,
            {
                "debt": round(float(row["limit_amt"]) * 1.4, 2),
                "client_id": row["client_id"],
                "product_code": row["product_code"],
                "month": row["month"],
            },
        )
    injected.append("5 portfolio rows with debt_amt above limit_amt")

    # 11. Batch metadata lies about the rowcount (P3, triggers reconciliation).
    actual = int(db.scalar("SELECT COUNT(*) FROM fct_account_turnover_daily", default=0))
    db.execute(
        """
        UPDATE etl_batch SET rows_loaded = :rows
        WHERE entity = 'fct_account_turnover_daily'
        """,
        {"rows": actual + 1234},
    )
    injected.append("etl_batch.rows_loaded differs from the mart rowcount by 1234")

    return injected


def describe_defects(db: Database) -> dict[str, Any]:  # pragma: no cover - diagnostics
    return {
        "clients": db.scalar("SELECT COUNT(*) FROM dim_client", default=0),
        "turnover_rows": db.scalar("SELECT COUNT(*) FROM fct_account_turnover_daily", default=0),
        "loan_rows": db.scalar("SELECT COUNT(*) FROM fct_loan_portfolio_monthly", default=0),
    }
