#!/usr/bin/env bash
set -euo pipefail

workspace="${1:-/workspace}"
cd "${workspace}"

export PATH="/opt/poetry/bin:/venv/bin:${PATH}"
export VIRTUAL_ENV=/venv

poetry --version
/venv/bin/python -m pre_commit --version
ln -sfn /opt/poetry/bin/poetry /usr/local/bin/poetry

if ! git config --global --get-all safe.directory | grep -Fxq "${workspace}"; then
    git config --global --add safe.directory "${workspace}"
fi

activation='. /venv/bin/activate'
touch /root/.bashrc
if ! grep -Fxq "${activation}" /root/.bashrc; then
    printf '\n%s\n' "${activation}" >> /root/.bashrc
fi

if git rev-parse --git-dir >/dev/null 2>&1; then
    poetry run pre-commit install
    poetry run pre-commit install --hook-type pre-push
fi
