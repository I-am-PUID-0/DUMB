from __future__ import annotations

import tomllib


def get_project_version(path: str = "pyproject.toml", default: str = "0.0.0") -> str:
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except Exception:
        return default

    version = data.get("project", {}).get("version")
    if version:
        return str(version)

    version = data.get("tool", {}).get("poetry", {}).get("version")
    if version:
        return str(version)

    return default
