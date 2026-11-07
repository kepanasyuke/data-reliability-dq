"""Локальный мок Jira REST v2 — чтобы показать постановку инцидентов без лицензии.

Сервер на стандартной библиотеке (``http.server``), поэтому для демо не нужен ни
Docker, ни Java, ни Atlassian-аккаунт. Поддерживается минимум, который использует
``reliability.jira``:

    POST /rest/api/2/issue              создать задачу
    GET  /rest/api/2/issue/<KEY>        прочитать задачу
    GET  /rest/api/2/search?jql=...     поиск по метке labels = "dq:<id>"
    GET  /issues                        все созданные задачи (для скринов и тестов)
    GET  /health                        живость и счётчик задач

Запуск: ``python tools/jira_mock.py --port 8099``.
"""

from __future__ import annotations

import base64
import json
import re
import threading
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlparse

LABEL_JQL = re.compile(r'labels\s*=\s*"([^"]+)"')
ISSUE_PREFIX = "/rest/api/2/issue/"
SEARCH_PATH = "/rest/api/2/search"

BOARD_TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Jira mock · DQ-инциденты</title>
<style>
 body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0; background: #f6f7fb; color: #202630; }}
 header {{ background: #1f2c3e; color: #fff; padding: 22px 32px; }}
 h1 {{ margin: 0 0 6px; font-size: 22px; }}
 header p {{ margin: 0; color: #c7d0de; font-size: 14px; }}
 main {{ padding: 24px 32px 48px; }}
 table {{ border-collapse: collapse; width: 100%; background: #fff; box-shadow: 0 1px 3px rgba(20,30,50,.08); }}
 th, td {{ text-align: left; padding: 10px 14px; border-bottom: 1px solid #e6e9f0; font-size: 14px; }}
 th {{ background: #f0f3f8; color: #3c4a5e; font-weight: 600; }}
 td.key {{ font-family: Consolas, monospace; font-weight: 600; }}
 .p1 {{ color: #c63434; font-weight: 600; }}
 .p2 {{ color: #d67814; font-weight: 600; }}
 .p3 {{ color: #5a6478; }}
 .empty {{ padding: 28px; text-align: center; color: #6e7682; }}
 footer {{ margin-top: 18px; color: #6e7682; font-size: 13px; }}
 code {{ background: #eef1f6; padding: 2px 6px; border-radius: 4px; }}
</style>
</head>
<body>
<header>
  <h1>Jira mock · очередь DQ-инцидентов</h1>
  <p>задач: {total} · проект {project} · данные из <code>reliability.jira</code></p>
</header>
<main>
  <table>
    <thead><tr><th>Key</th><th>Приоритет</th><th>Метки</th><th>Тема</th></tr></thead>
    <tbody>
      {rows}
    </tbody>
  </table>
  <footer>
    Создать задачи: <code>python run.py jira --url http://127.0.0.1:&lt;port&gt;</code> ·
    JSON: <code>/issues</code>, <code>/rest/api/2/search?jql=labels="dq:DQ-00001"</code>
  </footer>
</main>
</body>
</html>
"""


class IssueStore:
    """Задачи в памяти; потокобезопасно — сервер многопоточный."""

    def __init__(self) -> None:
        self._issues: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def create(self, fields: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            project = str(fields.get("project", {}).get("key") or "DQ")
            number = sum(1 for issue in self._issues if issue["key"].startswith(f"{project}-")) + 1
            key = f"{project}-{number}"
            issue = {
                "id": str(len(self._issues) + 1),
                "key": key,
                "self": f"{ISSUE_PREFIX}{key}",
                "fields": fields,
            }
            self._issues.append(issue)
            return dict(issue)

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            found = [issue for issue in self._issues if issue["key"] == key]
            return dict(found[0]) if found else None

    def search(self, label: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(issue)
                for issue in self._issues
                if label in (issue["fields"].get("labels") or [])
            ]

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(issue) for issue in self._issues]

    def clear(self) -> None:
        with self._lock:
            self._issues.clear()


def _respond(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _respond_html(handler: BaseHTTPRequestHandler, status: int, document: str) -> None:
    body = document.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _severity_class(labels: Sequence[str]) -> str:
    found = [label.split(":", 1)[1] for label in labels if label.startswith("severity:")]
    return (found[0] if found else "P3").lower()


def _board_row(issue: Mapping[str, Any]) -> str:
    fields = issue["fields"]
    labels = [str(item) for item in (fields.get("labels") or [])]
    priority = escape(str((fields.get("priority") or {}).get("name", "")))
    return (
        f'<tr><td class="key">{escape(str(issue["key"]))}</td>'
        f'<td class="{_severity_class(labels)}">{priority}</td>'
        f"<td>{escape(', '.join(labels))}</td>"
        f"<td>{escape(str(fields.get('summary', '')))}</td></tr>"
    )


def _board_html(issues: Sequence[Mapping[str, Any]]) -> str:
    """HTML-доска для браузера: то, что открывают после ``run.py jira``."""
    rows = "".join(_board_row(issue) for issue in issues) or (
        '<tr><td colspan="4" class="empty">задач пока нет — выполните '
        "<code>python run.py jira</code></td></tr>"
    )
    projects = {str((issue["fields"].get("project") or {}).get("key", "")) for issue in issues}
    project = next(iter(projects - {""}), "DQ")
    return BOARD_TEMPLATE.format(total=len(issues), project=escape(project), rows=rows)


def _authorized(handler: BaseHTTPRequestHandler, token: str) -> bool:
    if not token:
        return True
    header = handler.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    return decoded.split(":", 1)[-1] == token


def _handle_search(handler: BaseHTTPRequestHandler, store: IssueStore, query: str) -> None:
    params = parse_qs(query)
    jql = params.get("jql", [""])[0]
    match = LABEL_JQL.search(jql)
    issues = store.search(match.group(1)) if match else store.all()
    _respond(handler, 200, {"total": len(issues), "issues": issues, "jql": jql})


def _handle_get(handler: BaseHTTPRequestHandler, store: IssueStore, path: str) -> None:
    if path in ("/", "/board"):
        _respond_html(handler, 200, _board_html(store.all()))
    elif path == "/health":
        _respond(handler, 200, {"status": "ok", "issues": len(store.all())})
    elif path == "/issues":
        issues = store.all()
        _respond(handler, 200, {"total": len(issues), "issues": issues})
    elif path.startswith(ISSUE_PREFIX):
        issue = store.get(path[len(ISSUE_PREFIX) :])
        status, payload = (200, issue) if issue else (404, {"errorMessages": ["issue not found"]})
        _respond(handler, status, payload or {})
    elif path == SEARCH_PATH:
        _respond(handler, 200, {"total": 0, "issues": []})
    else:
        _respond(handler, 404, {"errorMessages": [f"no route {path}"]})


def _handle_create(handler: BaseHTTPRequestHandler, store: IssueStore) -> None:
    length = int(handler.headers.get("Content-Length") or 0)
    try:
        body = json.loads(handler.rfile.read(length).decode("utf-8") or "{}")
    except json.JSONDecodeError:
        _respond(handler, 400, {"errorMessages": ["body is not valid JSON"]})
        return
    fields = body.get("fields") or {}
    missing = [name for name in ("project", "summary") if not fields.get(name)]
    if missing:
        _respond(handler, 400, {"errorMessages": [f"missing fields: {', '.join(missing)}"]})
        return
    issue = store.create(fields)
    _respond(handler, 201, {"id": issue["id"], "key": issue["key"], "self": issue["self"]})


def _make_handler(store: IssueStore, token: str) -> type[BaseHTTPRequestHandler]:
    class JiraMockHandler(BaseHTTPRequestHandler):
        server_version = "JiraMock/1.0"

        def do_GET(self) -> None:  # noqa: N802 - имя задаёт http.server
            parsed = urlparse(self.path)
            if not _authorized(self, token):
                _respond(self, 401, {"errorMessages": ["unauthorized"]})
                return
            if parsed.path == SEARCH_PATH:
                _handle_search(self, store, parsed.query)
            else:
                _handle_get(self, store, parsed.path)

        def do_POST(self) -> None:  # noqa: N802 - имя задаёт http.server
            if not _authorized(self, token):
                _respond(self, 401, {"errorMessages": ["unauthorized"]})
                return
            if urlparse(self.path).path == "/rest/api/2/issue":
                _handle_create(self, store)
            else:
                _respond(self, 404, {"errorMessages": ["no route"]})

        def log_message(self, *args: Any) -> None:
            return

    return JiraMockHandler


def create_server(port: int = 0, token: str = "") -> tuple[ThreadingHTTPServer, IssueStore]:
    """Поднять сервер в отдельном потоке; ``port=0`` — свободный порт."""
    store = IssueStore()
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(store, token))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, store


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="jira-mock", description="Локальный мок Jira REST v2")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--token", default="", help="если задан, требуется Basic-авторизация")
    args = parser.parse_args(argv)

    server, _ = create_server(args.port, args.token)
    host, port = server.server_address[:2]
    print(f"Jira mock: http://{host}:{port}  (Ctrl+C — остановить)")
    print(f"  project key по умолчанию: DQ, задачи: GET http://{host}:{port}/issues")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nостановлено")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
