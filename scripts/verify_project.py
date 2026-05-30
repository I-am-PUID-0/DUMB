#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def check_pyproject() -> None:
    pyproject_path = ROOT / "pyproject.toml"
    with pyproject_path.open("rb") as handle:
        data = tomllib.load(handle)
    project = data.get("project", {})
    if project.get("name") != "DUMB":
        fail("pyproject.toml project.name must be DUMB")
    if project.get("requires-python") != ">=3.11,<4.0":
        fail(
            "pyproject.toml requires-python must remain >=3.11,<4.0 unless CI matrix is updated"
        )
    if not project.get("version"):
        fail(
            "pyproject.toml project.version is required for runtime and release automation"
        )


def check_release_manifest() -> None:
    pyproject_path = ROOT / "pyproject.toml"
    with pyproject_path.open("rb") as handle:
        project_version = tomllib.load(handle).get("project", {}).get("version")
    manifest_path = ROOT / ".github" / ".release-please-manifest.json"
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get(".") != project_version:
        fail(
            "release-please manifest version must match pyproject.toml project.version"
        )


def check_json_files() -> None:
    for relative in ("utils/dumb_config.json", "utils/dumb_config_schema.json"):
        path = ROOT / relative
        with path.open("r", encoding="utf-8") as handle:
            json.load(handle)


def check_env_example() -> None:
    generator_path = ROOT / "scripts" / "generate_env_example.py"
    spec = importlib.util.spec_from_file_location(
        "generate_env_example", generator_path
    )
    if spec is None or spec.loader is None:
        fail("unable to load scripts/generate_env_example.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with module.CONFIG_PATH.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    expected = module.generate_env_example(config)
    current = module.ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    if current != expected:
        fail(
            ".env.example is out of date; run `poetry run python scripts/generate_env_example.py`"
        )


def check_dockerignore_required_patterns() -> None:
    required_patterns = {
        ".git",
        ".github",
        ".env",
        "config/",
        "log/",
        "logs/",
        "__pycache__/",
        "*.py[cod]",
        ".ruff_cache/",
        ".venv/",
        "venv/",
    }
    dockerignore_path = ROOT / ".dockerignore"
    patterns = {
        line.strip()
        for line in dockerignore_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing = sorted(required_patterns - patterns)
    if missing:
        fail(".dockerignore missing required patterns: " + ", ".join(missing))


def check_workflow_permissions() -> None:
    workflow_dir = ROOT / ".github" / "workflows"
    missing = []
    for path in sorted(workflow_dir.glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        if "permissions:" not in text:
            missing.append(path.relative_to(ROOT).as_posix())
    if missing:
        fail("workflow files missing explicit permissions: " + ", ".join(missing))


def check_tests_are_importable_package() -> None:
    if not (ROOT / "tests" / "__init__.py").exists():
        fail("tests/__init__.py is required for predictable unittest discovery")


def main() -> None:
    check_pyproject()
    check_json_files()
    check_release_manifest()
    check_env_example()
    check_dockerignore_required_patterns()
    check_workflow_permissions()
    check_tests_are_importable_package()
    print("project metadata ok")


if __name__ == "__main__":
    main()
