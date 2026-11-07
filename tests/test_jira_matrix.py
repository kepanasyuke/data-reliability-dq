"""Jira-интеграция: payload v2, дедупликация по метке, локальный мок-сервер, CLI."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from reliability import incidents, jira, store
from reliability.checks import FAIL, CheckResult
from reliability.cli import build_parser, cmd_jira, main
from reliability.db import Database
from reliability.jira_mock import IssueStore, create_server

REPO_ROOT = Path(__file__).resolve().parents[1]
EVENT = {
    "action": "OPENED",
    "incident_id": "DQ-00001",
    "check_id": "turnover.orphan_client",
    "severity": "P1",
    "occurrences": 3,
}


@pytest.fixture()
def mock_jira():
    server, issue_store = create_server()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}", issue_store
    server.shutdown()
    server.server_close()


@pytest.fixture()
def secured_jira():
    server, issue_store = create_server(token="secret")
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}", issue_store
    server.shutdown()
    server.server_close()


def _issue(incident_id: str = "DQ-00001") -> CheckResult:
    return CheckResult(
        check_id="turnover.orphan_client",
        title="Сироты по справочнику",
        entity="fct_account_turnover_daily",
        severity="P1",
        owner="owner@example.org",
        status=FAIL,
        metric_value=6.0,
        unit="count",
        bounds={"fail": {"max": 0}},
        message="count: 6",
    )


def _write_config(tmp_path: Path, base_url: str = "") -> Path:
    path = tmp_path / "config" / "demo.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "env": "jira-test",
        "dsn": "artifacts/warehouse.sqlite3",
        "checks": str(REPO_ROOT / "checks" / "dq_checks.json"),
        "notify": {"channel": "#dq", "jira_project": "DQ", "jira_url": base_url},
        "artifacts": {"metrics": "m.prom", "report": "r.md"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _http(url: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _http_text(url: str) -> tuple[int, str, str]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")


# -- payload ----------------------------------------------------------------
@pytest.mark.parametrize(
    "severity,expected",
    [("P1", "Highest"), ("P2", "High"), ("P3", "Medium"), ("P4", "Medium"), ("", "Medium")],
)
def test_priority_mapping(severity: str, expected: str) -> None:
    assert jira.priority_for(severity) == expected


def test_incident_label_format() -> None:
    assert jira.incident_label("DQ-00007") == "dq:DQ-00007"


@pytest.mark.parametrize("action,word", [("OPENED", "открыт"), ("REPEATED", "повторяется"), ("AUTO_RESOLVED", "автозакрыт")])
def test_summary_mentions_action(action: str, word: str) -> None:
    payload = jira.issue_payload({**EVENT, "action": action}, "RUN-1", "DQ")
    assert word in payload["fields"]["summary"]


def test_payload_has_project_and_issue_type() -> None:
    fields = jira.issue_payload(EVENT, "RUN-1", "DATA")["fields"]
    assert fields["project"] == {"key": "DATA"}
    assert fields["issuetype"] == {"name": "Bug"}


def test_payload_labels_are_deduplicable() -> None:
    labels = jira.issue_payload(EVENT, "RUN-9", "DQ")["fields"]["labels"]
    assert {"reliability-dq", "severity:P1", "run:RUN-9", "dq:DQ-00001"} <= set(labels)


def test_payload_priority_follows_severity() -> None:
    fields = jira.issue_payload({**EVENT, "severity": "P2"}, "RUN-1", "DQ")["fields"]
    assert fields["priority"] == {"name": "High"}


def test_payload_description_keeps_run_and_incident() -> None:
    description = jira.issue_payload(EVENT, "RUN-42", "DQ")["fields"]["description"]
    assert "DQ-00001" in description
    assert "RUN-42" in description


def test_payload_summary_is_short_enough() -> None:
    long_event = {**EVENT, "check_id": "c" * 300}
    assert len(jira.issue_payload(long_event, "RUN-1", "DQ")["fields"]["summary"]) <= 254


# -- конфигурация -----------------------------------------------------------
def test_config_defaults() -> None:
    settings = jira.JiraConfig.from_notify({})
    assert (settings.base_url, settings.project, settings.enabled) == ("", "DQ", False)


def test_config_reads_notify_block() -> None:
    settings = jira.JiraConfig.from_notify(
        {"jira_url": "http://jira.local", "jira_project": "DATA", "jira_timeout": 9}
    )
    assert settings.base_url == "http://jira.local"
    assert settings.project == "DATA"
    assert settings.timeout == 9.0
    assert settings.enabled is True


def test_auth_header_is_basic_base64() -> None:
    settings = jira.JiraConfig(base_url="http://x", user="api", token="secret")
    assert settings.auth_header == "Basic " + base64.b64encode(b"api:secret").decode()


def test_auth_header_empty_without_token() -> None:
    assert jira.JiraConfig(base_url="http://x").auth_header == ""


# -- клиент на фейковом транспорте -----------------------------------------
def _transport_factory(status: int, body: dict, seen: list):
    def transport(method, url, auth, payload, timeout):
        seen.append((method, url, payload))
        return status, body

    return transport


def test_create_issue_sends_post_and_returns_body() -> None:
    seen: list = []
    client = jira.JiraClient(jira.JiraConfig(base_url="http://x"), _transport_factory(201, {"key": "DQ-1"}, seen))
    assert client.create_issue({"fields": {}}) == {"key": "DQ-1"}
    assert seen[0][:2] == ("POST", "http://x/rest/api/2/issue")


def test_create_issue_raises_on_error_status() -> None:
    client = jira.JiraClient(jira.JiraConfig(base_url="http://x"), _transport_factory(400, {"e": 1}, []))
    with pytest.raises(jira.JiraError):
        client.create_issue({"fields": {}})


def test_find_by_label_builds_jql() -> None:
    seen: list = []
    client = jira.JiraClient(jira.JiraConfig(base_url="http://x"), _transport_factory(200, {"issues": [{"key": "DQ-2"}]}, seen))
    assert client.find_by_label("dq:DQ-00002") == [{"key": "DQ-2"}]
    assert "labels" in seen[0][1]


def test_upsert_returns_existing_issue() -> None:
    transport = _transport_factory(200, {"issues": [{"key": "DQ-3"}]}, [])
    client = jira.JiraClient(jira.JiraConfig(base_url="http://x"), transport)
    assert client.upsert_issue({"fields": {}}, "dq:DQ-00003") == ({"key": "DQ-3"}, "EXISTING")


def test_upsert_creates_when_not_found() -> None:
    calls: list = []

    def transport(method, url, auth, payload, timeout):
        calls.append(method)
        return (200, {"issues": []}) if method == "GET" else (201, {"key": "DQ-4"})

    client = jira.JiraClient(jira.JiraConfig(base_url="http://x"), transport)
    assert client.upsert_issue({"fields": {}}, "dq:DQ-00004") == ({"key": "DQ-4"}, "CREATED")
    assert calls == ["GET", "POST"]


# -- dry-run и мок-сервер ---------------------------------------------------
def test_dry_run_returns_plan_without_network() -> None:
    results = jira.push_events([EVENT], "RUN-1", jira.JiraConfig(project="DQ"))
    assert results[0]["action"] == "DRY_RUN"
    assert results[0]["payload"]["fields"]["project"] == {"key": "DQ"}


def test_live_push_creates_then_reuses_issue(mock_jira) -> None:
    base_url, issue_store = mock_jira
    settings = jira.JiraConfig(base_url=base_url, project="DQ")
    first = jira.push_events([EVENT], "RUN-1", settings)
    second = jira.push_events([EVENT], "RUN-2", settings)
    assert first[0]["action"] == "CREATED"
    assert second[0]["action"] == "EXISTING"
    assert len(issue_store.all()) == 1


@pytest.mark.parametrize("count", [1, 3])
def test_mock_creates_one_issue_per_incident(mock_jira, count: int) -> None:
    base_url, issue_store = mock_jira
    events = [{**EVENT, "incident_id": f"DQ-0000{index}"} for index in range(1, count + 1)]
    results = jira.push_events(events, "RUN-1", jira.JiraConfig(base_url=base_url))
    assert all(item["action"] == "CREATED" for item in results)
    assert len(issue_store.all()) == count


def test_mock_health_endpoint(mock_jira) -> None:
    base_url, _ = mock_jira
    assert _http(f"{base_url}/health")[1]["status"] == "ok"


def test_mock_issues_endpoint(mock_jira) -> None:
    base_url, _ = mock_jira
    jira.push_events([EVENT], "RUN-1", jira.JiraConfig(base_url=base_url))
    status, body = _http(f"{base_url}/issues")
    assert status == 200
    assert body["total"] == 1


def test_mock_returns_issue_by_key(mock_jira) -> None:
    base_url, _ = mock_jira
    created = jira.push_events([EVENT], "RUN-1", jira.JiraConfig(base_url=base_url))[0]
    status, body = _http(f"{base_url}/rest/api/2/issue/{created['key']}")
    assert (status, body["key"]) == (200, created["key"])


def test_mock_returns_404_for_unknown_issue(mock_jira) -> None:
    base_url, _ = mock_jira
    assert _http(f"{base_url}/rest/api/2/issue/DQ-999")[0] == 404


def test_mock_rejects_issue_without_project(mock_jira) -> None:
    base_url, _ = mock_jira
    assert _http(f"{base_url}/rest/api/2/issue", "POST", {"fields": {"summary": "x"}})[0] == 400


def test_mock_requires_token_when_configured(secured_jira) -> None:
    base_url, _ = secured_jira
    assert _http(f"{base_url}/issues")[0] == 401


def test_client_with_wrong_token_raises(secured_jira) -> None:
    base_url, _ = secured_jira
    with pytest.raises(jira.JiraError):
        jira.push_events([EVENT], "RUN-1", jira.JiraConfig(base_url=base_url, user="mock", token="bad"))


def test_client_with_right_token_pushes(secured_jira) -> None:
    base_url, _ = secured_jira
    results = jira.push_events([EVENT], "RUN-1", jira.JiraConfig(base_url=base_url, user="mock", token="secret"))
    assert results[0]["action"] == "CREATED"


def test_store_clear_empties_issues() -> None:
    store_ = IssueStore()
    store_.create({"project": {"key": "DQ"}, "summary": "x"})
    store_.clear()
    assert store_.all() == []


# -- события и CLI ----------------------------------------------------------
def test_events_from_incidents_maps_fields() -> None:
    events = jira.events_from_incidents(
        [{"incident_id": "DQ-00001", "check_id": "c", "severity": "P2", "occurrences": 4}]
    )
    assert events[0] == {
        "action": "OPENED",
        "incident_id": "DQ-00001",
        "check_id": "c",
        "severity": "P2",
        "occurrences": 4,
    }


def test_jira_subcommand_is_parsed() -> None:
    args = build_parser().parse_args(["jira", "--url", "http://j", "--project", "DATA"])
    assert (args.command, args.url, args.project) == ("jira", "http://j", "DATA")


def test_cmd_jira_dry_run_prints_plan(tmp_path: Path, capsys) -> None:
    from reliability.config import load_config

    config_file = _write_config(tmp_path)
    config = load_config(config_file)
    with Database(config.database_path) as db:
        store.ensure_schema(db)
        incidents.sync_incidents(db, "RUN-1", [_issue()])
    args = build_parser().parse_args(["jira"])
    assert cmd_jira(config, args) == 0
    assert "dry-run" in capsys.readouterr().out


def test_cmd_jira_live_push_returns_zero(tmp_path: Path, mock_jira, capsys) -> None:
    from reliability.config import load_config

    base_url, issue_store = mock_jira
    config_file = _write_config(tmp_path)
    config = load_config(config_file)
    with Database(config.database_path) as db:
        store.ensure_schema(db)
        incidents.sync_incidents(db, "RUN-1", [_issue()])
    args = build_parser().parse_args(["jira", "--url", base_url])
    assert cmd_jira(config, args) == 0
    assert len(issue_store.all()) == 1
    assert "CREATED" in capsys.readouterr().out


def test_cmd_jira_unreachable_server_returns_two(tmp_path: Path, capsys) -> None:
    from reliability.config import load_config

    config = load_config(_write_config(tmp_path))
    with Database(config.database_path) as db:
        store.ensure_schema(db)
        incidents.sync_incidents(db, "RUN-1", [_issue()])
    args = build_parser().parse_args(["jira", "--url", "http://127.0.0.1:1"])
    assert cmd_jira(config, args) == 2
    assert "ошибка Jira" in capsys.readouterr().err


def test_main_runs_jira_command(tmp_path: Path, capsys) -> None:
    config_file = _write_config(tmp_path)
    assert main(["--config", str(config_file), "init"]) == 0
    assert main(["--config", str(config_file), "jira"]) == 0
    assert "Jira:" in capsys.readouterr().out


def test_board_empty_state(mock_jira) -> None:
    base_url, _ = mock_jira
    status, content_type, html = _http_text(f"{base_url}/")
    assert (status, "text/html" in content_type) == (200, True)
    assert "задач: 0" in html
    assert "задач пока нет" in html


def test_board_lists_created_issues(mock_jira) -> None:
    base_url, _ = mock_jira
    jira.push_events([EVENT], "RUN-1", jira.JiraConfig(base_url=base_url))
    _, _, html = _http_text(f"{base_url}/")
    assert "DQ-1" in html
    assert "Highest" in html
    assert "severity:P1" in html
    assert "turnover.orphan_client" in html


def test_board_marks_severity_class(mock_jira) -> None:
    base_url, _ = mock_jira
    jira.push_events([{**EVENT, "severity": "P2"}], "RUN-1", jira.JiraConfig(base_url=base_url))
    _, _, html = _http_text(f"{base_url}/board")
    assert 'class="p2"' in html


def test_board_escapes_summary(mock_jira) -> None:
    base_url, _ = mock_jira
    event = {**EVENT, "check_id": "<script>alert(1)</script>"}
    jira.push_events([event], "RUN-1", jira.JiraConfig(base_url=base_url))
    _, _, html = _http_text(f"{base_url}/")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
