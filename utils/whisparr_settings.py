"""Whisparr family selection and read-only database/runtime safety checks."""

from pathlib import Path
import re

import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException


class WhisparrConfigurationError(ValueError):
    pass


_DATABASE_FILES = {"v2": ("whisparr.db", "whisparr2.db"), "v3": ("whisparr3.db",)}
_CORE_VERSION = re.compile(rb"Whisparr\.Common, Version=(\d+\.\d+\.\d+\.\d+)")
_MAX_CORE_BYTES = 32 * 1024 * 1024


def normalize_whisparr_release_version(value):
    """Compare published release tags with the numeric version in Core.dll."""
    match = re.fullmatch(
        r"v?(\d+\.\d+\.\d+)(?:\.(\d+)|-(?:release|develop)\.(\d+))?",
        str(value or "").strip(),
        re.IGNORECASE,
    )
    if not match:
        return None
    revision = match.group(2) or match.group(3)
    return match.group(1) + (f".{revision}" if revision else "")


def read_whisparr_version(binary_path):
    """Read adjacent assembly metadata without executing the installed app."""
    if not binary_path:
        return None
    core_path = Path(binary_path).with_name("Whisparr.Core.dll")
    try:
        with core_path.open("rb") as handle:
            data = handle.read(_MAX_CORE_BYTES + 1)
    except OSError:
        return None
    if len(data) > _MAX_CORE_BYTES:
        return None
    versions = {match.decode("ascii") for match in _CORE_VERSION.findall(data)}
    return next(iter(versions)) if len(versions) == 1 else None


def _configured_binary(config):
    command = config.get("command") or []
    if isinstance(command, list):
        for argument in command[:2]:
            if isinstance(argument, str) and Path(argument).name in {
                "Whisparr",
                "Whisparr.dll",
            }:
                return argument
    install_dir = config.get("install_dir")
    if install_dir:
        for relative in ("Whisparr/Whisparr", "Whisparr"):
            candidate = Path(install_dir) / relative
            if candidate.is_file():
                return str(candidate)
    return None


def _database_artifacts(root, name):
    return any(
        path.exists() or path.is_symlink()
        for path in (
            *(root / (name + suffix) for suffix in ("", "-wal", "-shm", "-journal")),
            root / name.replace(".db", ".restore"),
        )
    )


def legacy_whisparr_database(config):
    if config.get("postgres_enabled") or not config.get("config_dir"):
        return False
    root = Path(config["config_dir"])
    return _database_artifacts(root, "whisparr.db") and not _database_artifacts(
        root, "whisparr2.db"
    )


def _sqlite_families(config):
    # Explicit PostgreSQL instances may retain inactive SQLite files. Those
    # files do not identify the live database provider or its schema.
    if config.get("postgres_enabled") or not config.get("config_dir"):
        return set()
    root = Path(config["config_dir"])
    families = set()
    for family, names in _DATABASE_FILES.items():
        for name in names:
            if _database_artifacts(root, name):
                families.add(family)
    if _database_artifacts(root, "whisparr.db") and _database_artifacts(
        root, "whisparr2.db"
    ):
        raise WhisparrConfigurationError(
            "Both legacy whisparr.db and current whisparr2.db artifacts exist. "
            "Resolve which v2 library to use in a separate data directory before "
            "installing or starting Whisparr; DUMB will not merge or remove them."
        )
    return families


def _legacy_branch(config):
    branch = str(config.get("branch") or "").strip().lower()
    if branch and branch != "main":
        return branch
    config_file = config.get("config_file")
    if not config_file and config.get("config_dir"):
        config_file = str(Path(config["config_dir"]) / "config.xml")
    if config_file:
        try:
            with open(config_file, "rb") as handle:
                content = handle.read(65537)
            if len(content) <= 65536:
                return str(ET.fromstring(content).findtext("Branch") or "").lower()
        except (OSError, ET.ParseError, ValueError, DefusedXmlException):
            pass
    return ""


def resolve_whisparr_version(config):
    """An empty selector preserves existing data; a new empty instance uses v2."""
    selected = config.get("whisparr_version", "")
    if selected not in ("", "v2", "v3"):
        raise WhisparrConfigurationError(
            "whisparr_version must be empty (detect existing version), v2, or v3."
        )
    families = _sqlite_families(config)
    if len(families) > 1:
        raise WhisparrConfigurationError(
            "Both Whisparr v2 and v3 databases or recovery files exist in config_dir. "
            "Use a separate data directory for each version; DUMB will not convert "
            "or remove either database."
        )
    existing = next(iter(families), None)
    if selected and existing and selected != existing:
        raise WhisparrConfigurationError(
            f"Whisparr {selected} cannot use the existing {existing} database in "
            "config_dir. Select the matching version or create a separate instance "
            "with its own data directory; DUMB does not convert these databases."
        )
    installed = (
        read_whisparr_version(_configured_binary(config))
        if config.get("postgres_enabled") or (not selected and not existing)
        else None
    )
    installed_major = installed.split(".")[0] if installed else None
    installed_family = f"v{installed_major}" if installed_major in {"2", "3"} else None
    if (
        selected
        and config.get("postgres_enabled")
        and installed_family
        and selected != installed_family
    ):
        raise WhisparrConfigurationError(
            "Changing the Whisparr major version on an existing PostgreSQL instance "
            "requires a separate instance and databases; DUMB does not convert "
            "Whisparr database schemas."
        )
    if not selected:
        branch = _legacy_branch(config)
        eros_repo = (
            str(config.get("repo_owner") or "").lower() == "whisparr"
            and str(config.get("repo_name") or "").lower() == "whisparr-eros"
        )
        selected = (
            existing
            or installed_family
            or ("v3" if branch in {"eros", "eros-develop"} or eros_repo else "v2")
        )
    if str(config.get("commit_sha") or "").strip():
        raise WhisparrConfigurationError(
            "Whisparr source/commit builds are not supported. Clear commit_sha and "
            "select a published release with whisparr_version and pinned_version."
        )
    for field in ("pinned_version", "release_version"):
        if (field == "release_version") != bool(config.get("release_version_enabled")):
            continue
        selector = str(config.get(field) or "").strip()
        if not selector or (
            field == "release_version"
            and selector.lower() in {"latest", "nightly", "prerelease"}
        ):
            continue
        version = normalize_whisparr_release_version(selector)
        if not version or version.split(".")[0] != selected[1:]:
            raise WhisparrConfigurationError(
                f"{field}={selector!r} is not a published Whisparr {selected} "
                "version. Use a matching release tag or numeric version; an Eros "
                "branch name is not a release tag."
            )
    return selected


def whisparr_repository(config):
    family = resolve_whisparr_version(config)
    owner = str(config.get("repo_owner") or "Whisparr").strip()
    repo = str(config.get("repo_name") or "Whisparr").strip()
    if owner.lower() == "whisparr" and repo.lower() in {"whisparr", "whisparr-eros"}:
        return "Whisparr", "Whisparr-Eros" if family == "v3" else "Whisparr"
    return owner, repo


def whisparr_branch(config):
    family = resolve_whisparr_version(config)
    branch = _legacy_branch(config)
    if family == "v3":
        return "eros-develop" if branch == "eros-develop" else "eros"
    return "v2-develop" if branch in {"nightly", "develop", "v2-develop"} else "v2"


def whisparr_release_selector(config):
    resolve_whisparr_version(config)
    if config.get("release_version_enabled"):
        release = str(config.get("release_version") or "latest").strip() or "latest"
        if release.lower() in {"latest", "nightly", "prerelease"}:
            return "prerelease" if release.lower() == "nightly" else release.lower()
        return release
    pinned = str(config.get("pinned_version") or "").strip()
    if pinned:
        return pinned
    return "prerelease" if whisparr_branch(config).endswith("-develop") else "latest"


def validate_whisparr_runtime(config, binary_path):
    family = resolve_whisparr_version(config)
    installed = read_whisparr_version(binary_path)
    if not installed:
        raise WhisparrConfigurationError(
            "Cannot verify the Whisparr runtime version from its adjacent "
            "Whisparr.Core.dll. Reinstall the selected published release."
        )
    if installed.split(".")[0] != family[1:]:
        raise WhisparrConfigurationError(
            f"Installed Whisparr {installed} does not match selected {family}. "
            "Reinstall the matching release before starting this instance."
        )
    if (
        family == "v2"
        and not config.get("postgres_enabled")
        and config.get("config_dir")
    ):
        root = Path(config["config_dir"])
        modern_runtime = tuple(int(part) for part in installed.split(".")) >= (
            2,
            2,
            0,
            0,
        )
        if modern_runtime and legacy_whisparr_database(config):
            raise WhisparrConfigurationError(
                "This Whisparr v2 release uses whisparr2.db and does not automatically "
                "migrate the existing whisparr.db library or its recovery files. "
                "Keep the working legacy runtime and prepare a separately backed-up, "
                "validated database migration before upgrading. DUMB has left the "
                "existing database unchanged."
            )
        if not modern_runtime and _database_artifacts(root, "whisparr2.db"):
            raise WhisparrConfigurationError(
                "This older Whisparr v2 runtime uses whisparr.db, but the existing "
                "library uses whisparr2.db. Install the matching current v2 release; "
                "DUMB will not start an empty legacy database."
            )


def whisparr_database_file(config):
    if resolve_whisparr_version(config) == "v3":
        return "whisparr3.db"
    root = Path(config.get("config_dir") or ".")
    return (
        "whisparr.db"
        if (root / "whisparr.db").exists() and not (root / "whisparr2.db").exists()
        else "whisparr2.db"
    )
