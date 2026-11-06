PYTHON ?= python
CONFIG ?= config/demo.json

.PHONY: help seed seed-clean run demo status report test install lint clean

help:
	@echo "make seed        - демо-хранилище с внесёнными дефектами"
	@echo "make seed-clean  - то же хранилище без дефектов"
	@echo "make run         - прогон каталога проверок (exit code по fail_on)"
	@echo "make demo        - сценарий: дефекты -> инцидент -> восстановление -> MTTR"
	@echo "make status      - открытые инциденты, MTTR, SLA"
	@echo "make report      - что упало в последнем прогоне"
	@echo "make test        - pytest"
	@echo "make install     - установка пакета в editable-режиме"

install:
	$(PYTHON) -m pip install -e ".[dev]"

seed:
	$(PYTHON) run.py --config $(CONFIG) seed

seed-clean:
	$(PYTHON) run.py --config $(CONFIG) seed --clean

run:
	$(PYTHON) run.py --config $(CONFIG) run

demo:
	$(PYTHON) run.py --config $(CONFIG) demo

status:
	$(PYTHON) run.py --config $(CONFIG) status

report:
	$(PYTHON) run.py --config $(CONFIG) report

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m compileall -q src dags tests run.py

clean:
	rm -rf artifacts .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
