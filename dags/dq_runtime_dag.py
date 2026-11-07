"""DQ runtime DAG: the loop that runs every 30 minutes on the analytic mart.

Task graph::

    etl_done_sensor -> run_checks -> [export_metrics, build_report] -> notify

Why it looks like this
----------------------
* ``run_checks`` is a single Python task with the whole catalogue inside: 12
  checks cost ~2 seconds total, so splitting them into 12 parallel tasks
  creates more scheduler overhead than value. If a check becomes expensive
  (a full-table reconciliation on a 500M-row partition), it is moved to its own
  task and its own pool - see ``BACKFILL_POOL`` below.
* ``retries=2`` with exponential backoff: transient Greenplum errors are
  dominated by "segment down" and "resource queue full" - both usually clear
  within minutes.
* ``sla=timedelta(minutes=20)`` fires ``sla_miss_callback``, which is the only
  place where a P1 that never even ran (DAG stuck in the queue) gets escalated.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta

from airflow import DAG
from airflow.decorators import task
from airflow.exceptions import AirflowException, AirflowSkipException
from airflow.operators.bash import BashOperator
from airflow.operators.empty import EmptyOperator
from airflow.sensors.external_task import ExternalTaskSensor
from airflow.utils.email import send_email

sys.path.insert(0, "/opt/airflow/reliability/src")

from reliability import incidents, metrics, report, store  # noqa: E402
from reliability.checks import CheckRunner, exit_code, load_checks  # noqa: E402
from reliability.config import load_config  # noqa: E402
from reliability.db import Database  # noqa: E402

log = logging.getLogger(__name__)

CONFIG_PATH = "/opt/airflow/reliability/config/greenplum.json"
ALERT_RECIPIENTS = ["data-reliability@bank.local"]
P1_SEVERITIES = {"P1"}

default_args = {
    "owner": "data-reliability",
    "depends_on_past": False,
    "email": ALERT_RECIPIENTS,
    "email_on_failure": True,
    "email_on_retry": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=3),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=20),
}


def _on_sla_miss(dag, dag_run, tasks, blocking_tis):  # pragma: no cover - Airflow hook
    """DQ, которое не запустилось, - тоже инцидент, но другого класса."""
    body = (
        f"DQ-прогон {dag.dag_id} не уложился в SLA.\n"
        f"Run: {dag_run.run_id}\n"
        f"Заблокированные таски: {', '.join(t.task_id for t in tasks)}\n"
        f"Проверить: `python run.py status` и очередь ресурсов Greenplum."
    )
    send_email(to=ALERT_RECIPIENTS, subject=f"[DQ SLA MISS] {dag.dag_id}", html_content=body)


def _runtime_params(context: dict, config) -> dict:
    run_date = datetime.fromisoformat(context["ds"])
    return {
        "run_ts": context["ts"],
        "run_date": context["ds"],
        "window_start": (run_date - timedelta(days=config.window_days)).date().isoformat(),
        "window_days": config.window_days,
        **config.params,
    }


def _results_from_store(db: Database, run_id: str, owner: str) -> list:
    from reliability.checks import CheckResult  # local import: keep DAG parse light

    return [
        CheckResult(
            check_id=row["check_id"],
            title=row["check_id"],
            entity=row["entity"],
            severity=row["severity"],
            owner=owner,
            status=row["status"],
            metric_value=row["metric_value"],
            unit=row["unit"] or "count",
            bounds={},
            message=row["message"] or "",
            rows_examined=row["rows_examined"],
            duration_ms=row["duration_ms"] or 0,
        )
        for row in store.results_for_run(db, run_id)
    ]


def _open_incident_counts(db: Database) -> dict[str, int]:
    counts: dict[str, int] = {}
    for incident in incidents.open_incidents(db):
        counts[incident["severity"]] = counts.get(incident["severity"], 0) + 1
    return counts


def _summary(run_id: str, results, events, payload, config) -> dict:
    return {
        "run_id": run_id,
        "exit_code": exit_code(results, config.fail_on),
        "failed": [r.check_id for r in results if r.failed],
        "p1_failed": [r.check_id for r in results if r.failed and r.severity in P1_SEVERITIES],
        "events": len(events),
        "notify": payload,
        "results": [r.as_dict() for r in results],
    }


def _log_summary(summary: dict) -> None:
    log.info("DQ run %s: %s", summary["run_id"], json.dumps(summary["results"], ensure_ascii=False))
    if summary["exit_code"] >= 1:
        log.warning("P1 breach: %s", summary["p1_failed"])


def _report_context(db: Database, config, run_id: str) -> report.ReportContext:
    return report.ReportContext(
        env=config.env,
        run_id=run_id,
        run_ts=datetime.now(),
        results=_results_from_store(db, run_id, config.owner),
        incidents=incidents.open_incidents(db),
        mttr=incidents.mttr(db),
        sla_breaches=incidents.sla_breaches(db),
        history=store.runs_history(db),
    )


with DAG(
    dag_id="dq_runtime_mart",
    description="DQ-проверки mart-слоя аналитического хранилища + синхронизация инцидентов",
    schedule="*/30 * * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    sla_miss_callback=_on_sla_miss,
    tags=["dq", "reliability", "mart"],
    doc_md=__doc__,
) as dag:
    wait_for_mart_load = ExternalTaskSensor(
        task_id="wait_for_mart_load",
        external_dag_id="mart_account_turnover_daily",
        external_task_id="publish_mart",
        allowed_states=["success"],
        failed_states=["failed", "skipped"],
        mode="reschedule",
        poke_interval=120,
        timeout=60 * 60 * 3,
    )

    @task(task_id="run_checks", sla=timedelta(minutes=20))
    def run_checks(**context) -> dict:
        config = load_config(CONFIG_PATH)
        run_id = f"RUN-{context['ts_nodash']}"
        with Database(config.database_path) as db:
            store.ensure_schema(db)
            specs = load_checks(config.checks_path)
            runner = CheckRunner(db, specs, _runtime_params(context, config))
            results = runner.run()
            store.save_results(db, run_id, context["ts"], results)
            events = incidents.sync_incidents(db, run_id, results, config.owner)
            payload = incidents.notification_payload(events, run_id, config.notify)
            summary = _summary(run_id, results, events, payload, config)
        _log_summary(summary)
        return summary

    @task(task_id="export_metrics", trigger_rule="all_done")
    def export_metrics(summary: dict) -> str:
        config = load_config(CONFIG_PATH)
        with Database(config.database_path) as db:
            store.ensure_schema(db)
            results = _results_from_store(db, summary["run_id"], config.owner)
            open_counts = _open_incident_counts(db)

        content = metrics.render(results, open_incidents=open_counts)
        path = metrics.write(config.metrics_path, content)
        return str(path)

    @task(task_id="build_report")
    def build_report(summary: dict) -> str:
        config = load_config(CONFIG_PATH)
        with Database(config.database_path) as db:
            store.ensure_schema(db)
            context = _report_context(db, config, summary["run_id"])
        path = report.write(config.report_path, report.render(context))
        return str(path)

    @task(task_id="notify_team", trigger_rule="all_done")
    def notify_team(summary: dict) -> None:
        """Открытые P1 уходят в чат дежурной смены, остальное - только в отчёт."""
        payload = summary["notify"]
        if not payload["events"]:
            raise AirflowSkipException("no incident events in this run")
        if not summary["p1_failed"]:
            log.info("P2/P3 events only, chat notification suppressed")
            return
        log.warning("P1 events -> %s", payload["channel"])
        log.info(json.dumps(payload, ensure_ascii=False, indent=2))
        # Прод-реализация: HTTP-хук в корпоративный мессенджер + создание тикета
        # в Jira (project=DQ) с дедупликацией по incident_id.

    @task(task_id="check_sla_breaches", trigger_rule="all_done")
    def check_sla_breaches() -> list[str]:
        config = load_config(CONFIG_PATH)
        with Database(config.database_path) as db:
            store.ensure_schema(db)
            breaches = incidents.sla_breaches(db)
        if breaches:
            log.error("SLA реакции нарушен: %s", breaches)
        return [b["incident_id"] for b in breaches]

    @task(trigger_rule="all_done")
    def fail_run_if_p1(summary: dict) -> None:
        """Красный таск в Airflow = сигнал, который видят все, а не только DQ-дашборд."""
        if summary["exit_code"] >= 1:
            raise AirflowException(
                f"P1 DQ breach in {summary['run_id']}: {', '.join(summary['p1_failed'])}"
            )

    rotate_artifacts = BashOperator(
        task_id="rotate_artifacts",
        bash_command="find /opt/airflow/reliability/artifacts -name 'dq_report_*.md' -mtime +30 -delete",
    )

    done = EmptyOperator(task_id="done")

    checked = run_checks()
    breach = check_sla_breaches()
    wait_for_mart_load >> checked
    checked >> [export_metrics(checked), build_report(checked), breach]
    checked >> notify_team(checked)
    checked >> fail_run_if_p1(checked)
    [export_metrics(checked), build_report(checked), breach] >> rotate_artifacts >> done
