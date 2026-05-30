.PHONY: format format-check lint lock-check metadata syntax test verify

POETRY ?= poetry
PYTHON ?= $(POETRY) run python
BLACK ?= $(POETRY) run black
RUFF ?= $(POETRY) run ruff
PYTHONPYCACHEPREFIX ?= /tmp/dumb-pycache
PYTHON_TARGETS ?= api utils tests scripts

metadata:
	$(PYTHON) scripts/verify_project.py

lock-check:
	$(POETRY) check --lock

format:
	$(BLACK) $(PYTHON_TARGETS)

format-check:
	$(BLACK) --check $(PYTHON_TARGETS)

lint:
	$(RUFF) check $(PYTHON_TARGETS)

syntax:
	PYTHONPYCACHEPREFIX=$(PYTHONPYCACHEPREFIX) $(PYTHON) -m compileall -q $(PYTHON_TARGETS)

test:
	$(PYTHON) -m unittest discover -s tests

verify: metadata lock-check format-check lint syntax test
