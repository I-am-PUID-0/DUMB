# syntax=docker/dockerfile:1.7

ARG DUMB_IMAGE=iampuid0/dumb:dev
FROM ${DUMB_IMAGE}

ARG POETRY_VERSION=2.4.1

# Source-built service artifacts persist across devcontainer rebuilds. Install
# Maintainerr's canvas dependencies here as well as in the production image so
# a locally rebuilt devcontainer does not depend on the last published dev tag.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev \
      libpixman-1-dev librsvg2-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml poetry.lock /tmp/dumb-dev-dependencies/

RUN --mount=type=cache,target=/root/.cache/pip,sharing=shared \
    python3.11 -m venv /opt/poetry && \
    /opt/poetry/bin/python -m pip install --upgrade pip setuptools wheel && \
    /opt/poetry/bin/python -m pip install "poetry==${POETRY_VERSION}" && \
    VIRTUAL_ENV=/venv /opt/poetry/bin/poetry \
      --directory /tmp/dumb-dev-dependencies \
      config virtualenvs.create false && \
    VIRTUAL_ENV=/venv /opt/poetry/bin/poetry \
      --directory /tmp/dumb-dev-dependencies \
      install --no-root --with dev --no-interaction && \
    /venv/bin/python -m pip install --upgrade --force-reinstall \
      "cffi>=1.16,<3.0" "cryptography>=48.0.1,<49.0.0" && \
    rm -rf /tmp/dumb-dev-dependencies

ENV PATH="/opt/poetry/bin:/venv/bin:${PATH}" \
    VIRTUAL_ENV=/venv

RUN poetry --version && \
    /venv/bin/python -m pre_commit --version && \
    /venv/bin/python -m black --version && \
    /venv/bin/python -m ruff --version
