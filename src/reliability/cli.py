"""Command line interface.

    python run.py seed --defects     # build the demo warehouse with injected failures
    python run.py run                # run the DQ catalogue, sync incidents, export metrics
    python run.py status             # open incidents, MTTR, SLA breaches
    python run.py demo               # full story: incident -> fix -> auto-resolve -> MTTR
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from . import incidents, metrics, report, store
from .checks import CheckResult, CheckRunner, exit_code, load_checks
from .config import Config, load_config
from .db import Database
from .synth import build_demo

SEVERITY_RANK = {"P1": 0, "P2": 1, "P3": 2}
STATUS_MARK = {"PASS": "OK  ", "WARN": "WARN", "FAIL": "FAIL", "ERROR": "ERR "}


def _runtime_params(config: Config, run_ts: datetime) -> dict[str, Any]:
    window_start = (run_ts.date() - timedelta(days=config.window_days)).isoformat()
    return {
        "run_ts": run_ts.isoformat(timespec="seconds"),
        "run_date": run_ts.date().isoformat(),
        "window_start": window_start,
        "window_days": config.window_days,
        "sla_turnover_hours": config.sla_for("fct_account_turnover_daily", 26.0),
        "sla_loans_hours": config.sla_for("fct_loan_portfolio_monthly", 24.0),
        **config.params,
    }


def _open_counts(db: Database) -> dict[str, int]:
    rows = db.query(
        """
        SELECT severity, COUNT(*) AS total
        FROM ops_dq_incident
        WHERE status <> 'RESOLVED'
        GROUP BY severity
        """
    )
    return {row["severity"]: int(row["total"]) for row in rows}


def _write_notifications(config: Config, payload: dict[str, Any]) -> Path:
    target = config.root / "artifacts" / "notifications.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return target


def execute_run(config: Config, db: Database, verbose: bool = True) -> dict[str, Any]:
    """Run every check, persist results, sync incidents, build artifacts."""
    specs = load_checks(config.checks_path)
    run_ts = datetime.now(timezone.utc)
    run_id = f"RUN-{run_ts.strftime('%Y%m%dT%H%M%S%f')[:-3]}"

    runner = CheckRunner(db, specs, _runtime_params(config, run_ts))
    results: list[CheckResult] = runner.run()

    store.save_results(db, run_id, run_ts.isoformat(timespec="seconds"), results)
    events = incidents.sync_incidents(db, run_id, results, config.owner)

    open_incidents = incidents.open_incidents(db)
    context = report.ReportContext(
        env=config.env,
        run_id=run_id,
        run_ts=run_ts,
        results=results,
        incidents=open_incidents,
        events=events,
        mttr=incidents.mttr(db),
        sla_breaches=incidents.sla_breaches(db),
        history=store.runs_history(db),
    )
    report_path = report.write(config.report_path, report.render(context))
    metrics_path = metrics.write(
        config.metrics_path,
        metrics.render(results, run_ts, _open_counts(db)),
    )
    payload = incidents.notification_payload(events, run_id, config.notify)
    notifications_path = _write_notifications(config, payload)

    if verbose:
        _print_results(results)
        opened = payload["summary"]["opened"]
        resolved = payload["summary"]["resolved"]
        print(f"\nrun_id={run_id}  инцидентов открыто: {opened}, автозакрыто: {resolved}")
        print(f"отчёт: {report_path}")
        print(f"метрики: {metrics_path}")
        print(f"уведомления: {notifications_path}")

    return {
        "run_id": run_id,
        "results": results,
        "events": events,
        "report_path": report_path,
        "metrics_path": metrics_path,
        "payload": payload,
    }


def _print_results(results: Sequence[CheckResult]) -> None:
    width = max((len(r.check_id) for r in results), default=10)
    for result in sorted(results, key=lambda r: (SEVERITY_RANK.get(r.severity, 9), r.check_id)):
        value = "-" if result.metric_value is None else f"{result.metric_value:g}"
        print(
            f"{STATUS_MARK.get(result.status, result.status)} "
            f"{result.severity} {result.check_id:<{width}} "
            f"{result.unit:<8} {value:<12} {result.entity:<30} {result.message[:70]}"
        )


def _seed(config: Config, defects: bool) -> dict[str, Any]:
    with Database(config.database_path) as db:
        store.ensure_schema(db)
        stats = build_demo(db, config.root, inject_defects=defects)
    return stats


def cmd_init(config: Config, args: argparse.Namespace) -> int:
    with Database(config.database_path) as db:
        store.ensure_schema(db)
    print(f"ops-схема готова: {config.database_path}")
    return 0


def cmd_seed(config: Config, args: argparse.Namespace) -> int:
    stats = _seed(config, defects=not args.clean)
    print(
        f"демо-хранилище пересобрано: клиентов {stats['clients']}, "
        f"счетов {stats['accounts']}, строк оборотов {stats['turnover_rows']}, "
        f"строк портфеля {stats['loan_rows']}"
    )
    if stats["injected_defects"]:
        print("внесены дефекты:")
        for item in stats["injected_defects"]:
            print(f"  - {item}")
    else:
        print("дефекты не вносились (--clean)")
    return 0


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    with Database(config.database_path) as db:
        store.ensure_schema(db)
        outcome = execute_run(config, db)
    if args.fail_on:
        config = Config(**{**config.__dict__, "fail_on": args.fail_on})
    return exit_code(outcome["results"], config.fail_on)


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    with Database(config.database_path) as db:
        store.ensure_schema(db)
        open_list = incidents.open_incidents(db)
        stats = incidents.mttr(db)
        breaches = incidents.sla_breaches(db)
        history = store.runs_history(db, limit=5)

    if not open_list:
        print("открытых инцидентов нет")
    else:
        print(f"открытых инцидентов: {len(open_list)}")
        print("| ID | Sev | Статус | Проверка | Открыт (UTC) | Повторов |")
        print("| --- | --- | --- | --- | --- | --- |")
        for incident in open_list:
            print(
                f"| {incident['incident_id']} | {incident['severity']} | "
                f"{incident['status']} | {incident['check_id']} | "
                f"{incident['opened_at']} | {incident['occurrences']} |"
            )

    overall = stats["overall"]
    if overall["resolved"]:
        print(
            f"\nMTTR: {overall['avg_minutes']} мин по {overall['resolved']} закрытым "
            f"(максимум {overall['max_minutes']} мин)"
        )
    for severity, item in stats["by_severity"].items():
        print(f"  {severity}: {item['avg_minutes']} мин (закрыто {item['resolved']})")

    if breaches:
        print("\nнарушение SLA реакции:")
        for breach in breaches:
            print(
                f"  {breach['incident_id']} ({breach['severity']}): "
                f"{breach['age_minutes']} мин > {breach['budget_minutes']} мин"
            )

    if history:
        print("\nпоследние прогоны:")
        for run in history:
            print(
                f"  {run['run_ts']}  {run['run_id']}  checks={run['checks']} "
                f"fail={run['failed']} warn={run['warned']} error={run['errored']}"
            )
    return 0


def cmd_ack(config: Config, args: argparse.Namespace) -> int:
    with Database(config.database_path) as db:
        store.ensure_schema(db)
        incident = incidents.ack(db, args.incident_id, note=args.note or "")
    print(f"{incident['incident_id']} переведён в ACK")
    return 0


def cmd_resolve(config: Config, args: argparse.Namespace) -> int:
    with Database(config.database_path) as db:
        store.ensure_schema(db)
        incident = incidents.resolve(db, args.incident_id, note=args.note or "")
    print(f"{incident['incident_id']} закрыт: {args.note or 'resolved manually'}")
    return 0


def cmd_demo(config: Config, args: argparse.Namespace) -> int:
    """End-to-end story: injected failures -> incident -> clean reload -> MTTR."""
    print("=" * 78)
    print("ШАГ 1. Собираем демо-хранилище с внесёнными дефектами")
    print("=" * 78)
    stats = _seed(config, defects=True)
    for item in stats["injected_defects"]:
        print(f"  - {item}")

    print("\n" + "=" * 78)
    print("ШАГ 2. Прогон каталога проверок: часть проверок падает, открываются инциденты")
    print("=" * 78)
    with Database(config.database_path) as db:
        store.ensure_schema(db)
        broken = execute_run(config, db)

    incident_report = config.root / "artifacts" / "dq_report_incident.md"
    incident_report.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(broken["report_path"], incident_report)
    code = exit_code(broken["results"], config.fail_on)

    print("\n" + "=" * 78)
    print("ШАГ 3. Перезаливаем данные без дефектов и повторяем прогон")
    print("=" * 78)
    _seed(config, defects=False)
    with Database(config.database_path) as db:
        store.ensure_schema(db)
        healthy = execute_run(config, db)
        stats_after = incidents.mttr(db)
        remaining = incidents.open_incidents(db)

    resolved_report = config.root / "artifacts" / "dq_report_resolved.md"
    shutil.copyfile(healthy["report_path"], resolved_report)

    print("\n" + "=" * 78)
    print("ИТОГ")
    print("=" * 78)
    print(f"  прогон с дефектами: {code} проверок с FAIL/ERROR, exit code {code}")
    print(f"  открытых инцидентов после восстановления: {len(remaining)}")
    overall = stats_after["overall"]
    if overall["resolved"]:
        print(
            f"  MTTR: {overall['avg_minutes']} мин по {overall['resolved']} закрытым инцидентам"
        )
    for severity, item in stats_after["by_severity"].items():
        print(f"    {severity}: {item['avg_minutes']} мин ({item['resolved']} шт.)")
    print(f"  отчёт инцидента: {incident_report}")
    print(f"  отчёт после восстановления: {resolved_report}")
    return 0


def cmd_report(config: Config, args: argparse.Namespace) -> int:
    with Database(config.database_path) as db:
        store.ensure_schema(db)
        run_id = store.latest_run_id(db)
        rows = store.results_for_run(db, run_id) if run_id else []
    if not run_id:
        print("нет ни одного прогона — выполните `run`")
        code = 1
    else:
        print(f"последний прогон {run_id}: {len(rows)} проверок, отчёт {config.report_path}")
        for row in rows:
            if row["status"] in ("FAIL", "ERROR"):
                print(f"  {STATUS_MARK[row['status']]} {row['severity']} {row['check_id']}: {row['message']}")
        code = 0
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reliability",
        description="Data Reliability / Data Quality framework (Greenplum, Airflow, Python)",
    )
    parser.add_argument("--config", default="config/demo.json", help="путь к JSON-конфигу")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="создать ops-таблицы").set_defaults(func=cmd_init)

    seed = sub.add_parser("seed", help="пересобрать демо-хранилище")
    seed.add_argument("--clean", action="store_true", help="без внесения дефектов")
    seed.set_defaults(func=cmd_seed)

    run = sub.add_parser("run", help="выполнить каталог проверок")
    run.add_argument("--fail-on", choices=["P1", "P2", "P3", "none"], help="минимальная severity для ненулевого exit code")
    run.set_defaults(func=cmd_run)

    sub.add_parser("status", help="открытые инциденты, MTTR, SLA").set_defaults(func=cmd_status)

    ack = sub.add_parser("ack", help="взять инцидент в работу")
    ack.add_argument("incident_id")
    ack.add_argument("--note", default="")
    ack.set_defaults(func=cmd_ack)

    resolve = sub.add_parser("resolve", help="закрыть инцидент вручную")
    resolve.add_argument("incident_id")
    resolve.add_argument("--note", default="")
    resolve.set_defaults(func=cmd_resolve)

    sub.add_parser("report", help="показать последний прогон").set_defaults(func=cmd_report)

    sub.add_parser("demo", help="полный сценарий: дефекты -> инцидент -> восстановление").set_defaults(func=cmd_demo)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    try:
        code = int(args.func(config, args))
    except (KeyError, ValueError) as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        code = 2
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
