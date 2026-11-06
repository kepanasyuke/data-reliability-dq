"""SLA-контур хранилища: свежесть, объём и время загрузки в разрезе суток.

Отличие от ``dq_runtime_mart``: тот даг отвечает на вопрос "данные корректны
сейчас?", этот - "укладывается ли конвейер в обязательства перед
потребителями?" (банковские дашборды открываются в 09:00, значит mart обязан
быть готов к 08:30). Проверки здесь - чистые SQL-запросы к ops-слою, без
Python-раннера, потому что результат нужен в виде SLI для SLO-отчёта.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from airflow import DAG
from airflow.decorators import task
from airflow.providers.common.sql.operators.sql import SQLCheckOperator, SQLValueCheckOperator
from airflow.utils.email import send_email

log = logging.getLogger(__name__)

GP_CONN_ID = "greenplum_mart"
ALERT_RECIPIENTS = ["data-reliability@bank.local", "bi-oncall@bank.local"]

# SLO: 99.5% суток в месяце, когда mart готов к 08:30.
DELIVERY_DEADLINE = "08:30"
FRESHNESS_SLA_HOURS = {"fct_account_turnover_daily": 26, "fct_loan_portfolio_monthly": 24}

default_args = {
    "owner": "data-reliability",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
    "email": ALERT_RECIPIENTS,
}


def _alert_freshness(context) -> str:  # pragma: no cover - Airflow hook
    ti = context["ti"]
    failed = ti.xcom_pull(task_ids="freshness_by_entity", key="return_value") or []
    body = (
        f"Нарушен SLA свежести mart-слоя.\n"
        f"Дата: {context['ds']}\n"
        f"Сущности вне SLA: {failed}\n"
        f"Проверить: ops.etl_batch, очередь ресурсов и статус сегментов.\n"
        f"Runbook: docs/incident-runbook.md#p1-freshness"
    )
    send_email(to=ALERT_RECIPIENTS, subject="[DQ][P1] mart freshness SLA breach", html_content=body)
    return body


with DAG(
    dag_id="mart_delivery_sla",
    description="SLA-контроль готовности mart-слоя к 08:30 и свежести загрузок",
    schedule="0 8 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=default_args,
    tags=["dq", "sla", "slo"],
    doc_md=__doc__,
) as dag:
    mart_ready = SQLCheckOperator(
        task_id="mart_ready_before_deadline",
        conn_id=GP_CONN_ID,
        sql="""
            SELECT CASE
                       WHEN COUNT(*) = 0 THEN 1
                       ELSE 0
                   END
              FROM ops.etl_batch
             WHERE (entity = 'fct_account_turnover_daily'
                    AND load_dt::date = current_date
                    AND status = 'SUCCESS'
                    AND load_dt::time <= TIME '08:30') IS NOT TRUE
        """,
        on_failure_callback=_alert_freshness,
    )

    freshness_turnover = SQLValueCheckOperator(
        task_id="freshness_turnover_within_sla",
        conn_id=GP_CONN_ID,
        sql="""
            SELECT ops.dq_age_hours(MAX(load_dt))
              FROM ops.etl_batch
             WHERE entity = 'fct_account_turnover_daily'
               AND status = 'SUCCESS'
        """,
        pass_value=FRESHNESS_SLA_HOURS["fct_account_turnover_daily"],
        tolerance={"max": 0},
        on_failure_callback=_alert_freshness,
    )

    freshness_loans = SQLValueCheckOperator(
        task_id="freshness_loans_within_sla",
        conn_id=GP_CONN_ID,
        sql="""
            SELECT ops.dq_age_hours(MAX(load_dt))
              FROM ops.etl_batch
             WHERE entity = 'fct_loan_portfolio_monthly'
               AND status = 'SUCCESS'
        """,
        pass_value=FRESHNESS_SLA_HOURS["fct_loan_portfolio_monthly"],
        tolerance={"max": 0},
        on_failure_callback=_alert_freshness,
    )

    volume_floor = SQLValueCheckOperator(
        task_id="volume_floor_48h",
        conn_id=GP_CONN_ID,
        sql="""
            WITH daily AS (
                SELECT dt, COUNT(*) AS cnt
                  FROM mart.fct_account_turnover_daily
                 WHERE dt >= current_date - 2
                 GROUP BY dt
            )
            SELECT MIN(cnt) FROM daily
        """,
        pass_value=10000,
        tolerance={"min_pct": 20},
        on_failure_callback=_alert_freshness,
    )

    @task(task_id="publish_slo_metrics")
    def publish_slo_metrics() -> None:
        """SLI: доля суток месяца, когда mart был готов до дедлайна."""
        log.info(
            "SLI delivery = успешные дни / календарные дни месяца; "
            "целевой SLO 99.5%%, бюджет ошибки ~2 часа в месяц"
        )
        # Прод-реализация: INSERT в ops.slo_daily + экспорт в Prometheus,
        # панель "Mart availability" в общем дашборде надёжности.

    [mart_ready, freshness_turnover, freshness_loans, volume_floor] >> publish_slo_metrics()
