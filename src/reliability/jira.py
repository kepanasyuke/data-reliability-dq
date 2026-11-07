"""Jira REST v2: постановка инцидентов задачами.

Почему REST v2, а не v3: во второй версии ``description`` — обычный текст, в
третьей требуется Atlassian Document Format (JSON-дерево). Для DQ-тикета
достаточно плоского текста, поэтому v2 проще и одинаково понимается и настоящей
Jira Server/Data Center, и локальным мок-сервером ``tools/jira_mock.py``.

Зависимостей нет: HTTP через ``urllib``. Если ``base_url`` пуст — клиент работает
в dry-run и возвращает план отправки, ничего не отправляя. Дедупликация: перед
созданием задачи ищется существующая по метке ``dq:<incident_id>``, повторный
прогон не плодит тикеты — так же, как ``sync_incidents`` не плодит инциденты.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

ISSUE_PATH = "/rest/api/2/issue"
SEARCH_PATH = "/rest/api/2/search"
ISSUE_TYPE = "Bug"
SOURCE_LABEL = "reliability-dq"
PRIORITY_BY_SEVERITY = {"P1": "Highest", "P2": "High", "P3": "Medium"}
ACTION_TEXT = {
    "OPENED": "открыт",
    "REPEATED": "повторяется",
    "AUTO_RESOLVED": "автозакрыт",
}

Transport = Callable[[str, str, str, "dict[str, Any] | None", float], "tuple[int, dict[str, Any]]"]


class JiraError(RuntimeError):
    """Jira вернула ошибку или недоступна."""


def priority_for(severity: str) -> str:
    return PRIORITY_BY_SEVERITY.get(severity, "Medium")


def incident_label(incident_id: str) -> str:
    return f"dq:{incident_id}"


def _summary(event: Mapping[str, Any]) -> str:
    action = ACTION_TEXT.get(str(event.get("action")), str(event.get("action")))
    return f"[DQ {event['severity']}] {event['check_id']} - инцидент {event['incident_id']} {action}"


def _description(event: Mapping[str, Any], run_id: str) -> str:
    return "\n".join(
        [
            f"Инцидент: {event['incident_id']}",
            f"Проверка: {event['check_id']}",
            f"Приоритет: {event['severity']} (SLA реакции см. incidents.SEVERITY_SLA_MINUTES)",
            f"Событие: {event.get('action')}",
            f"Повторов: {event.get('occurrences', 1)}",
            f"Прогон: {run_id}",
            "",
            "Открыть контекст: python run.py status && python run.py report",
        ]
    )


def issue_payload(event: Mapping[str, Any], run_id: str, project: str) -> dict[str, Any]:
    """Jira v2 payload: проект, тип задачи, приоритет, метки, текст."""
    return {
        "fields": {
            "project": {"key": project},
            "summary": _summary(event)[:254],
            "description": _description(event, run_id),
            "issuetype": {"name": ISSUE_TYPE},
            "priority": {"name": priority_for(str(event["severity"]))},
            "labels": [
                SOURCE_LABEL,
                f"severity:{event['severity']}",
                f"run:{run_id}",
                incident_label(str(event["incident_id"])),
            ],
        }
    }


@dataclass(frozen=True)
class JiraConfig:
    base_url: str = ""
    project: str = "DQ"
    user: str = ""
    token: str = ""
    timeout: float = 5.0

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    @property
    def auth_header(self) -> str:
        if not self.token:
            return ""
        raw = f"{self.user}:{self.token}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")

    @classmethod
    def from_notify(cls, notify: Mapping[str, Any] | None) -> JiraConfig:
        payload = dict(notify or {})
        return cls(
            base_url=str(payload.get("jira_url") or ""),
            project=str(payload.get("jira_project") or "DQ"),
            user=str(payload.get("jira_user") or ""),
            token=str(payload.get("jira_token") or ""),
            timeout=float(payload.get("jira_timeout") or 5.0),
        )


def _http_transport(
    method: str, url: str, auth_header: str, body: dict[str, Any] | None, timeout: float
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    if auth_header:
        request.add_header("Authorization", auth_header)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise JiraError(f"{method} {url} -> HTTP {exc.code}: {detail[:400]}") from exc
    except urllib.error.URLError as exc:
        raise JiraError(f"{method} {url} -> {exc.reason}") from exc
    return status, json.loads(text) if text else {}


class JiraClient:
    """Тонкая обёртка: create / search by label / upsert."""

    def __init__(self, config: JiraConfig, transport: Transport | None = None):
        self.config = config
        self._transport = transport or _http_transport

    def _call(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        url = self.config.base_url.rstrip("/") + path
        return self._transport(method, url, self.config.auth_header, body, self.config.timeout)

    def create_issue(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        status, body = self._call("POST", ISSUE_PATH, dict(payload))
        if status >= 400:
            raise JiraError(f"create issue failed: HTTP {status} {body}")
        return body

    def find_by_label(self, label: str) -> list[dict[str, Any]]:
        jql = f'labels = "{label}"'
        query = urllib.parse.urlencode({"jql": jql, "maxResults": 5})
        status, body = self._call("GET", f"{SEARCH_PATH}?{query}")
        if status >= 400:
            raise JiraError(f"search failed: HTTP {status} {body}")
        return list(body.get("issues") or [])

    def upsert_issue(self, payload: Mapping[str, Any], label: str) -> tuple[dict[str, Any], str]:
        existing = self.find_by_label(label)
        if existing:
            return existing[0], "EXISTING"
        return self.create_issue(payload), "CREATED"


def push_events(
    events: Iterable[Mapping[str, Any]],
    run_id: str,
    config: JiraConfig,
    transport: Transport | None = None,
) -> list[dict[str, Any]]:
    """Создать/найти задачу для каждого события; без ``base_url`` — только план."""
    results: list[dict[str, Any]] = []
    client = JiraClient(config, transport) if config.enabled else None
    for event in events:
        payload = issue_payload(event, run_id, config.project)
        label = incident_label(str(event["incident_id"]))
        if client is None:
            results.append(
                {
                    "incident_id": event["incident_id"],
                    "action": "DRY_RUN",
                    "label": label,
                    "payload": payload,
                }
            )
            continue
        issue, action = client.upsert_issue(payload, label)
        results.append(
            {
                "incident_id": event["incident_id"],
                "action": action,
                "key": issue.get("key"),
                "label": label,
                "payload": payload,
            }
        )
    return results


def events_from_incidents(incidents: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Открытые инциденты -> события того же вида, что отдаёт sync_incidents."""
    return [
        {
            "action": "OPENED",
            "incident_id": record["incident_id"],
            "check_id": record["check_id"],
            "severity": record["severity"],
            "occurrences": int(record.get("occurrences", 1)),
        }
        for record in incidents
    ]
