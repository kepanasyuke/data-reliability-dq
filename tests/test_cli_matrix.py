"""CLI: разбор аргументов, коды возврата, тексты для дежурной смены."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reliability import incidents, store
from reliability.checks import FAIL, CheckResult
from reliability.cli import build_parser, cmd_ack, cmd_init, cmd_report, cmd_resolve, main
from reliability.db import Database

REPO_ROOT = Path(__file__).resolve().parents[1]
SUBCOMMANDS = ["init", "seed", "run", "status", "ack", "resolve", "report", "demo"]
INVOCATIONS = [
    ["init"],
    ["seed"],
    ["run"],
    ["status"],
    ["ack", "DQ-00001"],
    ["resolve", "DQ-00001"],
    ["report"],
    ["demo"],
]


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config" / "demo.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "env": "cli-test",
        "dsn": "artifacts/warehouse.sqlite3",
        "checks": str(REPO_ROOT / "checks" / "dq_checks.json"),
        "window_days": 30,
        "fail_on": "P1",
        "owner": "cli@example.org",
        "artifacts": {
            "metrics": "artifacts/metrics/dq.prom",
            "report": "artifacts/dq_report.md",
        },
        "notify": {"channel": "#cli"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _result(status: str = FAIL) -> CheckResult:
    return CheckResult(
        check_id="cli.check",
        title="cli",
        entity="dim_client",
        severity="P1",
        owner="cli@example.org",
        status=status,
        metric_value=1.0,
        unit="count",
        bounds={"fail": {"max": 0}},
        message="count: 1",
    )


@pytest.mark.parametrize("argv", INVOCATIONS, ids=[item[0] for item in INVOCATIONS])
def test_subcommand_is_parsed(argv: list[str]) -> None:
    args = build_parser().parse_args(argv)
    assert args.command == argv[0]
    assert callable(args.func)


@pytest.mark.parametrize("name", SUBCOMMANDS)
def test_subcommand_has_help(name: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([name, "--help"])


def test_config_option_default() -> None:
    assert build_parser().parse_args(["init"]).config == "config/demo.json"


def test_config_option_override() -> None:
    args = build_parser().parse_args(["--config", "custom.json", "init"])
    assert args.config == "custom.json"


@pytest.mark.parametrize("value", ["P1", "P2", "P3", "none"])
def test_run_fail_on_choices(value: str) -> None:
    assert build_parser().parse_args(["run", "--fail-on", value]).fail_on == value


def test_run_fail_on_rejects_unknown() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--fail-on", "P9"])


def test_seed_clean_flag() -> None:
    assert build_parser().parse_args(["seed", "--clean"]).clean is True


def test_seed_default_is_defective() -> None:
    assert build_parser().parse_args(["seed"]).clean is False


@pytest.mark.parametrize("name", ["ack", "resolve"])
def test_incident_commands_require_id(name: str) -> None:
    args = build_parser().parse_args([name, "DQ-00001", "--note", "note"])
    assert args.incident_id == "DQ-00001"
    assert args.note == "note"


def test_incident_command_requires_argument() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["ack"])


def test_unknown_command_exits() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["nope"])


def test_missing_command_exits() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_main_init_creates_schema(config_file: Path) -> None:
    assert main(["--config", str(config_file), "init"]) == 0
    db_path = config_file.parent.parent / "artifacts" / "warehouse.sqlite3"
    assert db_path.exists()


def test_main_report_without_runs_returns_one(config_file: Path, capsys) -> None:
    assert main(["--config", str(config_file), "report"]) == 1
    assert "нет ни одного прогона" in capsys.readouterr().out


def test_main_report_lists_failures(config_file: Path, capsys) -> None:
    main(["--config", str(config_file), "init"])
    db_path = config_file.parent.parent / "artifacts" / "warehouse.sqlite3"
    with Database(str(db_path)) as db:
        store.save_results(db, "RUN-1", "2026-11-07T12:00:00", [_result()])
    assert main(["--config", str(config_file), "report"]) == 0
    assert "cli.check" in capsys.readouterr().out


def test_main_ack_unknown_incident_returns_two(config_file: Path, capsys) -> None:
    main(["--config", str(config_file), "init"])
    assert main(["--config", str(config_file), "ack", "DQ-99999"]) == 2
    assert "ошибка" in capsys.readouterr().err


def test_main_resolve_unknown_incident_returns_two(config_file: Path, capsys) -> None:
    main(["--config", str(config_file), "init"])
    assert main(["--config", str(config_file), "resolve", "DQ-99999"]) == 2
    assert "ошибка" in capsys.readouterr().err


def test_cmd_init_returns_zero(config_file: Path, capsys) -> None:
    from reliability.config import load_config

    config = load_config(config_file)
    assert cmd_init(config, None) == 0
    assert "ops-схема готова" in capsys.readouterr().out


def test_cmd_report_without_runs(config_file: Path, capsys) -> None:
    from reliability.config import load_config

    config = load_config(config_file)
    cmd_init(config, None)
    assert cmd_report(config, None) == 1


def test_cmd_ack_reports_status(config_file: Path, capsys) -> None:
    from reliability.config import load_config

    config = load_config(config_file)
    cmd_init(config, None)
    with Database(config.database_path) as db:
        events = incidents.sync_incidents(db, "RUN-1", [_result()])
    args = build_parser().parse_args(["ack", events[0]["incident_id"], "--note", "взял"])
    assert cmd_ack(config, args) == 0
    assert "переведён в ACK" in capsys.readouterr().out


def test_cmd_resolve_reports_note(config_file: Path, capsys) -> None:
    from reliability.config import load_config

    config = load_config(config_file)
    cmd_init(config, None)
    with Database(config.database_path) as db:
        events = incidents.sync_incidents(db, "RUN-1", [_result()])
    args = build_parser().parse_args(["resolve", events[0]["incident_id"], "--note", "перезалили"])
    assert cmd_resolve(config, args) == 0
    assert "перезалили" in capsys.readouterr().out
