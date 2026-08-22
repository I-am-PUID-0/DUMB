"""PostgreSQL configuration helpers for non-Arr dual-backend services."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sqlite3
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import yaml

from utils.download import Downloader
from utils.global_logger import logger
from utils.infinidysk_postgres_contracts import (
    INFINIDYSK_DATABASE_CONTRACTS as _INFINIDYSK_DATABASE_CONTRACTS,
    INFINIDYSK_TRANSIENT_SCHEMA_OBJECTS,
)
from utils.private_files import atomic_write_private_text

SERVICE_POSTGRES_KEYS = ("altmount", "bazarr", "infinidysk", "pulsarr", "seerr")
_INFINIDYSK_MIGRATION_CONTEXT = threading.local()
_INFINIDYSK_MIGRATION_ROOT = Path("/config/arr-postgres-migration")
_INFINIDYSK_AUTHORIZATION_FILENAME = "infinidysk.json"
_INFINIDYSK_AUTHORIZATION_FORMAT = "dumb.infinidysk-postgres-cutover"
_INFINIDYSK_AUTHORIZATION_VERSION = 1
_INFINIDYSK_POSTGRES_MIN_VERSION = (1, 2, 0)
_INFINIDYSK_POSTGRES_BASELINE_COMMIT = "8c960ffc39fc85fdf9166aafd6cb2846878ec3c2"
_INFINIDYSK_TABLE_COUNT = 23
_INFINIDYSK_FOREIGN_KEY_COUNT = 4
_INFINIDYSK_IDENTITY_COUNT = 2
_MAX_MIGRATION_RECORD_BYTES = 2 * 1024 * 1024
_MAX_AUTHORIZATION_BYTES = 16 * 1024
_MIGRATION_JOB_ID_RE = re.compile(r"[0-9a-f]{32}")
_INFINIDYSK_STABLE_RELEASE_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$", re.IGNORECASE)
_INFINIDYSK_INSTALLED_RELEASE_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-[0-9a-f]{8,40})?$", re.IGNORECASE
)
_INFINIDYSK_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_INFINIDYSK_INSTALL_STATE_FILENAME = ".dumb_infinidysk_install.json"


def service_postgres_enabled(service: dict | None) -> bool:
    return isinstance(service, dict) and service.get("postgres_enabled") is True


def infinidysk_launch_config_fingerprint(service: dict | None) -> str:
    """Hash the non-secret DUMB launch contract used by rehearsal and restart."""

    service = service if isinstance(service, dict) else {}
    command = service.get("command") or []
    if isinstance(command, str):
        try:
            command = shlex.split(command)
        except ValueError:
            command = [command.strip()]
    elif isinstance(command, (list, tuple)):
        command = [str(item) for item in command]
    else:
        command = [str(command)]
    config_dir = os.path.realpath(str(service.get("config_dir") or "/infinidysk"))
    env = service.get("env") if isinstance(service.get("env"), dict) else {}
    selected_env = {
        key: str(env.get(key) or "")
        for key in (
            "ASPNETCORE_URLS",
            "BACKEND_URL",
            "CONFIG_PATH",
            "FRONTEND_URL",
            "PORT",
        )
    }
    identity = {
        "command": command,
        "config_dir": config_dir,
        "backend_output_dir": os.path.realpath(
            str(service.get("backend_output_dir") or os.path.join(config_dir, "app"))
        ),
        "backend_port": str(service.get("backend_port") or ""),
        "frontend_port": str(service.get("frontend_port") or ""),
        "wait_for_url": str(service.get("wait_for_url") or ""),
        "env": selected_env,
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _infinidysk_postgres_selected(service: dict | None) -> bool:
    if not isinstance(service, dict):
        return False
    provider = str((service.get("env") or {}).get("DATABASE_PROVIDER") or "")
    return service_postgres_enabled(service) or provider.strip().lower() in {
        "postgres",
        "postgresql",
    }


def _infinidysk_postgres_version_error(
    selection: str, *, post_cutover: bool = False
) -> str:
    if post_cutover:
        return (
            "InfiniDysk PostgreSQL requires the selected runtime to be the "
            "cutover runtime or one of its descendants. "
            f"The selected or installed runtime ({selection}) could not be proven "
            "compatible. Select an official release, branch, or exact commit at "
            "or after the recorded PostgreSQL cutover commit. Older, diverged, or "
            "unverifiable targets are blocked while PostgreSQL is selected."
        )
    return (
        "InfiniDysk PostgreSQL requires a stable v1.2.0-or-newer runtime. "
        f"The selected or installed runtime ({selection}) cannot be proven safe. "
        "Select latest, a stable v1.2.0+ release, or the exact official v1.2.0 "
        "commit before starting PostgreSQL. Mutable branches, prereleases, and "
        "unverified commits are blocked while PostgreSQL is selected."
    )


def _infinidysk_stable_version(
    value: object, *, installed_marker: bool = False
) -> tuple[int, int, int] | None:
    pattern = (
        _INFINIDYSK_INSTALLED_RELEASE_RE
        if installed_marker
        else _INFINIDYSK_STABLE_RELEASE_RE
    )
    match = pattern.fullmatch(str(value or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups()[:3])


def validate_infinidysk_postgres_release_version(
    release: object,
) -> tuple[bool, str | None]:
    """Require a resolved stable release at or above InfiniDysk's PG floor."""

    value = str(release or "").strip()
    version = _infinidysk_stable_version(value)
    if version is None or version < _INFINIDYSK_POSTGRES_MIN_VERSION:
        return False, _infinidysk_postgres_version_error(value or "missing")
    return True, None


def validate_infinidysk_postgres_source_selection(
    service: dict | None,
    *,
    minimum_commit: str | None = None,
    downloader: Downloader | None = None,
) -> tuple[bool, str | None]:
    """Reject source selectors that cannot prove the PostgreSQL runtime floor."""

    if not _infinidysk_postgres_selected(service):
        return True, None
    service = service or {}
    minimum_commit = str(minimum_commit or "").strip().lower()
    if minimum_commit:
        if not _INFINIDYSK_COMMIT_RE.fullmatch(minimum_commit):
            return False, _infinidysk_postgres_version_error(
                "invalid cutover authorization", post_cutover=True
            )
        repository = (
            str(service.get("repo_owner") or "").strip().lower(),
            str(service.get("repo_name") or "").strip().lower(),
        )
        if repository != ("infinidysk", "infinidysk"):
            selected = "/".join(repository) if all(repository) else "missing repository"
            return False, (
                "InfiniDysk PostgreSQL post-cutover runtimes must come from the "
                "official infinidysk/infinidysk repository so DUMB can prove "
                f"commit ancestry. The selected repository ({selected}) cannot "
                "be verified against the recorded cutover commit."
            )
        target_commit, selection, resolve_error = _infinidysk_selected_runtime_commit(
            service, downloader=downloader
        )
        if not target_commit:
            return False, _infinidysk_postgres_version_error(
                f"{selection}: {resolve_error or 'commit resolution failed'}",
                post_cutover=True,
            )
        compatible, ancestry_error = _infinidysk_commit_is_descendant(
            minimum_commit,
            target_commit,
            downloader=downloader,
        )
        if not compatible:
            return False, _infinidysk_postgres_version_error(
                f"{selection} ({target_commit[:12]}): "
                f"{ancestry_error or 'not descended from the cutover commit'}",
                post_cutover=True,
            )
        return True, None

    commit_sha = str(service.get("commit_sha") or "").strip().lower()
    if commit_sha:
        if commit_sha == _INFINIDYSK_POSTGRES_BASELINE_COMMIT:
            return True, None
        return False, _infinidysk_postgres_version_error(
            f"commit {commit_sha[:12] or 'missing'}"
        )
    if service.get("branch_enabled") is True:
        branch = str(service.get("branch") or "main").strip() or "main"
        return False, _infinidysk_postgres_version_error(f"branch {branch}")
    repository = (
        str(service.get("repo_owner") or "").strip().lower(),
        str(service.get("repo_name") or "").strip().lower(),
    )
    if repository != ("infinidysk", "infinidysk"):
        selected = "/".join(repository) if all(repository) else "missing repository"
        return False, (
            "InfiniDysk PostgreSQL release runtimes must come from the official "
            "infinidysk/infinidysk repository. The selected repository "
            f"({selected}) cannot prove the audited v1.2.0-or-newer runtime. "
            "Use the official release source or the exact audited v1.2.0 commit."
        )
    release = (
        str(service.get("release_version") or "").strip()
        if service.get("release_version_enabled") is True
        else "latest"
    )
    release = release or "latest"
    if release.lower() == "latest":
        return True, None
    return validate_infinidysk_postgres_release_version(release)


def _infinidysk_selected_runtime_commit(
    service: dict,
    *,
    downloader: Downloader | None = None,
) -> tuple[str | None, str, str | None]:
    """Resolve the selected official release, branch, or exact commit."""

    resolver = downloader or Downloader()
    commit_sha = str(service.get("commit_sha") or "").strip().lower()
    if commit_sha:
        if not _INFINIDYSK_COMMIT_RE.fullmatch(commit_sha):
            return None, f"commit {commit_sha[:12] or 'missing'}", "invalid full SHA"
        return commit_sha, f"commit {commit_sha[:12]}", None
    if service.get("branch_enabled") is True:
        ref = str(service.get("branch") or "main").strip() or "main"
        selection = f"branch {ref}"
    else:
        ref = (
            str(service.get("release_version") or "").strip()
            if service.get("release_version_enabled") is True
            else "latest"
        )
        ref = ref or "latest"
        selection = f"release {ref}"
        if ref.lower() == "latest":
            ref, error = resolver.get_latest_release("infinidysk", "infinidysk")
            if not ref:
                return None, selection, error or "latest release lookup failed"
    commit, error = resolver.get_ref_commit_sha("infinidysk", "infinidysk", ref)
    if not commit or not _INFINIDYSK_COMMIT_RE.fullmatch(str(commit).lower()):
        return None, selection, error or "commit lookup failed"
    return str(commit).lower(), selection, None


def _infinidysk_commit_is_descendant(
    minimum_commit: str,
    target_commit: str,
    *,
    downloader: Downloader | None = None,
) -> tuple[bool, str | None]:
    """Use GitHub's compare graph to prove target == or descends from minimum."""

    if target_commit == minimum_commit:
        return True, None
    resolver = downloader or Downloader()
    url = (
        "https://api.github.com/repos/infinidysk/infinidysk/compare/"
        f"{minimum_commit}...{target_commit}"
    )
    response = resolver.fetch_with_retries(url, resolver.get_headers())
    if response is None or response.status_code != 200:
        status = response.status_code if response is not None else "no_response"
        return False, f"GitHub ancestry lookup failed with status {status}"
    payload = response.json() if hasattr(response, "json") else {}
    status = str((payload or {}).get("status") or "").strip().lower()
    if status in {"ahead", "identical"}:
        return True, None
    if status in {"behind", "diverged"}:
        return False, f"target is {status} relative to the cutover commit"
    return False, "GitHub returned an unknown ancestry result"


def infinidysk_installed_runtime_commit(
    config_dir: str | os.PathLike[str],
) -> str | None:
    """Read DUMB's bounded install provenance and bind it to version.txt."""

    root = Path(os.path.realpath(os.fspath(config_dir)))
    state = _read_bounded_private_json(
        root / _INFINIDYSK_INSTALL_STATE_FILENAME,
        16 * 1024,
        require_controller_owner=False,
    )
    commit = str((state or {}).get("source_commit") or "").strip().lower()
    if not _INFINIDYSK_COMMIT_RE.fullmatch(commit):
        return None
    try:
        marker = (root / "version.txt").read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    if commit[:8] not in marker and commit[:12] not in marker:
        return None
    return commit


def validate_infinidysk_postgres_installed_version(
    config_dir: str | os.PathLike[str],
    *,
    minimum_commit: str | None = None,
    downloader: Downloader | None = None,
) -> tuple[bool, str | None]:
    """Validate the bounded installed marker before a PostgreSQL-backed start."""

    version_path = Path(os.path.realpath(os.fspath(config_dir))) / "version.txt"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(version_path, flags)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size < 1
                or info.st_size > 256
            ):
                raise ValueError("version marker is not a bounded private file")
            raw = os.read(descriptor, 257)
        finally:
            os.close(descriptor)
        if len(raw) > 256:
            raise ValueError("version marker is too large")
        marker = raw.decode("utf-8").strip()
    except (OSError, UnicodeError, ValueError) as error:
        return False, _infinidysk_postgres_version_error(
            f"unreadable version marker: {error}"
        )

    minimum_commit = str(minimum_commit or "").strip().lower()
    if minimum_commit:
        installed_commit = infinidysk_installed_runtime_commit(config_dir)
        if not installed_commit:
            return False, _infinidysk_postgres_version_error(
                f"{marker or 'missing'}: installed commit provenance is unavailable",
                post_cutover=True,
            )
        compatible, ancestry_error = _infinidysk_commit_is_descendant(
            minimum_commit,
            installed_commit,
            downloader=downloader,
        )
        if compatible:
            return True, None
        return False, _infinidysk_postgres_version_error(
            f"{marker} ({installed_commit[:12]}): {ancestry_error}",
            post_cutover=True,
        )

    if marker.lower() == f"commit-{_INFINIDYSK_POSTGRES_BASELINE_COMMIT[:12]}":
        return True, None
    version = _infinidysk_stable_version(marker, installed_marker=True)
    if version is None or version < _INFINIDYSK_POSTGRES_MIN_VERSION:
        return False, _infinidysk_postgres_version_error(marker or "missing")
    return True, None


def _postgres_target_fingerprint(postgres_config: dict, database: str) -> str:
    identity = {
        "host": str(postgres_config.get("host") or "127.0.0.1"),
        "port": int(postgres_config.get("port") or 5432),
        "user": str(postgres_config.get("user") or "DUMB"),
        "database": str(database),
        "config_dir": os.path.realpath(
            str(postgres_config.get("config_dir") or "/postgres_data")
        ),
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def infinidysk_postgres_physical_identity(
    postgres_config: dict, database: str
) -> dict[str, str] | None:
    """Read the live cluster/database identity without exposing credentials."""

    try:
        import psycopg2

        connection = psycopg2.connect(
            host=str(postgres_config.get("host") or "127.0.0.1"),
            port=int(postgres_config.get("port") or 5432),
            user=str(postgres_config.get("user") or "DUMB"),
            password=str(postgres_config.get("password") or "postgres"),
            dbname=str(database),
            connect_timeout=5,
            application_name="dumb-infinidysk-authorization",
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT system_identifier::text FROM pg_control_system()"
                )
                system_row = cursor.fetchone()
                cursor.execute(
                    "SELECT oid::text FROM pg_database WHERE datname = current_database()"
                )
                database_row = cursor.fetchone()
        finally:
            connection.close()
    except Exception:
        return None
    system_identifier = str((system_row or [""])[0] or "").strip()
    database_oid = str((database_row or [""])[0] or "").strip()
    if not system_identifier.isdigit() or not database_oid.isdigit():
        return None
    return {
        "system_identifier": system_identifier,
        "database_oid": database_oid,
    }


def infinidysk_sqlite_source_path_fingerprint(path: str | os.PathLike[str]) -> str:
    """Return a non-reversible binding for the canonical InfiniDysk main DB path."""

    canonical = os.path.realpath(os.fspath(path))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _npgsql_connection_targets_runtime(
    value: str,
    postgres_config: dict,
    database: str,
) -> bool:
    """Compare non-secret ADO.NET target fields while allowing password rotation."""

    if not isinstance(value, str) or not value or len(value) > 16 * 1024:
        return False
    aliases = {
        "host": "host",
        "server": "host",
        "data source": "host",
        "port": "port",
        "database": "database",
        "initial catalog": "database",
        "username": "user",
        "user id": "user",
        "userid": "user",
        "user": "user",
        "password": "password",
        "pwd": "password",
    }
    parsed: dict[str, str] = {}
    current: list[str] = []
    segments: list[str] = []
    quote_character: str | None = None
    index = 0
    while index < len(value):
        character = value[index]
        if quote_character:
            if character == quote_character:
                if index + 1 < len(value) and value[index + 1] == quote_character:
                    current.append(character)
                    index += 2
                    continue
                quote_character = None
            else:
                current.append(character)
        elif character in {'"', "'"}:
            quote_character = character
        elif character == ";":
            segments.append("".join(current))
            current = []
        else:
            current.append(character)
        index += 1
    if quote_character:
        return False
    segments.append("".join(current))
    for segment in segments:
        name, separator, raw_value = segment.partition("=")
        if not separator:
            if segment.strip():
                return False
            continue
        canonical = aliases.get(name.strip().lower())
        if canonical:
            if canonical in parsed:
                return False
            parsed[canonical] = raw_value.strip()
    expected = {
        "host": str(postgres_config.get("host") or "127.0.0.1"),
        "port": str(int(postgres_config.get("port") or 5432)),
        "database": database,
        "user": str(postgres_config.get("user") or "DUMB"),
    }
    return all(
        parsed.get(name) == expected_value for name, expected_value in expected.items()
    )


def _read_bounded_private_json(
    path: Path,
    maximum_bytes: int,
    *,
    require_controller_owner: bool = True,
) -> dict | None:
    """Read one controller record without following links or unbounded input."""

    try:
        path_stat = path.lstat()
    except (FileNotFoundError, OSError):
        return None
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or path_stat.st_nlink != 1
        or path_stat.st_size < 1
        or path_stat.st_size > maximum_bytes
        or stat.S_IMODE(path_stat.st_mode) & 0o077
        or (
            require_controller_owner
            and (path_stat.st_uid, path_stat.st_gid) != (os.geteuid(), os.getegid())
        )
    ):
        return None

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        opened_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_nlink != 1
            or (opened_stat.st_dev, opened_stat.st_ino)
            != (path_stat.st_dev, path_stat.st_ino)
            or opened_stat.st_size > maximum_bytes
            or (
                require_controller_owner
                and (opened_stat.st_uid, opened_stat.st_gid)
                != (os.geteuid(), os.getegid())
            )
        ):
            return None
        chunks = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw_payload = b"".join(chunks)
        if len(raw_payload) > maximum_bytes:
            return None
    finally:
        os.close(descriptor)

    try:
        final_stat = path.lstat()
        if (final_stat.st_dev, final_stat.st_ino) != (
            path_stat.st_dev,
            path_stat.st_ino,
        ):
            return None
        payload = json.loads(raw_payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _direct_regular_file(path: Path) -> os.stat_result | None:
    try:
        path_stat = path.lstat()
    except (FileNotFoundError, OSError):
        return None
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or path_stat.st_nlink != 1
    ):
        return None
    return path_stat


def _sqlite_source_fingerprints(path: Path) -> tuple[str, str] | None:
    path_stat = _direct_regular_file(path)
    if path_stat is None or path_stat.st_size < 1:
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        try:
            schema_rows = connection.execute(
                "SELECT type, name, tbl_name, COALESCE(sql, '') "
                "FROM sqlite_master WHERE name <> '__EFMigrationsLock' "
                "AND name NOT GLOB 'sqlite_stat*' "
                "ORDER BY type, name, tbl_name"
            ).fetchall()
            schema_rows = [
                row
                for row in schema_rows
                if str(row[1]) not in INFINIDYSK_TRANSIENT_SCHEMA_OBJECTS
                and str(row[2]) not in INFINIDYSK_TRANSIENT_SCHEMA_OBJECTS
            ]
            history_rows = connection.execute(
                'SELECT "MigrationId" FROM "__EFMigrationsHistory" '
                'ORDER BY "MigrationId"'
            ).fetchall()
        finally:
            connection.close()
        final_stat = path.lstat()
    except (OSError, sqlite3.DatabaseError):
        return None
    if (final_stat.st_dev, final_stat.st_ino) != (
        path_stat.st_dev,
        path_stat.st_ino,
    ):
        return None
    schema_payload = json.dumps(schema_rows, separators=(",", ":"), ensure_ascii=False)
    migration_payload = json.dumps(
        [str(row[0]) for row in history_rows],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return (
        hashlib.sha256(schema_payload.encode("utf-8")).hexdigest(),
        hashlib.sha256(migration_payload.encode("utf-8")).hexdigest(),
    )


def _infinidysk_database_contract(
    *,
    adapter_schema: str | None,
    sqlite_schema_fingerprint: str | None,
    sqlite_migration_history_fingerprint: str | None,
    postgres_schema_fingerprint: str | None,
) -> dict | None:
    return next(
        (
            contract
            for contract in _INFINIDYSK_DATABASE_CONTRACTS
            if adapter_schema == contract["adapter_schema"]
            and sqlite_schema_fingerprint == contract["sqlite_schema_fingerprint"]
            and sqlite_migration_history_fingerprint
            == contract["sqlite_migration_history_fingerprint"]
            and postgres_schema_fingerprint == contract["postgres_schema_fingerprint"]
        ),
        None,
    )


def _authorization_from_completed_job(payload: dict) -> dict | None:
    job_id = str(payload.get("job_id") or "").strip().lower()
    binding = payload.get("binding") or {}
    result = payload.get("result") or {}
    main_import = (result.get("imports") or {}).get("main") or {}
    physical_identity = result.get("postgres_physical_identity") or {}
    rehearsal_job_id = str(result.get("rehearsal_job_id") or "").strip().lower()
    database = str(binding.get("postgres_database") or "").strip()
    minimum_runtime_commit = (
        str(binding.get("service_source_commit") or "").strip().lower()
    )
    database_contract = _infinidysk_database_contract(
        adapter_schema=result.get("adapter_schema"),
        sqlite_schema_fingerprint=binding.get("source_schema_fingerprint"),
        sqlite_migration_history_fingerprint=binding.get(
            "source_migration_history_fingerprint"
        ),
        postgres_schema_fingerprint=result.get("postgres_schema_fingerprint"),
    )
    if not _INFINIDYSK_COMMIT_RE.fullmatch(minimum_runtime_commit):
        service_version = str(binding.get("service_version") or "").strip().lower()
        if service_version in {
            "v1.2.0",
            "1.2.0",
            f"v1.2.0-{_INFINIDYSK_POSTGRES_BASELINE_COMMIT[:8]}",
            f"1.2.0-{_INFINIDYSK_POSTGRES_BASELINE_COMMIT[:8]}",
            f"commit-{_INFINIDYSK_POSTGRES_BASELINE_COMMIT[:12]}",
        }:
            minimum_runtime_commit = _INFINIDYSK_POSTGRES_BASELINE_COMMIT
    if not (
        _MIGRATION_JOB_ID_RE.fullmatch(job_id)
        and _MIGRATION_JOB_ID_RE.fullmatch(rehearsal_job_id)
        and str(payload.get("rehearsal_job_id") or "").strip().lower()
        == rehearsal_job_id
        and payload.get("status") == "completed"
        and payload.get("mode") == "cutover"
        and payload.get("service_key") == "infinidysk"
        and str(payload.get("process_name") or "")
        and binding.get("process_name") == payload.get("process_name")
        and result.get("mode") == "cutover"
        and result.get("validated") is True
        and result.get("cutover_performed") is True
        and result.get("application_health_verified") is True
        and result.get("binding") == binding
        and database_contract is not None
        and (
            not binding.get("database_contract")
            or binding.get("database_contract") == database_contract["id"]
        )
        and main_import.get("postgres_schema_fingerprint")
        == database_contract["postgres_schema_fingerprint"]
        and main_import.get("validated") is True
        and main_import.get("tables") == _INFINIDYSK_TABLE_COUNT
        and main_import.get("primary_key_digests_validated") == _INFINIDYSK_TABLE_COUNT
        and main_import.get("full_row_digests_validated") == _INFINIDYSK_TABLE_COUNT
        and main_import.get("foreign_keys_validated") == _INFINIDYSK_FOREIGN_KEY_COUNT
        and main_import.get("sequences_validated") == _INFINIDYSK_IDENTITY_COUNT
        and binding.get("service_key") == "infinidysk"
        and binding.get("source_schema_fingerprint")
        and binding.get("source_path_fingerprint")
        and binding.get("launch_config_fingerprint")
        and binding.get("postgres_target_fingerprint")
        and _INFINIDYSK_COMMIT_RE.fullmatch(minimum_runtime_commit)
        and database
        and result.get("postgres_databases") == [database]
        and str(physical_identity.get("system_identifier") or "").isdigit()
        and str(physical_identity.get("database_oid") or "").isdigit()
    ):
        return None
    return {
        "format": _INFINIDYSK_AUTHORIZATION_FORMAT,
        "version": _INFINIDYSK_AUTHORIZATION_VERSION,
        "job_id": job_id,
        "rehearsal_job_id": rehearsal_job_id,
        "process_name": str(payload.get("process_name") or ""),
        "service_key": "infinidysk",
        "adapter_schema": database_contract["adapter_schema"],
        "database_contract": database_contract["id"],
        "minimum_runtime_commit": minimum_runtime_commit,
        "minimum_runtime_repository": "infinidysk/infinidysk",
        "minimum_runtime_version": str(binding.get("service_version") or ""),
        "source_schema_fingerprint": binding["source_schema_fingerprint"],
        "source_path_fingerprint": binding["source_path_fingerprint"],
        "launch_config_fingerprint": binding["launch_config_fingerprint"],
        "source_migration_history_fingerprint": binding[
            "source_migration_history_fingerprint"
        ],
        "postgres_database": database,
        "postgres_target_fingerprint": binding["postgres_target_fingerprint"],
        "postgres_schema_fingerprint": database_contract["postgres_schema_fingerprint"],
        "postgres_physical_identity": {
            "system_identifier": str(physical_identity["system_identifier"]),
            "database_oid": str(physical_identity["database_oid"]),
        },
        "application_health_verified": True,
    }


def infinidysk_postgres_runtime_floor(
    service: dict | None,
    *,
    migration_root: str | os.PathLike[str] = _INFINIDYSK_MIGRATION_ROOT,
) -> str | None:
    """Return the durable cutover commit floor, upgrading legacy evidence safely."""

    if not isinstance(service, dict):
        return None
    root = Path(migration_root)
    authorization_dir = root / "authorizations"
    path = authorization_dir / _INFINIDYSK_AUTHORIZATION_FILENAME
    try:
        root_stat = root.lstat()
        directory_stat = authorization_dir.lstat()
    except (FileNotFoundError, OSError):
        return None
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
        or (root_stat.st_uid, root_stat.st_gid) != (os.geteuid(), os.getegid())
        or stat.S_IMODE(root_stat.st_mode) != 0o700
        or stat.S_ISLNK(directory_stat.st_mode)
        or not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_dev != root_stat.st_dev
        or os.path.ismount(authorization_dir)
        or (directory_stat.st_uid, directory_stat.st_gid)
        != (os.geteuid(), os.getegid())
        or stat.S_IMODE(directory_stat.st_mode) != 0o700
    ):
        return None
    authorization = _read_bounded_private_json(path, _MAX_AUTHORIZATION_BYTES)
    authorization_contract = _infinidysk_database_contract(
        adapter_schema=(authorization or {}).get("adapter_schema"),
        sqlite_schema_fingerprint=(authorization or {}).get(
            "source_schema_fingerprint"
        ),
        sqlite_migration_history_fingerprint=(authorization or {}).get(
            "source_migration_history_fingerprint"
        ),
        postgres_schema_fingerprint=(authorization or {}).get(
            "postgres_schema_fingerprint"
        ),
    )
    env = service.get("env") if isinstance(service.get("env"), dict) else {}
    config_path = Path(
        os.path.realpath(
            str(env.get("CONFIG_PATH") or service.get("config_dir") or "/infinidysk")
        )
    )
    if not authorization or not (
        authorization.get("format") == _INFINIDYSK_AUTHORIZATION_FORMAT
        and authorization.get("version") == _INFINIDYSK_AUTHORIZATION_VERSION
        and authorization.get("service_key") == "infinidysk"
        and authorization.get("process_name") == str(service.get("process_name") or "")
        and authorization_contract is not None
        and (
            not authorization.get("database_contract")
            or authorization.get("database_contract") == authorization_contract["id"]
        )
        and _MIGRATION_JOB_ID_RE.fullmatch(str(authorization.get("job_id") or ""))
        and authorization.get("launch_config_fingerprint")
        == infinidysk_launch_config_fingerprint(service)
        and authorization.get("source_path_fingerprint")
        == infinidysk_sqlite_source_path_fingerprint(config_path / "db.sqlite")
    ):
        return None
    minimum_commit = (
        str(authorization.get("minimum_runtime_commit") or "").strip().lower()
    )
    if _INFINIDYSK_COMMIT_RE.fullmatch(minimum_commit):
        return minimum_commit

    config_dir = str(service.get("config_dir") or "/infinidysk")
    minimum_commit = infinidysk_installed_runtime_commit(config_dir) or ""
    if not _INFINIDYSK_COMMIT_RE.fullmatch(minimum_commit):
        return None
    upgraded = dict(authorization)
    upgraded.update(
        {
            "minimum_runtime_commit": minimum_commit,
            "minimum_runtime_repository": "infinidysk/infinidysk",
            "minimum_runtime_version": _read_infinidysk_version_marker(config_dir),
        }
    )
    try:
        atomic_write_private_text(
            path,
            json.dumps(upgraded, sort_keys=True, separators=(",", ":")),
        )
    except OSError as error:
        logger.warning(
            "Could not upgrade InfiniDysk PostgreSQL runtime authorization: %s",
            error,
        )
        return None
    return minimum_commit


def _read_infinidysk_version_marker(config_dir: str | os.PathLike[str]) -> str:
    try:
        return (
            Path(os.path.realpath(os.fspath(config_dir)))
            .joinpath("version.txt")
            .read_text(encoding="utf-8")
            .strip()[:256]
        )
    except OSError:
        return ""


def infinidysk_postgres_completed_job_valid(payload: dict) -> bool:
    """Validate the complete guarded-cutover contract without writing a marker."""

    return _authorization_from_completed_job(payload) is not None


def _authorization_matches_runtime(
    authorization: dict,
    service: dict,
    postgres_config: dict,
    source_fingerprints: tuple[str, str],
    *,
    require_live_target: bool = True,
) -> bool:
    database = service_postgres_database_name("infinidysk", None, service)
    live_physical_identity = (
        infinidysk_postgres_physical_identity(postgres_config, database)
        if require_live_target
        else authorization.get("postgres_physical_identity")
    )
    authorization_contract = _infinidysk_database_contract(
        adapter_schema=authorization.get("adapter_schema"),
        sqlite_schema_fingerprint=authorization.get("source_schema_fingerprint"),
        sqlite_migration_history_fingerprint=authorization.get(
            "source_migration_history_fingerprint"
        ),
        postgres_schema_fingerprint=authorization.get("postgres_schema_fingerprint"),
    )
    return bool(
        authorization.get("format") == _INFINIDYSK_AUTHORIZATION_FORMAT
        and authorization.get("version") == _INFINIDYSK_AUTHORIZATION_VERSION
        and _MIGRATION_JOB_ID_RE.fullmatch(str(authorization.get("job_id") or ""))
        and _MIGRATION_JOB_ID_RE.fullmatch(
            str(authorization.get("rehearsal_job_id") or "")
        )
        and authorization.get("service_key") == "infinidysk"
        and authorization.get("process_name") == str(service.get("process_name") or "")
        and authorization_contract is not None
        and (
            not authorization.get("database_contract")
            or authorization.get("database_contract") == authorization_contract["id"]
        )
        and authorization.get("source_schema_fingerprint") == source_fingerprints[0]
        and authorization.get("source_path_fingerprint")
        == infinidysk_sqlite_source_path_fingerprint(
            Path(
                os.path.realpath(
                    str(
                        (service.get("env") or {}).get("CONFIG_PATH")
                        or service.get("config_dir")
                        or "/infinidysk"
                    )
                )
            )
            / "db.sqlite"
        )
        and authorization.get("launch_config_fingerprint")
        == infinidysk_launch_config_fingerprint(service)
        and authorization.get("source_migration_history_fingerprint")
        == source_fingerprints[1]
        == authorization_contract["sqlite_migration_history_fingerprint"]
        and authorization.get("postgres_database") == database
        and authorization.get("postgres_target_fingerprint")
        == _postgres_target_fingerprint(postgres_config, database)
        and authorization.get("postgres_schema_fingerprint")
        == authorization_contract["postgres_schema_fingerprint"]
        and live_physical_identity is not None
        and authorization.get("postgres_physical_identity") == live_physical_identity
        and authorization.get("application_health_verified") is True
    )


def _completed_job_authorization(
    jobs_dir: Path,
    service: dict,
    postgres_config: dict,
    source_fingerprints: tuple[str, str],
    *,
    require_live_target: bool = True,
) -> dict | None:
    try:
        directory_stat = jobs_dir.lstat()
        if (
            stat.S_ISLNK(directory_stat.st_mode)
            or not stat.S_ISDIR(directory_stat.st_mode)
            or (directory_stat.st_uid, directory_stat.st_gid)
            != (os.geteuid(), os.getegid())
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            return None
        entries = sorted(
            jobs_dir.iterdir(),
            key=lambda entry: entry.stat(follow_symlinks=False).st_mtime_ns,
            reverse=True,
        )[:4096]
    except (FileNotFoundError, OSError):
        return None
    for path in entries:
        if not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
            continue
        payload = _read_bounded_private_json(path, _MAX_MIGRATION_RECORD_BYTES)
        if not payload or payload.get("job_id") != path.stem:
            continue
        authorization = _authorization_from_completed_job(payload)
        if authorization and _authorization_matches_runtime(
            authorization,
            service,
            postgres_config,
            source_fingerprints,
            require_live_target=require_live_target,
        ):
            return authorization
    return None


def _ensure_authorization_directory(root: Path) -> Path:
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    root_stat = root.lstat()
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise RuntimeError("Migration authorization root is not a real directory.")
    if (root_stat.st_uid, root_stat.st_gid) != (os.geteuid(), os.getegid()):
        os.chown(root, os.geteuid(), os.getegid())
    root.chmod(0o700)
    authorization_dir = root / "authorizations"
    authorization_dir.mkdir(mode=0o700, exist_ok=True)
    authorization_stat = authorization_dir.lstat()
    if (
        stat.S_ISLNK(authorization_stat.st_mode)
        or not stat.S_ISDIR(authorization_stat.st_mode)
        or authorization_stat.st_dev != root_stat.st_dev
        or os.path.ismount(authorization_dir)
    ):
        raise RuntimeError("Migration authorization storage is unsafe.")
    if (authorization_stat.st_uid, authorization_stat.st_gid) != (
        os.geteuid(),
        os.getegid(),
    ):
        os.chown(authorization_dir, os.geteuid(), os.getegid())
    authorization_dir.chmod(0o700)
    for directory in (root, authorization_dir):
        directory_stat = directory.lstat()
        if (directory_stat.st_uid, directory_stat.st_gid) != (
            os.geteuid(),
            os.getegid(),
        ) or stat.S_IMODE(directory_stat.st_mode) != 0o700:
            raise RuntimeError(
                "Migration authorization storage is not controller-private."
            )
    return authorization_dir


def record_infinidysk_postgres_migration_completion(
    payload: dict,
    *,
    migration_root: str | os.PathLike[str] = _INFINIDYSK_MIGRATION_ROOT,
) -> None:
    """Persist the minimal controller-owned authorization needed after cleanup."""

    authorization = _authorization_from_completed_job(payload)
    if authorization is None:
        raise RuntimeError(
            "Completed InfiniDysk migration did not satisfy the authorization contract."
        )
    authorization_dir = _ensure_authorization_directory(Path(migration_root))
    path = authorization_dir / _INFINIDYSK_AUTHORIZATION_FILENAME
    atomic_write_private_text(
        path,
        json.dumps(authorization, sort_keys=True, separators=(",", ":")),
    )
    path_stat = path.lstat()
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_nlink != 1
        or (path_stat.st_uid, path_stat.st_gid) != (os.geteuid(), os.getegid())
        or stat.S_IMODE(path_stat.st_mode) != 0o600
    ):
        raise RuntimeError("Migration authorization record was not stored privately.")


def clear_infinidysk_postgres_migration_completion(
    *,
    migration_root: str | os.PathLike[str] = _INFINIDYSK_MIGRATION_ROOT,
) -> bool:
    """Prove the InfiniDysk authorization is absent or safely remove it."""

    root = Path(migration_root)
    authorization_dir = root / "authorizations"
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
        or (root_stat.st_uid, root_stat.st_gid) != (os.geteuid(), os.getegid())
        or stat.S_IMODE(root_stat.st_mode) != 0o700
    ):
        logger.warning(
            "Refusing to inspect an InfiniDysk authorization through an unsafe root."
        )
        return False
    try:
        authorization_stat = authorization_dir.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if (
        stat.S_ISLNK(authorization_stat.st_mode)
        or not stat.S_ISDIR(authorization_stat.st_mode)
        or authorization_stat.st_dev != root_stat.st_dev
        or os.path.ismount(authorization_dir)
        or (authorization_stat.st_uid, authorization_stat.st_gid)
        != (os.geteuid(), os.getegid())
        or stat.S_IMODE(authorization_stat.st_mode) != 0o700
    ):
        logger.warning(
            "Refusing to remove an InfiniDysk authorization through unsafe storage."
        )
        return False
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(authorization_dir, flags)
    except OSError:
        return False
    try:
        opened_stat = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(opened_stat.st_mode)
            or (opened_stat.st_dev, opened_stat.st_ino)
            != (authorization_stat.st_dev, authorization_stat.st_ino)
            or (opened_stat.st_uid, opened_stat.st_gid) != (os.geteuid(), os.getegid())
            or stat.S_IMODE(opened_stat.st_mode) != 0o700
        ):
            logger.warning(
                "Refusing to remove an InfiniDysk authorization through changed "
                "storage."
            )
            return False
        try:
            path_stat = os.stat(
                _INFINIDYSK_AUTHORIZATION_FILENAME,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_nlink != 1
            or (path_stat.st_uid, path_stat.st_gid) != (os.geteuid(), os.getegid())
            or stat.S_IMODE(path_stat.st_mode) != 0o600
        ):
            logger.warning(
                "Refusing to remove an unsafe InfiniDysk migration authorization path."
            )
            return False
        try:
            os.unlink(_INFINIDYSK_AUTHORIZATION_FILENAME, dir_fd=directory_fd)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        try:
            os.stat(
                _INFINIDYSK_AUTHORIZATION_FILENAME,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return True
        except OSError:
            return False
        logger.warning("InfiniDysk migration authorization still exists after removal.")
        return False
    finally:
        os.close(directory_fd)


def infinidysk_postgres_cutover_completed(
    service: dict | None,
    postgres_config: dict | None = None,
    *,
    migration_root: str | os.PathLike[str] = _INFINIDYSK_MIGRATION_ROOT,
    require_live_target: bool = True,
) -> bool:
    """Verify controller-owned evidence for the exact preserved SQLite source."""

    if not isinstance(service, dict) or not isinstance(postgres_config, dict):
        return False
    env = service.get("env") or {}
    config_path = Path(
        os.path.realpath(
            str(env.get("CONFIG_PATH") or service.get("config_dir") or "/infinidysk")
        )
    )
    source_fingerprints = _sqlite_source_fingerprints(config_path / "db.sqlite")
    if source_fingerprints is None:
        return False
    root = Path(migration_root)
    try:
        root_stat = root.lstat()
    except (FileNotFoundError, OSError):
        return False
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
        or (root_stat.st_uid, root_stat.st_gid) != (os.geteuid(), os.getegid())
        or stat.S_IMODE(root_stat.st_mode) != 0o700
    ):
        return False
    authorization_dir = root / "authorizations"
    marker_path = authorization_dir / _INFINIDYSK_AUTHORIZATION_FILENAME
    marker_exists = os.path.lexists(marker_path)
    if marker_exists:
        try:
            authorization_stat = authorization_dir.lstat()
        except OSError:
            return False
        if (
            stat.S_ISLNK(authorization_stat.st_mode)
            or not stat.S_ISDIR(authorization_stat.st_mode)
            or authorization_stat.st_dev != root_stat.st_dev
            or os.path.ismount(authorization_dir)
            or (authorization_stat.st_uid, authorization_stat.st_gid)
            != (os.geteuid(), os.getegid())
            or stat.S_IMODE(authorization_stat.st_mode) != 0o700
        ):
            return False
    authorization = _read_bounded_private_json(marker_path, _MAX_AUTHORIZATION_BYTES)
    if marker_exists:
        return bool(
            authorization
            and _authorization_matches_runtime(
                authorization,
                service,
                postgres_config,
                source_fingerprints,
                require_live_target=require_live_target,
            )
        )
    authorization = _completed_job_authorization(
        root / "jobs",
        service,
        postgres_config,
        source_fingerprints,
        require_live_target=require_live_target,
    )
    return authorization is not None


def infinidysk_postgres_runtime_configured(
    service: dict | None,
    postgres_config: dict | None = None,
    *,
    migration_root: str | os.PathLike[str] = _INFINIDYSK_MIGRATION_ROOT,
    require_live_target: bool = True,
) -> bool:
    """Verify a prior guarded cutover; PostgreSQL environment alone is insufficient."""

    if not isinstance(service, dict) or not isinstance(postgres_config, dict):
        return False
    env = service.get("env") or {}
    database = service_postgres_database_name("infinidysk", None, service)
    if str(env.get("DATABASE_PROVIDER") or "").strip().lower() not in {
        "postgres",
        "postgresql",
    } or not _npgsql_connection_targets_runtime(
        str(env.get("DATABASE_CONNECTION_STRING") or ""),
        postgres_config,
        database,
    ):
        return False
    return infinidysk_postgres_cutover_completed(
        service,
        postgres_config,
        migration_root=migration_root,
        require_live_target=require_live_target,
    )


def validate_infinidysk_postgres_candidate_update(
    current_service: dict,
    candidate_service: dict,
    current_postgres_config: dict,
    candidate_postgres_config: dict | None = None,
    *,
    migration_root: str | os.PathLike[str] = _INFINIDYSK_MIGRATION_ROOT,
) -> tuple[bool, str | None]:
    """Validate one candidate config before any API/onboarding persistence."""

    candidate_postgres_config = (
        candidate_postgres_config
        if isinstance(candidate_postgres_config, dict)
        else current_postgres_config
    )
    current_cutover = infinidysk_postgres_cutover_completed(
        current_service,
        current_postgres_config,
        migration_root=migration_root,
    )
    current_runtime = infinidysk_postgres_runtime_configured(
        current_service,
        current_postgres_config,
        migration_root=migration_root,
    )
    candidate_cutover = infinidysk_postgres_cutover_completed(
        candidate_service,
        candidate_postgres_config,
        migration_root=migration_root,
    )
    candidate_runtime = infinidysk_postgres_runtime_configured(
        candidate_service,
        candidate_postgres_config,
        migration_root=migration_root,
    )
    minimum_commit = (
        infinidysk_postgres_runtime_floor(
            current_service,
            migration_root=migration_root,
        )
        if current_cutover or current_runtime
        else None
    )
    source_safe, source_error = validate_infinidysk_postgres_source_selection(
        candidate_service,
        minimum_commit=minimum_commit,
    )
    if not source_safe:
        return False, source_error
    if (current_cutover and not candidate_cutover) or (
        current_runtime and not candidate_runtime
    ):
        return False, (
            "This change would break InfiniDysk's guarded PostgreSQL cutover or "
            "rollback binding. Use the migration rollback action before changing "
            "its process, source path, provider, database, or PostgreSQL target."
        )

    current_env = current_service.get("env") or {}
    provider_is_postgres = str(
        current_env.get("DATABASE_PROVIDER") or ""
    ).strip().lower() in {"postgres", "postgresql"}
    postgres_selected = provider_is_postgres or current_cutover or current_runtime
    candidate_env = candidate_service.get("env") or {}
    current_config_path = str(
        current_env.get("CONFIG_PATH")
        or current_service.get("config_dir")
        or "/infinidysk"
    )
    candidate_config_path = str(
        candidate_env.get("CONFIG_PATH")
        or candidate_service.get("config_dir")
        or "/infinidysk"
    )
    if provider_is_postgres:
        current_database = service_postgres_database_name(
            "infinidysk", None, current_service
        )
        candidate_database = service_postgres_database_name(
            "infinidysk", None, candidate_service
        )
        static_binding_matches = (
            str(candidate_service.get("process_name") or "")
            == str(current_service.get("process_name") or "")
            and infinidysk_sqlite_source_path_fingerprint(
                Path(os.path.realpath(current_config_path)) / "db.sqlite"
            )
            == infinidysk_sqlite_source_path_fingerprint(
                Path(os.path.realpath(candidate_config_path)) / "db.sqlite"
            )
            and current_database == candidate_database
            and _postgres_target_fingerprint(current_postgres_config, current_database)
            == _postgres_target_fingerprint(
                candidate_postgres_config, candidate_database
            )
        )
        if not static_binding_matches:
            return False, (
                "InfiniDysk's persisted PostgreSQL provider is bound to its current "
                "process, SQLite rollback source, database, and managed PostgreSQL "
                "target. Restore target availability or use the guarded rollback "
                "before changing those fields. Password rotation remains allowed."
            )
    if postgres_selected and str(candidate_service.get("process_name") or "") != str(
        current_service.get("process_name") or ""
    ):
        return False, (
            "InfiniDysk's process name cannot change while PostgreSQL is selected "
            "because the guarded rollback is bound to that identity. Use the "
            "migration rollback action before renaming the service."
        )

    enabling_postgres = (
        current_service.get("postgres_enabled") is not True
        and candidate_service.get("postgres_enabled") is True
    )
    if not infinidysk_postgres_migration_authorized():
        for managed_key in (
            "DATABASE_PROVIDER",
            "DATABASE_CONNECTION_STRING",
        ):
            if current_env.get(managed_key) != candidate_env.get(managed_key):
                return False, (
                    f"InfiniDysk {managed_key} is DUMB-managed. Change "
                    "postgres_enabled or use the guarded migration workflow instead "
                    "of editing the database provider environment directly."
                )
    if enabling_postgres:
        safe, error = validate_infinidysk_postgres_fresh_install(
            current_config_path,
            True,
            service=current_service,
            postgres_config=current_postgres_config,
            migration_root=migration_root,
        )
        if not safe:
            return False, error
        safe, error = validate_infinidysk_postgres_fresh_install(
            candidate_config_path,
            True,
            service=candidate_service,
            postgres_config=candidate_postgres_config,
            migration_root=migration_root,
        )
        if not safe:
            return False, error

    if candidate_service.get("postgres_enabled") is False and postgres_selected:
        safe, error = validate_infinidysk_postgres_fresh_install(
            candidate_config_path,
            False,
            service=candidate_service,
            postgres_config=candidate_postgres_config,
            migration_root=migration_root,
        )
        if not safe:
            return False, error
        return False, (
            "InfiniDysk cannot switch directly from PostgreSQL to SQLite. Use the "
            "guarded migration rollback action when rollback is available."
        )

    return True, None


def infinidysk_postgres_migration_authorized() -> bool:
    """Return whether this thread is performing DUMB's guarded cutover start."""

    return int(getattr(_INFINIDYSK_MIGRATION_CONTEXT, "depth", 0) or 0) > 0


@contextmanager
def authorize_infinidysk_postgres_migration():
    """Narrowly authorize setup during a migration-owned PostgreSQL start."""

    previous = int(getattr(_INFINIDYSK_MIGRATION_CONTEXT, "depth", 0) or 0)
    _INFINIDYSK_MIGRATION_CONTEXT.depth = previous + 1
    try:
        yield
    finally:
        _INFINIDYSK_MIGRATION_CONTEXT.depth = previous


def validate_infinidysk_postgres_fresh_install(
    config_path: str,
    enabled: bool,
    *,
    service: dict | None = None,
    postgres_config: dict | None = None,
    migration_root: str | os.PathLike[str] = _INFINIDYSK_MIGRATION_ROOT,
    allow_offline_authorization: bool = False,
) -> tuple[bool, str | None]:
    """Refuse an implicit InfiniDysk SQLite-to-PostgreSQL provider switch."""

    env = service.get("env") if isinstance(service, dict) else {}
    persisted_provider_is_postgres = str(
        (env or {}).get("DATABASE_PROVIDER") or ""
    ).strip().lower() in {"postgres", "postgresql"}
    if (
        not enabled
        and persisted_provider_is_postgres
        and not infinidysk_postgres_migration_authorized()
    ):
        return False, (
            "InfiniDysk cannot switch from PostgreSQL to a new or preserved SQLite "
            "database through postgres_enabled. PostgreSQL may contain live data; "
            "use the migration rollback action when rollback is available."
        )

    main_database = Path(os.path.realpath(config_path)) / "db.sqlite"
    paths = (
        main_database,
        Path(f"{main_database}-wal"),
        Path(f"{main_database}-shm"),
        Path(f"{main_database}-journal"),
    )
    initialized = False
    for path in paths:
        try:
            path_stat = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            return (
                False,
                "InfiniDysk could not safely inspect its SQLite main database: "
                f"{error}",
            )
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or path_stat.st_nlink != 1
        ):
            return (
                False,
                "InfiniDysk SQLite main database artifacts must be direct regular "
                "files with one link before PostgreSQL can be enabled.",
            )
        initialized = initialized or path_stat.st_size > 0
    if not initialized:
        return True, None

    if not enabled:
        if infinidysk_postgres_cutover_completed(
            service,
            postgres_config,
            migration_root=migration_root,
        ):
            return False, (
                "InfiniDysk cannot switch a completed PostgreSQL cutover back to the "
                "preserved SQLite backup through postgres_enabled. PostgreSQL may "
                "contain newer writes; use the migration rollback action, which "
                "requires explicit confirmation and clears the cutover authorization."
            )
        return True, None

    if infinidysk_postgres_migration_authorized():
        return True, None
    if infinidysk_postgres_runtime_configured(
        service,
        postgres_config,
        migration_root=migration_root,
        require_live_target=not allow_offline_authorization,
    ):
        return True, None

    return False, (
        "Direct InfiniDysk PostgreSQL switching is supported only for fresh "
        "installations. "
        f"An existing SQLite main database was found at {main_database}; DUMB will not "
        "switch providers without the guarded SQLite-to-PostgreSQL migration. Run a "
        "successful rehearsal and cutover from the InfiniDysk service page, or disable "
        "postgres_enabled to keep using SQLite."
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").lower()).strip("_") or "default"


def service_postgres_database_name(
    key: str, instance_name: str | None, service: dict
) -> str:
    configured = str(service.get("postgres_database") or "").strip()
    if configured:
        return configured
    if instance_name and str(instance_name).lower() != "default":
        return f"{key}_{_slug(instance_name)}"
    return key


def postgres_connection_values(postgres_config: dict, database: str) -> dict[str, str]:
    return {
        "host": str(postgres_config.get("host") or "127.0.0.1"),
        "port": str(postgres_config.get("port") or 5432),
        "user": str(postgres_config.get("user") or "DUMB"),
        "password": str(postgres_config.get("password") or "postgres"),
        "database": database,
    }


def postgres_dsn(postgres_config: dict, database: str) -> str:
    values = postgres_connection_values(postgres_config, database)
    host = values["host"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return "postgres://{}:{}@{}:{}/{}?sslmode=disable".format(
        quote(values["user"], safe=""),
        quote(values["password"], safe=""),
        host,
        values["port"],
        quote(values["database"], safe=""),
    )


def postgres_npgsql_connection_string(postgres_config: dict, database: str) -> str:
    """Build the ADO.NET connection string expected by InfiniDysk/Npgsql."""

    values = postgres_connection_values(postgres_config, database)

    def quoted(value: str) -> str:
        return '"' + str(value).replace('"', '""') + '"'

    return ";".join(
        (
            f"Host={quoted(values['host'])}",
            f"Port={quoted(values['port'])}",
            f"Database={quoted(values['database'])}",
            f"Username={quoted(values['user'])}",
            f"Password={quoted(values['password'])}",
        )
    )


def apply_service_postgres_config(
    key: str,
    service: dict,
    postgres_config: dict,
    database: str,
    *,
    enabled: bool,
) -> bool:
    """Apply a service's runtime database selection without starting it."""
    if key not in SERVICE_POSTGRES_KEYS:
        raise ValueError(f"Unsupported PostgreSQL service: {key}")

    values = postgres_connection_values(postgres_config, database)
    changed = False

    if key == "altmount":
        config_dir = str(service.get("config_dir") or "/altmount")
        config_file = str(
            service.get("config_file") or os.path.join(config_dir, "config.yaml")
        )
        # AltMount's setup owns creation of the complete first-run document.
        # Writing a database-only file here would make that initializer skip.
        if not os.path.isfile(config_file):
            return False
        data = {}
        try:
            with open(config_file, "r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
                data = loaded if isinstance(loaded, dict) else {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Could not update AltMount database config: %s", exc)
            return False
        database_config = data.setdefault("database", {})
        desired = (
            {
                "type": "postgres",
                "path": os.path.join(config_dir, "altmount.db"),
                "dsn": postgres_dsn(postgres_config, database),
            }
            if enabled
            else {
                "type": "sqlite",
                "path": os.path.join(config_dir, "altmount.db"),
            }
        )
        for name, value in desired.items():
            if database_config.get(name) != value:
                database_config[name] = value
                changed = True
        if not enabled and database_config.pop("dsn", None) is not None:
            changed = True
        if changed:
            temporary = f"{config_file}.tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                yaml.safe_dump(data, handle, sort_keys=False)
            try:
                stat = os.stat(config_file)
                os.chmod(temporary, stat.st_mode & 0o777)
                os.chown(temporary, stat.st_uid, stat.st_gid)
            except OSError:
                pass
            os.replace(temporary, config_file)
        return changed

    env = service.setdefault("env", {})
    if key == "infinidysk":
        desired = {"DATABASE_PROVIDER": "postgres" if enabled else "sqlite"}
        connection_fields = {
            "DATABASE_CONNECTION_STRING": postgres_npgsql_connection_string(
                postgres_config, database
            )
        }
    elif key == "bazarr":
        desired = {"POSTGRES_ENABLED": "true" if enabled else "false"}
        connection_fields = {
            "POSTGRES_HOST": values["host"],
            "POSTGRES_PORT": values["port"],
            "POSTGRES_DATABASE": values["database"],
            "POSTGRES_USERNAME": values["user"],
            "POSTGRES_PASSWORD": values["password"],
        }
    elif key == "pulsarr":
        desired = {"dbType": "postgres" if enabled else "sqlite"}
        connection_fields = {
            "dbHost": values["host"],
            "dbPort": values["port"],
            "dbName": values["database"],
            "dbUser": values["user"],
            "dbPassword": values["password"],
        }
    else:
        desired = {"DB_TYPE": "postgres" if enabled else "sqlite"}
        connection_fields = {
            "DB_HOST": values["host"],
            "DB_PORT": values["port"],
            "DB_NAME": values["database"],
            "DB_USER": values["user"],
            "DB_PASS": values["password"],
        }
    if enabled:
        desired.update(connection_fields)
    else:
        for name in connection_fields:
            if env.pop(name, None) is not None:
                changed = True
    for name, value in desired.items():
        if env.get(name) != value:
            env[name] = value
            changed = True
    return changed


def iter_postgres_services(config_manager):
    for key in SERVICE_POSTGRES_KEYS:
        section = config_manager.get(key, {}) or {}
        if isinstance(section.get("instances"), dict):
            for instance_name, service in section["instances"].items():
                if (
                    isinstance(service, dict)
                    and service.get("enabled")
                    and service_postgres_enabled(service)
                ):
                    yield key, instance_name, service
        elif section.get("enabled") and service_postgres_enabled(section):
            yield key, None, section


def configure_service_postgres_runtime(
    config_manager, *, allow_offline_authorization: bool = False
) -> bool:
    """Register databases and synchronize runtime config for opted-in services.

    Early startup callers may validate the persisted cutover authorization before
    PostgreSQL is running. The process-launch guard still requires the live target.
    """
    selected = list(iter_postgres_services(config_manager))
    postgres_config = config_manager.get("postgres", {}) or {}
    eligible = []
    for key, instance_name, service in selected:
        if key == "infinidysk":
            env = service.get("env") or {}
            config_path = str(
                env.get("CONFIG_PATH") or service.get("config_dir") or "/infinidysk"
            )
            safe, error = validate_infinidysk_postgres_fresh_install(
                config_path,
                True,
                service=service,
                postgres_config=postgres_config,
                allow_offline_authorization=allow_offline_authorization,
            )
            if not safe:
                logger.warning("%s", error)
                continue
        eligible.append((key, instance_name, service))
    selected = eligible
    if not selected:
        return False
    changed = False
    if not postgres_config.get("enabled"):
        postgres_config["enabled"] = True
        changed = True
    databases = postgres_config.setdefault("databases", [])
    for key, instance_name, service in selected:
        database = service_postgres_database_name(key, instance_name, service)
        entry = next(
            (
                item
                for item in databases
                if isinstance(item, dict) and str(item.get("name")) == database
            ),
            None,
        )
        if entry is not None:
            if entry.get("enabled") is not True:
                entry["enabled"] = True
                changed = True
        else:
            databases.append({"name": database, "enabled": True})
            changed = True
        if apply_service_postgres_config(
            key, service, postgres_config, database, enabled=True
        ):
            changed = True
            logger.info(
                "Synchronized DUMB-managed PostgreSQL configuration for %s.",
                service.get("process_name") or key,
            )
    return changed
