"""Launcher для локального мока Jira: ``python tools/jira_mock.py --port 8099``.

Сама реализация лежит в ``src/reliability/jira_mock.py``, чтобы её импортировали
и тесты, и генератор картинок для README.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from reliability.jira_mock import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
