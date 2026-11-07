"""Генерация иллюстраций для README.

Что делает скрипт:
  * собирает демо-хранилище, прогоняет каталог проверок с дефектами и без;
  * рисует PNG-таблицы из ops-схемы (результаты проверок, инциденты, метрики);
  * собирает GIF с динамикой метрик: красные карточки -> зелёные после перезаливки.

Запуск::

    python tools/render_assets.py

Картинки кладутся в ``docs/images/``. Это рендеры демо-данных из SQLite, а не
скриншоты pgAdmin: проект запускается без кластера Greenplum.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from reliability import incidents, jira, store  # noqa: E402
from reliability.cli import execute_run  # noqa: E402
from reliability.config import Config, load_config  # noqa: E402
from reliability.db import Database  # noqa: E402
from reliability.jira_mock import create_server  # noqa: E402
from reliability.synth import build_demo  # noqa: E402

OUT_DIR = REPO_ROOT / "docs" / "images"
FONT_DIR = Path("C:/Windows/Fonts")
CONFIG_PATH = REPO_ROOT / "config" / "demo.json"

BG = (250, 250, 252)
INK = (32, 38, 48)
MUTED = (110, 118, 130)
HEAD_BG = (32, 44, 62)
STRIPE = (243, 245, 249)
STATUS_COLORS = {
    "PASS": (34, 148, 82),
    "WARN": (214, 150, 20),
    "FAIL": (198, 52, 52),
    "ERROR": (122, 30, 62),
}
SEVERITY_COLORS = {"P1": (198, 52, 52), "P2": (214, 120, 20), "P3": (90, 100, 120)}


# -- шрифты -----------------------------------------------------------------
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "consolab.ttf" if bold else "consola.ttf"
    for candidate in (name, "arialbd.ttf" if bold else "arial.ttf"):
        path = FONT_DIR / candidate
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


# -- таблицы ----------------------------------------------------------------
def _cell_text(value: Any, limit: int = 34) -> str:
    text = "-" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _row_height() -> int:
    return 38


def _table_geometry(headers: Sequence[str], widths: Sequence[int]) -> tuple[int, int]:
    width = sum(widths) + 48
    height = 104 + _row_height() * 1 + 8
    return width, height


def _draw_header(draw: ImageDraw.ImageDraw, headers: Sequence[str], widths: Sequence[int]) -> None:
    right = sum(widths) + 48
    draw.rectangle([0, 56, right, 104], fill=(255, 255, 255))
    draw.line([0, 104, right, 104], fill=(222, 226, 234), width=1)
    x = 24
    for header, width in zip(headers, widths, strict=True):
        draw.text((x, 66), header, font=_font(17, True), fill=HEAD_BG)
        x += width


def _draw_rows(
    draw: ImageDraw.ImageDraw,
    rows: Sequence[Sequence[Any]],
    widths: Sequence[int],
    colors: Sequence[Sequence[tuple[int, int, int] | None]] | None,
) -> None:
    top = 104 + 8
    row_height = _row_height()
    for index, row in enumerate(rows):
        y = top + index * row_height
        draw.rectangle([0, y, sum(widths) + 48, y + row_height], fill=STRIPE if index % 2 else BG)
        x = 24
        for column, (value, width) in enumerate(zip(row, widths, strict=True)):
            color = INK
            if colors and colors[index][column]:
                color = colors[index][column]  # type: ignore[assignment]
            draw.text((x, y + 9), _cell_text(value), font=_font(17, bool(colors and colors[index][column])), fill=color)
            x += width


def _save_table(
    path: Path,
    title: str,
    subtitle: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    widths: Sequence[int],
    colors: Sequence[Sequence[tuple[int, int, int] | None]] | None = None,
) -> Path:
    width, base_height = _table_geometry(headers, widths)
    height = base_height + _row_height() * len(rows)
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, width, height], outline=(222, 226, 234))
    draw.text((24, 16), title, font=_font(22, True), fill=INK)
    draw.text((24, 44), subtitle, font=_font(14), fill=MUTED)
    _draw_header(draw, headers, widths)
    _draw_rows(draw, rows, widths, colors)
    image.save(path)
    return path


# -- сбор данных ------------------------------------------------------------
def _result_rows(results: Sequence[Any]) -> tuple[list[list[Any]], list[list[tuple[int, int, int] | None]]]:
    rows: list[list[Any]] = []
    colors: list[list[tuple[int, int, int] | None]] = []
    for result in results:
        rows.append(
            [
                result.check_id,
                result.entity,
                result.severity,
                result.status,
                "-" if result.metric_value is None else f"{result.metric_value:g}",
                result.unit,
            ]
        )
        colors.append([None, None, SEVERITY_COLORS.get(result.severity), STATUS_COLORS.get(result.status), None, None])
    return rows, colors


def _incident_rows(records: Sequence[dict[str, Any]]) -> tuple[list[list[Any]], list[list[tuple[int, int, int] | None]]]:
    rows: list[list[Any]] = []
    colors: list[list[tuple[int, int, int] | None]] = []
    for record in records:
        rows.append(
            [
                record["incident_id"],
                record["severity"],
                record["status"],
                record["check_id"],
                str(record["opened_at"])[:16],
                record["occurrences"],
            ]
        )
        colors.append([None, SEVERITY_COLORS.get(record["severity"]), None, None, None, None])
    return rows, colors


def _collect(config: Config) -> dict[str, Any]:
    """Прогон с дефектами и после перезаливки; попутно собираем таблицы и снимки."""
    with Database(config.database_path) as db:
        store.ensure_schema(db)
        build_demo(db, REPO_ROOT, inject_defects=True, seed=42)
        broken = execute_run(config, db, verbose=False)
        incident_records = incidents.open_incidents(db)
        repeats = [execute_run(config, db, verbose=False) for _ in range(2)]

        build_demo(db, REPO_ROOT, inject_defects=False, seed=42)
        healthy = execute_run(config, db, verbose=False)
        clean_repeats = [execute_run(config, db, verbose=False) for _ in range(2)]
        stats = incidents.mttr(db)

    return {
        "broken": broken,
        "repeats": repeats,
        "healthy": healthy,
        "clean_repeats": clean_repeats,
        "incidents": incident_records,
        "mttr": stats,
    }


# -- GIF --------------------------------------------------------------------
def _card_rect(index: int) -> tuple[int, int, int, int]:
    columns = 3
    width, height = 368, 118
    margin_x, margin_y = 24, 118
    gap = 10
    column = index % columns
    row = index // columns
    left = margin_x + column * (width + gap)
    top = margin_y + row * (height + gap)
    return left, top, left + width, top + height


def _draw_card(draw: ImageDraw.ImageDraw, index: int, result: Any) -> None:
    left, top, right, bottom = _card_rect(index)
    color = STATUS_COLORS.get(result.status, MUTED)
    draw.rounded_rectangle([left, top, right, bottom], radius=10, fill=(255, 255, 255), outline=color, width=2)
    draw.rectangle([left, top, left + 6, bottom], fill=color)
    draw.text((left + 16, top + 10), result.check_id[:30], font=_font(15, True), fill=INK)
    draw.text((left + 16, top + 34), f"{result.status} · {result.severity}", font=_font(14, True), fill=color)
    value = "-" if result.metric_value is None else f"{result.metric_value:g}"
    draw.text((left + 16, top + 58), f"{value} {result.unit}", font=_font(15), fill=MUTED)
    draw.text((left + 16, top + 84), result.entity[:30], font=_font(12), fill=MUTED)


def _frame(snapshot: dict[str, Any], caption: str, step: int, total: int) -> Image.Image:
    image = Image.new("RGB", (1180, 700), BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, 1180, 100], fill=HEAD_BG)
    draw.text((24, 18), "Data Quality · mart-слой банка", font=_font(24, True), fill=(255, 255, 255))
    draw.text((24, 56), caption, font=_font(16), fill=(206, 214, 226))
    for index, result in enumerate(snapshot["results"][:12]):
        _draw_card(draw, index, result)
    draw.text(
        (24, 640),
        f"run {snapshot['run_id']} · открыто {snapshot['opened']} · автозакрыто {snapshot['resolved']}",
        font=_font(15),
        fill=MUTED,
    )
    draw.text((24, 668), f"кадр {step}/{total}", font=_font(14), fill=MUTED)
    return image


def _snapshot(outcome: dict[str, Any], opened: int, resolved: int, caption: str) -> dict[str, Any]:
    return {
        "run_id": outcome["run_id"],
        "results": outcome["results"],
        "opened": opened,
        "resolved": resolved,
        "caption": caption,
    }


def _gif_frames(collected: dict[str, Any]) -> list[Image.Image]:
    summary = collected["broken"]["payload"]["summary"]
    open_after_broken = summary["opened"]
    clean_summary = collected["healthy"]["payload"]["summary"]
    captions = [
        (collected["broken"], f"дефекты внесены: {open_after_broken} инцидентов открыто"),
        (collected["repeats"][0], "повторный прогон через 30 минут: тикеты не дублируются"),
        (collected["repeats"][1], "третий прогон: растёт только счётчик повторов"),
        (collected["healthy"], f"перезаливка без дефектов: {clean_summary['resolved']} авто-закрытий"),
        (collected["clean_repeats"][0], "следующий прогон: всё зелёное"),
        (collected["clean_repeats"][1], "MTTR зафиксирован, SLA в норме"),
    ]
    snapshots = [
        _snapshot(outcome, open_after_broken if index < 3 else 0, 0 if index < 3 else clean_summary["resolved"], caption)
        for index, (outcome, caption) in enumerate(captions)
    ]
    return [_frame(item, item["caption"], index + 1, len(snapshots)) for index, item in enumerate(snapshots)]


# -- основной сценарий ------------------------------------------------------
def _jira_rows(issues: Sequence[dict[str, Any]]) -> tuple[list[list[Any]], list[list[tuple[int, int, int] | None]]]:
    rows: list[list[Any]] = []
    colors: list[list[tuple[int, int, int] | None]] = []
    for issue in issues:
        fields = issue["fields"]
        severity = str(fields["labels"][1]).split(":", 1)[-1]
        rows.append(
            [
                issue["key"],
                fields["priority"]["name"],
                f"severity:{severity}",
                fields["summary"][:56],
            ]
        )
        colors.append([None, SEVERITY_COLORS.get(severity), None, None])
    return rows, colors


def _render_jira(collected: dict[str, Any]) -> Path:
    """Поднять мок Jira, поставить задачи по инцидентам и нарисовать очередь."""
    server, issue_store = create_server()
    host, port = server.server_address[:2]
    settings = jira.JiraConfig(base_url=f"http://{host}:{port}", project="DQ")
    events = jira.events_from_incidents(collected["incidents"])
    results = jira.push_events(events, collected["broken"]["run_id"], settings)
    issues = issue_store.all()
    server.shutdown()
    rows, colors = _jira_rows(issues)
    created = sum(1 for item in results if item["action"] == "CREATED")
    return _save_table(
        OUT_DIR / "jira_issues.png",
        "Jira · задачи по DQ-инцидентам",
        f"мок Jira REST v2 {host}:{port} · создано {created} задач · дедуп по метке dq:<incident_id>",
        ["Key", "Приоритет", "Метка", "Тема"],
        rows,
        [90, 130, 150, 620],
        colors,
    )


def _metrics_excerpt() -> list[str]:
    path = REPO_ROOT / "artifacts" / "metrics" / "dq.prom"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [line for line in lines if not line.startswith("#")][:18]


def _metrics_image(lines: Sequence[str]) -> Path:
    width, height = 1180, 104 + 22 * (len(lines) + 2)
    image = Image.new("RGB", (width, height), (24, 28, 36))
    draw = ImageDraw.Draw(image)
    draw.text((24, 18), "artifacts/metrics/dq.prom", font=_font(20, True), fill=(236, 240, 248))
    draw.text((24, 48), "выгрузка Prometheus textfile, первые строки", font=_font(14), fill=(150, 160, 178))
    for index, line in enumerate(lines):
        color = (255, 255, 255) if index % 2 == 0 else (208, 216, 230)
        draw.text((24, 88 + index * 22), line[:120], font=_font(15), fill=color)
    path = OUT_DIR / "metrics_textfile.png"
    image.save(path)
    return path


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    config = load_config(CONFIG_PATH)
    collected = _collect(config)

    broken_rows, broken_colors = _result_rows(collected["broken"]["results"])
    _save_table(
        OUT_DIR / "checks_table.png",
        "ops_dq_result · прогон с дефектами",
        f"run {collected['broken']['run_id']} · таблица результатов проверок",
        ["Проверка", "Сущность", "Sev", "Статус", "Значение", "Ед."],
        broken_rows,
        [250, 250, 60, 90, 110, 80],
        broken_colors,
    )

    clean_rows, clean_colors = _result_rows(collected["healthy"]["results"])
    _save_table(
        OUT_DIR / "checks_table_clean.png",
        "ops_dq_result · после перезаливки",
        f"run {collected['healthy']['run_id']} · все проверки зелёные, инциденты закрыты",
        ["Проверка", "Сущность", "Sev", "Статус", "Значение", "Ед."],
        clean_rows,
        [250, 250, 60, 90, 110, 80],
        clean_colors,
    )

    incident_rows, incident_colors = _incident_rows(collected["incidents"])
    _save_table(
        OUT_DIR / "incidents_table.png",
        "ops_dq_incident · инциденты прогона с дефектами",
        "дедупликация по check_id|entity, приоритеты P1–P3",
        ["ID", "Sev", "Статус", "Проверка", "Открыт (UTC)", "Повторов"],
        incident_rows,
        [110, 60, 110, 300, 200, 110],
        incident_colors,
    )

    frames = _gif_frames(collected)
    gif_path = OUT_DIR / "dq_metrics.gif"
    frames[0].save(
        gif_path,
        save_all=True,
        append_images=frames[1:],
        duration=1400,
        loop=0,
        optimize=True,
    )

    metrics_path = _metrics_image(_metrics_excerpt())
    jira_path = _render_jira(collected)
    print(f"готово: {len(frames)} кадров GIF")
    for path in sorted(OUT_DIR.glob("*")):
        print(f"  {path.relative_to(REPO_ROOT)}  {path.stat().st_size // 1024} КБ")
    return 0 if jira_path.exists() and metrics_path.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
