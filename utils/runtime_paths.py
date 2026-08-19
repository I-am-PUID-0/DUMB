"""Resolve files that belong to the DUMB controller source tree.

The container image installs the controller at ``/``. Native deployments can
install the same source under a conventional application directory and set
``DUMB_PROJECT_ROOT``. When the variable is unset, deriving the root from this
module keeps the existing container layout unchanged and makes local checkouts
use their own files instead of stale image-layer copies.
"""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    configured_root = str(os.environ.get("DUMB_PROJECT_ROOT") or "").strip()
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def project_path(*parts: str) -> Path:
    return project_root().joinpath(*parts)


def healthcheck_script() -> str:
    return str(project_path("healthcheck.py"))


def pyproject_file() -> str:
    return str(project_path("pyproject.toml"))


def default_config_file() -> str:
    return str(project_path("utils", "dumb_config.json"))


def default_schema_file() -> str:
    return str(project_path("utils", "dumb_config_schema.json"))
