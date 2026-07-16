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
    explicit_name="${GIT_USER_NAME:-}"
    explicit_email="${GIT_USER_EMAIL:-}"
    local_name="$(git config --local --get user.name || true)"
    local_email="$(git config --local --get user.email || true)"
    global_name="$(git config --global --get user.name || true)"
    global_email="$(git config --global --get user.email || true)"

    if [[ -n "${explicit_name}" || -n "${explicit_email}" ]]; then
        if [[ -z "${explicit_name}" || -z "${explicit_email}" ]]; then
            printf '%s\n' \
                'Git identity was not changed: set both GIT_USER_NAME and GIT_USER_EMAIL.' >&2
        else
            git config --global user.name "${explicit_name}"
            git config --global user.email "${explicit_email}"
            git config --local user.name "${explicit_name}"
            git config --local user.email "${explicit_email}"
        fi
    elif [[ -n "${local_name}" && -n "${local_email}" ]]; then
        : # The bind-mounted repository already has a durable maintainer identity.
    elif [[ -n "${global_name}" && -n "${global_email}" ]]; then
        # Preserve the detected maintainer identity with this bind-mounted clone
        # so it survives a devcontainer rebuild even when /root/.gitconfig does not.
        git config --local user.name "${global_name}"
        git config --local user.email "${global_email}"
    else
        printf '%s\n' \
            'Git identity is not configured. Set it once with git config --local user.name/user.email, or provide GIT_USER_NAME and GIT_USER_EMAIL.' >&2
    fi
    poetry run pre-commit install
    poetry run pre-commit install --hook-type pre-push
fi
