.PHONY: audit secret-scan security env-check env-example format format-check lint lock-check metadata regression-image regression-service-updates syntax test verify

POETRY ?= poetry
PYTHON ?= $(POETRY) run python
BLACK ?= $(POETRY) run black
RUFF ?= $(POETRY) run ruff
PYTHONPYCACHEPREFIX ?= /tmp/dumb-pycache
PYTHON_TARGETS ?= api utils tests scripts
REGRESSION_PYTHON ?= python3
REGRESSION_JOBS ?= 2
REGRESSION_ARGS ?=
REGRESSION_IMAGE ?= dumb-regression-base:local
REGRESSION_DOCKER_BUILD_ARGS ?= --pull

audit:
	$(POETRY) run pip-audit

secret-scan:
	$(PYTHON) scripts/security_scan.py

security:
	$(MAKE) audit secret-scan

env-example:
	$(PYTHON) scripts/generate_env_example.py

env-check:
	$(PYTHON) scripts/generate_env_example.py --check

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

regression-image:
	docker build $(REGRESSION_DOCKER_BUILD_ARGS) -f .devcontainer/Dockerfile -t $(REGRESSION_IMAGE) .

regression-service-updates:
	$(REGRESSION_PYTHON) scripts/regression_service_updates.py --image $(REGRESSION_IMAGE) --jobs $(REGRESSION_JOBS) $(REGRESSION_ARGS)

verify: metadata lock-check format-check lint syntax test security
