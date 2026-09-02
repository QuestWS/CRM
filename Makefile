.PHONY: install dev init serve worker test lint doctor check-mail brief

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt
	@echo "Now: cp .env.example .env && edit it, then 'make init'"

dev:
	.venv/bin/pip install -r requirements-dev.txt

init:
	.venv/bin/python -m crm.cli init

serve:
	.venv/bin/python -m crm.cli serve --reload

worker:
	.venv/bin/python -m crm.cli worker

doctor:
	.venv/bin/python -m crm.cli doctor

check-mail:
	.venv/bin/python -m crm.cli check-mail

brief:
	.venv/bin/python -m crm.cli brief

test:
	.venv/bin/python -m pytest tests/ -q

lint:
	.venv/bin/ruff check crm tests
