"""Command line interface.

    python run.py seed               # build the demo warehouse with injected failures
    python run.py seed --clean       # rebuild it cleanly (the recovery step)
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
from typing import Any, Mapping, Sequence

from . import incidents, jira, metrics, report, store
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


def _report_context(
    config: Config, db: Database, run_id: str, run_ts: datetime, results: Sequence[CheckResult],
    events: Sequence[dict[str, Any]],
) -> report.ReportContext:
    return report.ReportContext(
        env=config.env,
        run_id=run_id,
        run_ts=run_ts,
        results=results,
        incidents=incidents.open_incidents(db),
        events=events,
        mttr=incidents.mttr(db),
        sla_breaches=incidents.sla_breaches(db),
        history=store.runs_history(db),
    )


def _write_artifacts(
    config: Config,
    db: Database,
    run_id: str,
    run_ts: datetime,
    results: Sequence[CheckResult],
    events: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    context = _report_context(config, db, run_id, run_ts, results, events)
    report_path = report.write(config.report_path, report.render(context))
    metrics_path = metrics.write(
        config.metrics_path,
        metrics.render(results, run_ts, _open_counts(db)),
    )
    payload = incidents.notification_payload(events, run_id, config.notify)
    notifications_path = _write_notifications(config, payload)
    return {
        "report_path": report_path,
        "metrics_path": metrics_path,
        "notifications_path": notifications_path,
        "payload": payload,
    }


def _announce(run_id: str, artifacts: Mapping[str, Any], results: Sequence[CheckResult]) -> None:
    _print_results(results)
    summary = artifacts["payload"]["summary"]
    print(f"\nrun_id={run_id}  инцидентов открыто: {summary['opened']}, автозакрыто: {summary['resolved']}")
    print(f"отчёт: {artifacts['report_path']}")
    print(f"метрики: {artifacts['metrics_path']}")
    print(f"уведомления: {artifacts['notifications_path']}")


def execute_run(config: Config, db: Database, verbose: bool = True) -> dict[str, Any]:
    """Run every check, persist results, sync incidents, build artifacts."""
    specs = load_checks(config.checks_path)
    run_ts = datetime.now(timezone.utc)
    run_id = f"RUN-{run_ts.strftime('%Y%m%dT%H%M%S%f')[:-3]}"

    runner = CheckRunner(db, specs, _runtime_params(config, run_ts))
    results: list[CheckResult] = runner.run()

    store.save_results(db, run_id, run_ts.isoformat(timespec="seconds"), results)
    events = incidents.sync_incidents(db, run_id, results, config.owner)
    artifacts = _write_artifacts(config, db, run_id, run_ts, results, events)

    if verbose:
        _announce(run_id, artifacts, results)

    return {
        "run_id": run_id,
        "results": results,
        "events": events,
        "report_path": artifacts["report_path"],
        "metrics_path": artifacts["metrics_path"],
        "payload": artifacts["payload"],
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


def _print_incident_table(open_list: Sequence[Mapping[str, Any]]) -> None:
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


def _print_mttr(stats: Mapping[str, Any]) -> None:
    overall = stats["overall"]
    if overall["resolved"]:
        print(
            f"\nMTTR: {overall['avg_minutes']} мин по {overall['resolved']} закрытым "
            f"(максимум {overall['max_minutes']} мин)"
        )
    for severity, item in stats["by_severity"].items():
        print(f"  {severity}: {item['avg_minutes']} мин (закрыто {item['resolved']})")


def _print_breaches(breaches: Sequence[Mapping[str, Any]]) -> None:
    if breaches:
        print("\nнарушение SLA реакции:")
        for breach in breaches:
            print(
                f"  {breach['incident_id']} ({breach['severity']}): "
                f"{breach['age_minutes']} мин > {breach['budget_minutes']} мин"
            )


def _print_history(history: Sequence[Mapping[str, Any]]) -> None:
    if history:
        print("\nпоследние прогоны:")
        for run in history:
            print(
                f"  {run['run_ts']}  {run['run_id']}  checks={run['checks']} "
                f"fail={run['failed']} warn={run['warned']} error={run['errored']}"
            )


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    with Database(config.database_path) as db:
        store.ensure_schema(db)
        open_list = incidents.open_incidents(db)
        stats = incidents.mttr(db)
        breaches = incidents.sla_breaches(db)
        history = store.runs_history(db, limit=5)

    _print_incident_table(open_list)
    _print_mttr(stats)
    _print_breaches(breaches)
    _print_history(history)
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


def _step(title: str, blank: bool = True) -> None:
    print(("\n" if blank else "") + "=" * 78)
    print(title)
    print("=" * 78)


def _demo_broken_run(config: Config) -> tuple[dict[str, Any], Path, int]:
    """Step 2: run the catalogue over defective data, keep the artifact and exit code."""
    _step("ШАГ 2. Прогон каталога проверок: часть проверок падает, открываются инциденты")
    with Database(config.database_path) as db:
        store.ensure_schema(db)
        broken = execute_run(config, db)

    incident_report = config.root / "artifacts" / "dq_report_incident.md"
    incident_report.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(broken["report_path"], incident_report)
    return broken, incident_report, exit_code(broken["results"], config.fail_on)


def _demo_healthy_run(config: Config) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    """Step 3: reload clean data, run again, return the remaining incident state."""
    _step("ШАГ 3. Перезаливаем данные без дефектов и повторяем прогон")
    _seed(config, defects=False)
    with Database(config.database_path) as db:
        store.ensure_schema(db)
        healthy = execute_run(config, db)
        stats_after = incidents.mttr(db)
        remaining = incidents.open_incidents(db)

    resolved_report = config.root / "artifacts" / "dq_report_resolved.md"
    shutil.copyfile(healthy["report_path"], resolved_report)
    return resolved_report, remaining, stats_after


def _failed_count(results: Sequence[CheckResult]) -> int:
    """How many checks ended in FAIL or ERROR."""
    return sum(1 for result in results if result.failed)


def _print_demo_summary(
    code: int,
    results: Sequence[CheckResult],
    remaining: Sequence[Mapping[str, Any]],
    stats_after: Mapping[str, Any],
    incident_report: Path,
    resolved_report: Path,
) -> None:
    print(f"  прогон с дефектами: {_failed_count(results)} проверок с FAIL/ERROR, exit code {code}")
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


def cmd_demo(config: Config, args: argparse.Namespace) -> int:
    """End-to-end story: injected failures -> incident -> clean reload -> MTTR."""
    _step("ШАГ 1. Собираем демо-хранилище с внесёнными дефектами", blank=False)
    stats = _seed(config, defects=True)
    for item in stats["injected_defects"]:
        print(f"  - {item}")

    broken, incident_report, code = _demo_broken_run(config)
    resolved_report, remaining, stats_after = _demo_healthy_run(config)

    _step("ИТОГ")
    _print_demo_summary(
        code, broken["results"], remaining, stats_after, incident_report, resolved_report
    )
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


def _print_jira(results: Sequence[Mapping[str, Any]], settings: jira.JiraConfig) -> None:
    mode = settings.base_url if settings.enabled else "dry-run"
    print(f"Jira: инцидентов {len(results)}, режим {mode}")
    for item in results:
        print(
            f"  {item['incident_id']:<12} {item['action']:<9} "
            f"{item.get('key') or '-':<8} {item['label']}"
        )
    if not settings.enabled:
        print("подсказка: задайте notify.jira_url в конфиге или --url, чтобы отправить реально")


def cmd_jira(config: Config, args: argparse.Namespace) -> int:
    """Отправить открытые инциденты в Jira; без URL — dry-run с планом задач."""
    with Database(config.database_path) as db:
        store.ensure_schema(db)
        open_list = incidents.open_incidents(db)

    notify = dict(config.notify)
    if args.url:
        notify["jira_url"] = args.url
    settings = jira.JiraConfig.from_notify(notify)
    if getattr(args, "project", ""):
        settings = jira.JiraConfig(**{**settings.__dict__, "project": args.project})
    events = jira.events_from_incidents(open_list)

    code = 0
    try:
        results = jira.push_events(events, args.run_id, settings)
        _print_jira(results, settings)
    except jira.JiraError as exc:
        print(f"ошибка Jira: {exc}", file=sys.stderr)
        code = 2
    return code


def _add_simple_parser(
    sub: argparse._SubParsersAction, name: str, help_text: str, func: Any
) -> argparse.ArgumentParser:
    parser = sub.add_parser(name, help=help_text)
    parser.set_defaults(func=func)
    return parser


def _add_seed_parser(sub: argparse._SubParsersAction) -> None:
    seed = sub.add_parser("seed", help="пересобрать демо-хранилище")
    seed.add_argument("--clean", action="store_true", help="без внесения дефектов")
    seed.set_defaults(func=cmd_seed)


def _add_run_parser(sub: argparse._SubParsersAction) -> None:
    run = sub.add_parser("run", help="выполнить каталог проверок")
    run.add_argument(
        "--fail-on",
        choices=["P1", "P2", "P3", "none"],
        help="минимальная severity для ненулевого exit code",
    )
    run.set_defaults(func=cmd_run)


def _add_incident_parser(
    sub: argparse._SubParsersAction, name: str, help_text: str, func: Any
) -> None:
    parser = sub.add_parser(name, help=help_text)
    parser.add_argument("incident_id")
    parser.add_argument("--note", default="")
    parser.set_defaults(func=func)


def _add_jira_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("jira", help="отправить открытые инциденты в Jira")
    parser.add_argument(
        "--url",
        default="",
        help="базовый URL Jira (пусто — dry-run из notify.jira_url)",
    )
    parser.add_argument("--project", default="", help="ключ проекта, по умолчанию из конфига")
    parser.add_argument("--run-id", default="MANUAL", help="метка прогона в labels")
    parser.set_defaults(func=cmd_jira)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reliability",
        description="Data Reliability / Data Quality framework (Greenplum, Airflow, Python)",
    )
    parser.add_argument("--config", default="config/demo.json", help="путь к JSON-конфигу")
    sub = parser.add_subparsers(dest="command", required=True)

    _add_simple_parser(sub, "init", "создать ops-таблицы", cmd_init)
    _add_seed_parser(sub)
    _add_run_parser(sub)
    _add_simple_parser(sub, "status", "открытые инциденты, MTTR, SLA", cmd_status)
    _add_incident_parser(sub, "ack", "взять инцидент в работу", cmd_ack)
    _add_incident_parser(sub, "resolve", "закрыть инцидент вручную", cmd_resolve)
    _add_simple_parser(sub, "report", "показать последний прогон", cmd_report)
    _add_jira_parser(sub)
    _add_simple_parser(
        sub, "demo", "полный сценарий: дефекты -> инцидент -> восстановление", cmd_demo
    )
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
