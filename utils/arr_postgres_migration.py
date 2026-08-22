"""Guarded SQLite-to-PostgreSQL migration workflow for supported services.

The normal ``postgres_enabled`` option only changes the configured database
backend.  This module provides the deliberately separate, observable migration
workflow used by the API and frontend.
"""

from __future__ import annotations

import copy
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg2
import yaml
from psycopg2 import sql
from psycopg2.extras import execute_values

from utils.arr_postgres import apply_arr_postgres_config, arr_postgres_database_names
from utils.infinidysk_postgres_contracts import (
    INFINIDYSK_DATABASE_CONTRACTS,
    INFINIDYSK_TRANSIENT_SCHEMA_OBJECTS,
)
from utils.infinidysk_migration_admission import (
    ACTIVE_NAMESPACE_MIGRATION_BLOCKER,
    EXTERNAL_MUTATION_BLOCKER,
    INFINIDYSK_MIGRATION_ADMISSION_LOCK,
    infinidysk_external_mutation_active,
    infinidysk_namespace_migration_active,
)
from utils.postgres import initialize_postgres_databases
from utils.port_probe import is_port_available
from utils.private_files import atomic_write_private_text
from utils.service_postgres import (
    apply_service_postgres_config,
    authorize_infinidysk_postgres_migration,
    clear_infinidysk_postgres_migration_completion,
    infinidysk_installed_runtime_commit,
    infinidysk_launch_config_fingerprint,
    infinidysk_sqlite_source_path_fingerprint,
    infinidysk_postgres_completed_job_valid,
    infinidysk_postgres_physical_identity,
    record_infinidysk_postgres_migration_completion,
    service_postgres_database_name,
    validate_infinidysk_postgres_installed_version,
    validate_infinidysk_postgres_source_selection,
)

# Export the newest audited contract through the original names for callers and
# tests that only need the current boundary. Validation below remains explicitly
# multi-contract so an already-authorized v1.2.0 cutover stays valid.
_INFINIDYSK_CURRENT_DATABASE_CONTRACT = INFINIDYSK_DATABASE_CONTRACTS[-1]
INFINIDYSK_SQLITE_TERMINAL_MIGRATION = _INFINIDYSK_CURRENT_DATABASE_CONTRACT[
    "sqlite_terminal_migration"
]
INFINIDYSK_SQLITE_MIGRATION_COUNT = _INFINIDYSK_CURRENT_DATABASE_CONTRACT[
    "sqlite_migration_count"
]
INFINIDYSK_SQLITE_MIGRATION_HISTORY_SHA256 = _INFINIDYSK_CURRENT_DATABASE_CONTRACT[
    "sqlite_migration_history_fingerprint"
]
INFINIDYSK_SQLITE_SCHEMA_FINGERPRINT = _INFINIDYSK_CURRENT_DATABASE_CONTRACT[
    "sqlite_schema_fingerprint"
]
INFINIDYSK_POSTGRES_ADAPTER_SCHEMA = _INFINIDYSK_CURRENT_DATABASE_CONTRACT[
    "adapter_schema"
]
INFINIDYSK_POSTGRES_SCHEMA_FINGERPRINT = _INFINIDYSK_CURRENT_DATABASE_CONTRACT[
    "postgres_schema_fingerprint"
]
INFINIDYSK_POSTGRES_MIGRATIONS = _INFINIDYSK_CURRENT_DATABASE_CONTRACT[
    "postgres_migrations"
]
INFINIDYSK_POSTGRES_TABLES = (
    "Accounts",
    "ArticleMissCacheEntries",
    "BlobCleanupItems",
    "ConfigItems",
    "DavCleanupItems",
    "DavItems",
    "DavMultipartFiles",
    "DavNzbFiles",
    "DavRarFiles",
    "HealthCheckResults",
    "HealthCheckStats",
    "HistoryCleanupItems",
    "HistoryItems",
    "IndexerApiHits",
    "ListSources",
    "NzbBlobCleanupItems",
    "NzbNames",
    "NzbResolutionGroups",
    "Par2RepairJobs",
    "QueueItems",
    "QueueNzbContents",
    "WantedItems",
    "WatchdogEntries",
)
INFINIDYSK_POSTGRES_FUNCTIONS = (
    "fn_QueueItems_AddNzbBlobCleanup",
    "fn_HistoryItems_Delete_AddNzbBlobCleanup",
    "fn_DavItems_BlobCleanup",
    "fn_DavItems_Delete_Cleanup",
    "fn_HealthCheckResults_Stats",
)
INFINIDYSK_POSTGRES_TRIGGERS = (
    "TR_QueueItems_AddNzbBlobCleanup",
    "TR_HistoryItems_Delete_AddNzbBlobCleanup",
    "TR_DavItems_Delete_AddBlobCleanup",
    "TR_DavItems_Update_AddBlobCleanup",
    "TR_DavItems_Delete_Cleanup",
    "TR_HealthCheckResults_IncrementStats",
    "TR_HealthCheckResults_DecrementStats",
    "TR_HealthCheckResults_UpdateStats",
)
INFINIDYSK_POSTGRES_TRIGGER_BINDINGS = {
    "TR_QueueItems_AddNzbBlobCleanup": (
        "QueueItems",
        "fn_QueueItems_AddNzbBlobCleanup",
    ),
    "TR_HistoryItems_Delete_AddNzbBlobCleanup": (
        "HistoryItems",
        "fn_HistoryItems_Delete_AddNzbBlobCleanup",
    ),
    "TR_DavItems_Delete_AddBlobCleanup": ("DavItems", "fn_DavItems_BlobCleanup"),
    "TR_DavItems_Update_AddBlobCleanup": ("DavItems", "fn_DavItems_BlobCleanup"),
    "TR_DavItems_Delete_Cleanup": ("DavItems", "fn_DavItems_Delete_Cleanup"),
    "TR_HealthCheckResults_IncrementStats": (
        "HealthCheckResults",
        "fn_HealthCheckResults_Stats",
    ),
    "TR_HealthCheckResults_DecrementStats": (
        "HealthCheckResults",
        "fn_HealthCheckResults_Stats",
    ),
    "TR_HealthCheckResults_UpdateStats": (
        "HealthCheckResults",
        "fn_HealthCheckResults_Stats",
    ),
}
INFINIDYSK_POSTGRES_FOREIGN_KEYS = (
    "FK_DavMultipartFiles_DavItems_Id",
    "FK_DavNzbFiles_DavItems_Id",
    "FK_DavRarFiles_DavItems_Id",
    "FK_QueueNzbContents_QueueItems_Id",
)
INFINIDYSK_POSTGRES_FOREIGN_KEY_LAYOUTS = {
    "FK_DavMultipartFiles_DavItems_Id": (
        "DavMultipartFiles",
        ("Id",),
        "DavItems",
        ("Id",),
    ),
    "FK_DavNzbFiles_DavItems_Id": (
        "DavNzbFiles",
        ("Id",),
        "DavItems",
        ("Id",),
    ),
    "FK_DavRarFiles_DavItems_Id": (
        "DavRarFiles",
        ("Id",),
        "DavItems",
        ("Id",),
    ),
    "FK_QueueNzbContents_QueueItems_Id": (
        "QueueNzbContents",
        ("Id",),
        "QueueItems",
        ("Id",),
    ),
}
INFINIDYSK_POSTGRES_IDENTITIES = (
    ("IndexerApiHits", "Id"),
    ("WatchdogEntries", "Id"),
)
INFINIDYSK_AUXILIARY_SQLITE_STORES = (
    "metrics.sqlite",
    "warden.db",
    "usenet-migration.db",
)
INFINIDYSK_TRANSIENT_TABLES = tuple(sorted(INFINIDYSK_TRANSIENT_SCHEMA_OBJECTS))
INFINIDYSK_IMPORT_BATCH_BYTES = 4 * 1024 * 1024
INFINIDYSK_FULL_ROW_DIGEST_ITERSIZE = 16
INFINIDYSK_PRIMARY_KEY_DIGEST_ITERSIZE = 1000
_POSTGRES_TIMESTAMP_FRACTION_RE = re.compile(
    r"^(?P<prefix>.+[T ]\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d+))?"
    r"(?P<suffix>[Zz]|[+-]\d{2}(?::?\d{2})?)?$"
)
MAX_MIGRATION_JOB_BYTES = 2 * 1024 * 1024

SUPPORTED_SERVICES = {
    "sonarr": {
        "main_file": "sonarr.db",
        "minimum_version": (4, 0, 0, 615),
        "key_tables": ("Series", "Episodes", "EpisodeFiles", "History"),
    },
    "radarr": {
        "main_file": "radarr.db",
        "minimum_version": (4, 1, 0, 6133),
        "key_tables": ("Movies", "MovieFiles", "History"),
    },
    "lidarr": {
        "main_file": "lidarr.db",
        "minimum_version": (1, 1, 2, 2890),
        "key_tables": ("Artists", "Albums", "TrackFiles", "History"),
    },
    "prowlarr": {
        "main_file": "prowlarr.db",
        "minimum_version": None,
        "key_tables": ("Indexers", "Applications", "History"),
    },
    "whisparr": {
        "main_file": "whisparr.db",
        "minimum_version": None,
        "key_tables": ("Series", "Episodes", "EpisodeFiles", "History"),
    },
    "bazarr": {
        "minimum_version": (1, 1, 5),
        "key_tables": (
            "table_shows",
            "table_movies",
            "table_history",
            "table_history_movie",
        ),
        "excluded_tables": ("alembic_version",),
    },
    "pulsarr": {
        "minimum_version": None,
        "key_tables": ("users", "notifications", "webhooks"),
        "excluded_tables": ("knex_migrations", "knex_migrations_lock"),
    },
    "seerr": {
        "minimum_version": None,
        "key_tables": ("user", "media", "media_request", "settings"),
        "excluded_tables": ("migrations",),
    },
    "altmount": {
        "minimum_version": None,
        "key_tables": ("import_queue", "import_migrations", "store_refs"),
        "excluded_tables": ("goose_db_version",),
    },
    "infinidysk": {
        "minimum_version": (1, 2, 0),
        "key_tables": (
            "ConfigItems",
            "DavItems",
            "QueueItems",
            "HistoryItems",
            "Accounts",
        ),
        "excluded_tables": (
            "__EFMigrationsHistory",
            "__EFMigrationsLock",
            *INFINIDYSK_TRANSIENT_TABLES,
        ),
        "rehearsal_required": True,
    },
}
ARR_SERVICE_KEYS = {"sonarr", "radarr", "lidarr", "prowlarr", "whisparr"}
TERMINAL_JOB_STATUSES = {
    "completed",
    "failed",
    "failed_rolled_back",
    "rolled_back",
    "interrupted",
    "rollback_failed",
}
ACTIVE_JOB_STATUSES = {"queued", "running", "finalizing", "rolling_back"}
INFINIDYSK_RECOVERY_PENDING_JOB_STATUSES = {
    "failed",
    "interrupted",
    "rollback_failed",
}
# Keep the original location so existing Sonarr/Radarr jobs and rollback backups
# remain available after the workflow expands beyond Arr applications.
DEFAULT_ROOT = "/config/arr-postgres-migration"


class ArrPostgresMigrationError(RuntimeError):
    """Expected migration failure safe to report without a traceback."""


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug or "service"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _ensure_private_directory(path.parent)
    atomic_write_private_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True),
    )
    path_stat = path.lstat()
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_nlink != 1
        or (path_stat.st_uid, path_stat.st_gid) != (os.geteuid(), os.getegid())
        or stat.S_IMODE(path_stat.st_mode) != 0o600
    ):
        raise ArrPostgresMigrationError(
            "Migration job state was not stored as a private controller file."
        )


def _preflight_binding(preflight: dict[str, Any]) -> dict[str, Any]:
    return {
        "process_name": preflight.get("process_name"),
        "service_key": preflight.get("service_key"),
        "instance_name": preflight.get("instance_name"),
        "service_version": preflight.get("service_version"),
        "service_source_commit": preflight.get("service_source_commit"),
        "source_schema_fingerprint": preflight.get("source_schema_fingerprint"),
        "source_path_fingerprint": preflight.get("source_path_fingerprint"),
        "launch_config_fingerprint": preflight.get("launch_config_fingerprint"),
        "source_migration_history_fingerprint": preflight.get(
            "source_migration_history_fingerprint"
        ),
        "database_contract": preflight.get("database_contract"),
        "postgres_database": (preflight.get("postgres") or {}).get("main_database"),
        "postgres_target_fingerprint": (preflight.get("postgres") or {}).get(
            "target_fingerprint"
        ),
    }


def _postgres_target_fingerprint(postgres_config: dict[str, Any], database: str) -> str:
    """Bind a migration record to its non-secret PostgreSQL endpoint identity."""

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


def _version_tuple(value: str | None) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", str(value or ""))
    return tuple(int(item) for item in numbers[:4])


def _infinidysk_application_health(
    api_state,
    process_name: str,
    instance: dict[str, Any],
) -> tuple[bool, str]:
    """Require the managed process plus both official v1.2 readiness endpoints."""

    if api_state is None:
        return False, "DUMB process health is unavailable."
    get_details = getattr(api_state, "get_status_details", None)
    if not callable(get_details):
        return False, "DUMB application health details are unavailable."
    details = get_details(process_name, include_health=True) or {}
    if details.get("status") != "running":
        return False, "InfiniDysk is not running."
    if details.get("health_status") != "healthy":
        return False, "InfiniDysk's managed /health probe is not healthy."

    return _infinidysk_loopback_health(instance)


def _infinidysk_loopback_health(
    instance: dict[str, Any],
) -> tuple[bool, str]:
    """Probe only InfiniDysk's fixed loopback health/readiness endpoints."""

    try:
        port = int(instance.get("backend_port") or 8080)
    except (TypeError, ValueError):
        return False, "InfiniDysk has an invalid backend port."
    if port < 1 or port > 65535:
        return False, "InfiniDysk has an invalid backend port."

    for endpoint in ("health", "ready"):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request("GET", f"/{endpoint}")
            response = connection.getresponse()
            status_code = int(response.status)
            body = response.read(65)
        except Exception:
            return False, f"InfiniDysk /{endpoint} did not answer successfully."
        finally:
            connection.close()
        if (
            status_code != 200
            or len(body) > 64
            or body.decode("utf-8", errors="replace").strip() != "Healthy"
        ):
            return False, f"InfiniDysk /{endpoint} is not Healthy."
    return True, "InfiniDysk /health and /ready are Healthy."


def _format_bytes(value: int | None) -> str:
    size = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _ensure_private_directory(path: Path) -> None:
    """Create a DUMB-owned migration directory and enforce mode 0700."""

    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        entry_stat = path.lstat()
    except OSError as error:
        raise ArrPostgresMigrationError(
            "Migration storage could not be inspected safely."
        ) from error
    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
        raise ArrPostgresMigrationError(
            "Migration storage must be a real private directory."
        )
    if (entry_stat.st_uid, entry_stat.st_gid) != (os.geteuid(), os.getegid()):
        try:
            os.chown(path, os.geteuid(), os.getegid())
        except OSError as error:
            raise ArrPostgresMigrationError(
                "Migration storage is not owned by the DUMB controller."
            ) from error
    path.chmod(0o700)
    final_stat = path.lstat()
    if (final_stat.st_uid, final_stat.st_gid) != (
        os.geteuid(),
        os.getegid(),
    ) or stat.S_IMODE(final_stat.st_mode) != 0o700:
        raise ArrPostgresMigrationError(
            "Migration storage ownership or private mode could not be enforced."
        )


def _ensure_private_child_directory(root: Path, path: Path) -> None:
    """Create a private child without accepting nested mounts or symlinks."""

    root = root.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ArrPostgresMigrationError(
            "Migration storage escaped the configured fixed root."
        ) from error

    _ensure_private_directory(root)
    root_stat = root.stat()
    current = root
    for part in relative.parts:
        current = current / part
        if os.path.lexists(current):
            try:
                entry_stat = current.lstat()
            except OSError as error:
                raise ArrPostgresMigrationError(
                    "Migration storage could not be inspected safely."
                ) from error
            if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
                raise ArrPostgresMigrationError(
                    "Migration storage below the fixed root must contain only "
                    "real private directories."
                )
            if entry_stat.st_dev != root_stat.st_dev or os.path.ismount(current):
                raise ArrPostgresMigrationError(
                    "Nested mounts are not allowed inside a migration job or "
                    "backup path."
                )
        else:
            try:
                current.mkdir(mode=0o700)
            except OSError as error:
                raise ArrPostgresMigrationError(
                    "Migration storage could not be created safely."
                ) from error
            entry_stat = current.lstat()
            if (
                not stat.S_ISDIR(entry_stat.st_mode)
                or entry_stat.st_dev != root_stat.st_dev
                or os.path.ismount(current)
            ):
                raise ArrPostgresMigrationError(
                    "Migration storage changed while it was being created."
                )
        if (entry_stat.st_uid, entry_stat.st_gid) != (
            os.geteuid(),
            os.getegid(),
        ):
            try:
                os.chown(current, os.geteuid(), os.getegid())
            except OSError as error:
                raise ArrPostgresMigrationError(
                    "Migration child storage is not controller-owned."
                ) from error
        current.chmod(0o700)


def _validate_private_child_directory(root: Path, path: Path) -> Path:
    """Validate an existing controller-private child without creating or repairing it."""

    root = root.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(root)
        root_stat = root.lstat()
    except (ValueError, OSError) as error:
        raise ArrPostgresMigrationError(
            "Migration backup path escaped its controller-owned root."
        ) from error
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
        or (root_stat.st_uid, root_stat.st_gid) != (os.geteuid(), os.getegid())
        or stat.S_IMODE(root_stat.st_mode) != 0o700
    ):
        raise ArrPostgresMigrationError(
            "Migration backup root is not controller-private."
        )
    current = root
    for part in relative.parts:
        current = current / part
        try:
            entry_stat = current.lstat()
        except OSError as error:
            raise ArrPostgresMigrationError(
                "Migration backup directory is missing or unsafe."
            ) from error
        if (
            stat.S_ISLNK(entry_stat.st_mode)
            or not stat.S_ISDIR(entry_stat.st_mode)
            or entry_stat.st_dev != root_stat.st_dev
            or os.path.ismount(current)
            or (entry_stat.st_uid, entry_stat.st_gid) != (os.geteuid(), os.getegid())
            or stat.S_IMODE(entry_stat.st_mode) != 0o700
        ):
            raise ArrPostgresMigrationError(
                "Migration backup directory is not controller-private."
            )
    return path


def _validate_private_backup_file(path: Path, backup_dir: Path) -> Path:
    """Require one direct mode-0600 controller file inside the selected job backup."""

    path = path.absolute()
    if path.parent != backup_dir:
        raise ArrPostgresMigrationError(
            "Migration backup file escaped the selected private job directory."
        )
    try:
        entry_stat = path.lstat()
    except OSError as error:
        raise ArrPostgresMigrationError(
            "A required migration backup file is missing."
        ) from error
    if (
        stat.S_ISLNK(entry_stat.st_mode)
        or not stat.S_ISREG(entry_stat.st_mode)
        or entry_stat.st_nlink != 1
        or (entry_stat.st_uid, entry_stat.st_gid) != (os.geteuid(), os.getegid())
        or stat.S_IMODE(entry_stat.st_mode) != 0o600
    ):
        raise ArrPostgresMigrationError(
            "A migration backup file is not a private controller-owned regular file."
        )
    return path


def _copy_private_file(source: Path, destination: Path) -> None:
    _ensure_private_directory(destination.parent)
    shutil.copy2(source, destination)
    destination.chmod(0o600)


def _is_regular_file_without_symlink(path: Path) -> bool:
    try:
        entry_stat = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(entry_stat.st_mode)
        and not stat.S_ISLNK(entry_stat.st_mode)
        and entry_stat.st_nlink == 1
    )


def _direct_file_identity(path: Path) -> tuple[int, int]:
    try:
        entry_stat = path.lstat()
    except OSError as error:
        raise ArrPostgresMigrationError(
            f"SQLite source {path.name} could not be inspected safely."
        ) from error
    if (
        not stat.S_ISREG(entry_stat.st_mode)
        or stat.S_ISLNK(entry_stat.st_mode)
        or entry_stat.st_nlink != 1
    ):
        raise ArrPostgresMigrationError(
            f"SQLite source {path.name} must be a direct regular file with one link."
        )
    return entry_stat.st_dev, entry_stat.st_ino


def _sqlite_schema_fingerprint(
    path: Path,
    excluded_tables: set[str] | frozenset[str] | None = None,
) -> str:
    """Hash the main SQLite schema without reading application secrets."""

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        # SQLite may create volatile engine-owned statistics objects such as
        # sqlite_stat1 after ANALYZE. Ignore only those statistics: generated
        # auto-indexes and sqlite_sequence remain part of the exact contract.
        rows = connection.execute(
            "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
            "WHERE name <> '__EFMigrationsLock' AND name NOT GLOB 'sqlite_stat*' "
            "ORDER BY type, name, tbl_name"
        ).fetchall()
    finally:
        connection.close()
    excluded_tables = excluded_tables or set()
    if excluded_tables:
        rows = [
            row
            for row in rows
            if str(row[1]) not in excluded_tables and str(row[2]) not in excluded_tables
        ]
    payload = json.dumps(rows, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sqlite_foreign_key_violations(path: Path) -> list[tuple[Any, ...]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        return connection.execute("PRAGMA foreign_key_check").fetchmany(11)
    finally:
        connection.close()


def _infinidysk_sqlite_terminal_migration(path: Path) -> str | None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        row = connection.execute(
            'SELECT "MigrationId" FROM "__EFMigrationsHistory" '
            'ORDER BY "MigrationId" DESC LIMIT 1'
        ).fetchone()
        return str(row[0]) if row else None
    except sqlite3.DatabaseError:
        return None
    finally:
        connection.close()


def _infinidysk_sqlite_schema_details(path: Path) -> dict[str, Any] | None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        table_rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        table_rows = [
            row for row in table_rows if str(row[0]) not in INFINIDYSK_TRANSIENT_TABLES
        ]
        history_rows = connection.execute(
            'SELECT "MigrationId" FROM "__EFMigrationsHistory" '
            'ORDER BY "MigrationId"'
        ).fetchall()
        foreign_keys = []
        for child_table in INFINIDYSK_POSTGRES_TABLES:
            escaped_table = child_table.replace('"', '""')
            for row in connection.execute(
                f'PRAGMA foreign_key_list("{escaped_table}")'
            ).fetchall():
                foreign_keys.append(
                    (
                        child_table,
                        str(row[3]),
                        str(row[2]),
                        str(row[4]),
                        str(row[6]).upper(),
                    )
                )
    except sqlite3.DatabaseError:
        return None
    finally:
        connection.close()
    migration_history = tuple(str(row[0]) for row in history_rows)
    history_payload = json.dumps(
        list(migration_history), separators=(",", ":"), ensure_ascii=False
    )
    return {
        "tables": tuple(str(row[0]) for row in table_rows),
        "migration_history": migration_history,
        "migration_history_fingerprint": hashlib.sha256(
            history_payload.encode("utf-8")
        ).hexdigest(),
        "foreign_keys": tuple(sorted(foreign_keys)),
    }


def _infinidysk_expected_sqlite_foreign_keys() -> set[tuple[str, ...]]:
    return {
        (
            child_table,
            child_columns[0],
            parent_table,
            parent_columns[0],
            "CASCADE",
        )
        for child_table, child_columns, parent_table, parent_columns in (
            INFINIDYSK_POSTGRES_FOREIGN_KEY_LAYOUTS.values()
        )
    }


def _infinidysk_contract_by_id(contract_id: str | None) -> dict[str, Any] | None:
    return next(
        (
            contract
            for contract in INFINIDYSK_DATABASE_CONTRACTS
            if contract["id"] == str(contract_id or "")
        ),
        None,
    )


def _infinidysk_sqlite_contract(
    details: dict[str, Any] | None,
    schema_fingerprint: str | None,
) -> dict[str, Any] | None:
    """Return the exact audited contract for a source SQLite database."""

    if not details:
        return None
    migration_history = details.get("migration_history", ())
    expected_tables = set(INFINIDYSK_POSTGRES_TABLES) | {
        "__EFMigrationsHistory",
        "__EFMigrationsLock",
    }
    if (
        set(details.get("tables", ())) != expected_tables
        or set(details.get("foreign_keys", ()))
        != _infinidysk_expected_sqlite_foreign_keys()
    ):
        return None
    for contract in INFINIDYSK_DATABASE_CONTRACTS:
        if (
            schema_fingerprint == contract["sqlite_schema_fingerprint"]
            and len(migration_history) == contract["sqlite_migration_count"]
            and migration_history
            and migration_history[-1] == contract["sqlite_terminal_migration"]
            and details.get("migration_history_fingerprint")
            == contract["sqlite_migration_history_fingerprint"]
        ):
            return contract
    return None


def _infinidysk_sqlite_contract_matches(
    details: dict[str, Any] | None,
    schema_fingerprint: str | None,
) -> bool:
    """Match any audited data contract without pinning the application release."""

    return _infinidysk_sqlite_contract(details, schema_fingerprint) is not None


def _infinidysk_contract_for_binding(
    binding: dict[str, Any] | None,
) -> dict[str, Any] | None:
    binding = binding or {}
    return next(
        (
            contract
            for contract in INFINIDYSK_DATABASE_CONTRACTS
            if (
                not binding.get("database_contract")
                or binding.get("database_contract") == contract["id"]
            )
            and binding.get("source_schema_fingerprint")
            == contract["sqlite_schema_fingerprint"]
            and binding.get("source_migration_history_fingerprint")
            == contract["sqlite_migration_history_fingerprint"]
        ),
        None,
    )


def _infinidysk_runtime_command(
    instance: dict[str, Any],
) -> tuple[list[str] | None, str | None]:
    from utils.setup import _nzbdav_build_command

    config_dir = str(instance.get("config_dir") or "/infinidysk")
    output_dir = str(
        instance.get("backend_output_dir") or os.path.join(config_dir, "app")
    )
    return _nzbdav_build_command(output_dir, config_dir, prefer_native=True)


def _validate_infinidysk_snapshot(
    path: Path,
    binding: dict[str, Any],
) -> None:
    if _sqlite_schema_fingerprint(
        path, set(INFINIDYSK_TRANSIENT_TABLES)
    ) != binding.get("source_schema_fingerprint"):
        raise ArrPostgresMigrationError(
            "The InfiniDysk SQLite backup schema does not match preflight."
        )
    details = _infinidysk_sqlite_schema_details(path)
    if not details:
        raise ArrPostgresMigrationError(
            "The InfiniDysk SQLite backup schema could not be inspected."
        )
    if not _infinidysk_sqlite_contract_matches(
        details, _sqlite_schema_fingerprint(path, set(INFINIDYSK_TRANSIENT_TABLES))
    ) or details["migration_history_fingerprint"] != binding.get(
        "source_migration_history_fingerprint"
    ):
        raise ArrPostgresMigrationError(
            "The InfiniDysk SQLite backup does not match DUMB's supported source "
            "database contract."
        )


def _restore_infinidysk_sqlite_snapshot(
    snapshot: Path,
    destination: Path,
    binding: dict[str, Any],
) -> None:
    """Atomically restore the cold snapshot without weakening service ownership."""

    snapshot_identity = _direct_file_identity(snapshot)
    _validate_infinidysk_snapshot(snapshot, binding)
    destination_identity = _direct_file_identity(destination)
    destination_stat = destination.lstat()
    sidecar_identities = {}
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{destination}{suffix}")
        if not os.path.lexists(sidecar):
            continue
        if not _is_regular_file_without_symlink(sidecar):
            raise ArrPostgresMigrationError(
                "An unsafe InfiniDysk SQLite sidecar blocked rollback."
            )
        sidecar_identities[sidecar] = _direct_file_identity(sidecar)
    temporary_path = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".db.sqlite.dumb-rollback-",
            dir=str(destination.parent),
        )
        temporary_path = Path(temporary_name)
        with (
            snapshot.open("rb") as source_handle,
            os.fdopen(descriptor, "wb") as destination_handle,
        ):
            shutil.copyfileobj(source_handle, destination_handle, length=1024 * 1024)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
            os.fchmod(
                destination_handle.fileno(), stat.S_IMODE(destination_stat.st_mode)
            )
            os.fchown(
                destination_handle.fileno(),
                destination_stat.st_uid,
                destination_stat.st_gid,
            )
        if _direct_file_identity(destination) != destination_identity:
            raise ArrPostgresMigrationError(
                "InfiniDysk SQLite changed while rollback was being restored."
            )
        if _direct_file_identity(snapshot) != snapshot_identity or any(
            _direct_file_identity(sidecar) != identity
            for sidecar, identity in sidecar_identities.items()
        ):
            raise ArrPostgresMigrationError(
                "InfiniDysk rollback inputs changed while the snapshot was staged."
            )
        os.replace(temporary_path, destination)
        temporary_path = None
        for sidecar, identity in sidecar_identities.items():
            if _direct_file_identity(sidecar) != identity:
                raise ArrPostgresMigrationError(
                    "An InfiniDysk SQLite sidecar changed during rollback."
                )
            sidecar.unlink()
        _validate_infinidysk_snapshot(destination, binding)
        healthy, message = _sqlite_quick_check(destination)
        if not healthy or _sqlite_foreign_key_violations(destination):
            raise ArrPostgresMigrationError(
                "Restored InfiniDysk SQLite failed integrity validation: " f"{message}"
            )
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _read_arr_version(config_dir: Path, key: str) -> str | None:
    logs_dir = config_dir / "logs"
    if not logs_dir.is_dir():
        return None
    pattern = re.compile(
        rf"Starting\s+{re.escape(key.capitalize())}\b.*?Version\s+([0-9][0-9.]+)",
        re.IGNORECASE,
    )
    candidates = sorted(
        logs_dir.glob("*.txt"),
        key=lambda item: item.stat().st_mtime if item.exists() else 0,
        reverse=True,
    )
    for path in candidates[:8]:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in reversed(deque(handle, maxlen=5000)):
                    match = pattern.search(line)
                    if match:
                        return match.group(1)
        except OSError:
            continue
    return None


def _read_infinidysk_version_marker(config_dir: Path) -> str | None:
    """Read DUMB's exact bounded provenance marker without lossy log parsing."""

    path = config_dir / "version.txt"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size < 1
                or info.st_size > 256
            ):
                return None
            raw = os.read(descriptor, 257)
        finally:
            os.close(descriptor)
        if len(raw) > 256:
            return None
        return raw.decode("utf-8").strip() or None
    except (OSError, UnicodeError):
        return None


def _sqlite_quick_check(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, "Database file is missing."
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        result = connection.execute("PRAGMA quick_check(1)").fetchone()
        message = str(result[0] if result else "No result")
        return message.lower() == "ok", message
    finally:
        connection.close()


def _sqlite_tables(
    connection: sqlite3.Connection, excluded_tables: set[str] | None = None
) -> list[str]:
    excluded_tables = excluded_tables or set()
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows if str(row[0]) not in excluded_tables]


def _sqlite_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    escaped = table.replace('"', '""')
    return [
        str(row[1]) for row in connection.execute(f'PRAGMA table_info("{escaped}")')
    ]


def _sqlite_row_counts(
    path: Path, excluded_tables: set[str] | None = None
) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        counts = {}
        for table in _sqlite_tables(connection, excluded_tables):
            escaped = table.replace('"', '""')
            counts[table] = int(
                connection.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0]
            )
        return counts
    finally:
        connection.close()


def _postgres_params(postgres_config: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": postgres_config.get("host", "127.0.0.1"),
        "port": int(postgres_config.get("port", 5432)),
        "user": postgres_config.get("user", "DUMB"),
        "password": postgres_config.get("password", "postgres"),
    }


def _pg_connect(postgres_config: dict[str, Any], database: str):
    return psycopg2.connect(dbname=database, **_postgres_params(postgres_config))


def _database_exists(postgres_config: dict[str, Any], database: str) -> bool:
    connection = _pg_connect(postgres_config, "postgres")
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [database])
            return cursor.fetchone() is not None
    finally:
        connection.close()


def _postgres_database_summary(
    postgres_config: dict[str, Any], database: str
) -> dict[str, Any]:
    if not _database_exists(postgres_config, database):
        return {
            "name": database,
            "exists": False,
            "table_count": 0,
            "row_count": 0,
        }
    connection = _pg_connect(postgres_config, database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*), COALESCE(SUM(n_live_tup), 0) "
                "FROM pg_stat_user_tables WHERE schemaname = 'public'"
            )
            table_count, row_count = cursor.fetchone()
            return {
                "name": database,
                "exists": True,
                "table_count": int(table_count or 0),
                "row_count": int(row_count or 0),
            }
    finally:
        connection.close()


def _postgres_role_summary(postgres_config: dict[str, Any]) -> dict[str, bool]:
    connection = _pg_connect(postgres_config, "postgres")
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rolsuper, rolcreatedb FROM pg_roles WHERE rolname = current_user"
            )
            row = cursor.fetchone()
            return {
                "superuser": bool(row and row[0]),
                "createdb": bool(row and row[1]),
            }
    finally:
        connection.close()


def _resolve_instance(config_manager, process_name: str):
    key, instance_name = config_manager.find_key_for_process(process_name)
    if key not in SUPPORTED_SERVICES:
        raise ArrPostgresMigrationError(
            "SQLite-to-PostgreSQL migration is not available for this service."
        )
    instance = config_manager.get_instance(instance_name, key)
    if not isinstance(instance, dict):
        raise ArrPostgresMigrationError("Service configuration was not found.")
    return key, instance_name, instance


def _source_paths(key: str, instance: dict[str, Any]) -> dict[str, Any]:
    config_dir = Path(
        str(
            instance.get("config_dir") or ("/infinidysk" if key == "infinidysk" else "")
        )
    )
    if key == "infinidysk":
        env = instance.get("env") or {}
        configured_path = str(env.get("CONFIG_PATH") or config_dir)
        data_root = Path(os.path.realpath(configured_path))
        return {
            "config_dir": config_dir,
            "config_xml": None,
            "main": data_root / "db.sqlite",
            "auxiliary": [
                data_root / filename for filename in INFINIDYSK_AUXILIARY_SQLITE_STORES
            ],
        }
    if key == "bazarr":
        data_dir = Path("/bazarr/data")
        command = instance.get("command") or []
        if isinstance(command, list):
            try:
                config_index = command.index("--config")
                configured_data_dir = str(command[config_index + 1]).strip()
                if configured_data_dir:
                    data_dir = Path(configured_data_dir)
            except (ValueError, IndexError):
                pass

        configured_file = Path(
            str(instance.get("config_file") or data_dir / "config" / "config.yaml")
        )
        config_candidates = [
            configured_file,
            data_dir / "config" / "config.yaml",
            data_dir / "config.yaml",
        ]
        config_file = next(
            (candidate for candidate in config_candidates if candidate.is_file()),
            data_dir / "config" / "config.yaml",
        )
        return {
            "config_dir": config_dir,
            "config_xml": config_file,
            "main": data_dir / "db" / "bazarr.db",
        }
    if key == "pulsarr":
        return {
            "config_dir": config_dir,
            "config_xml": None,
            "main": config_dir / "data" / "db" / "pulsarr.db",
        }
    if key == "seerr":
        env = instance.get("env") or {}
        data_dir = Path(str(env.get("CONFIG_DIRECTORY") or config_dir / "config"))
        return {
            "config_dir": config_dir,
            "config_xml": None,
            "main": data_dir / "db" / "db.sqlite3",
        }
    if key == "altmount":
        config_file = Path(
            str(instance.get("config_file") or config_dir / "config.yaml")
        )
        sqlite_path = config_dir / "altmount.db"
        if config_file.is_file():
            try:
                with config_file.open("r", encoding="utf-8") as handle:
                    database = (yaml.safe_load(handle) or {}).get("database") or {}
                configured_path = str(database.get("path") or "").strip()
                if configured_path:
                    sqlite_path = Path(configured_path)
                    if not sqlite_path.is_absolute():
                        sqlite_path = config_dir / sqlite_path
            except (OSError, TypeError, ValueError, yaml.YAMLError):
                pass
        return {
            "config_dir": config_dir,
            "config_xml": config_file,
            "main": sqlite_path,
        }
    return {
        "config_dir": config_dir,
        "config_xml": Path(
            str(instance.get("config_file") or config_dir / "config.xml")
        ),
        "main": config_dir / SUPPORTED_SERVICES[key]["main_file"],
        "log": config_dir / "logs.db",
    }


def _database_names(
    key: str, instance_name: str | None, instance: dict[str, Any]
) -> dict[str, str]:
    if key in ARR_SERVICE_KEYS:
        main_db, log_db = arr_postgres_database_names(
            key, instance_name or "Default", instance
        )
        return {"main": main_db, "log": log_db}
    return {
        "main": service_postgres_database_name(key, instance_name, instance),
    }


def _apply_database_config(
    key: str,
    instance_name: str | None,
    instance: dict[str, Any],
    paths: dict[str, Path | None],
    postgres_config: dict[str, Any],
    databases: dict[str, str],
    *,
    enabled: bool,
) -> None:
    instance["postgres_enabled"] = bool(enabled)
    if key in ARR_SERVICE_KEYS:
        instance["postgres_main_db"] = databases["main"]
        instance["postgres_log_db"] = databases["log"]
        apply_arr_postgres_config(
            key,
            instance_name or "Default",
            instance,
            str(paths["config_xml"]),
            postgres_config,
        )
        return
    instance["postgres_database"] = databases["main"]
    apply_service_postgres_config(
        key,
        instance,
        postgres_config,
        databases["main"],
        enabled=enabled,
    )


def _clear_infinidysk_rollback_authorization(root: Path) -> None:
    """Fail rollback unless the durable PostgreSQL authorization is consumed."""

    if clear_infinidysk_postgres_migration_completion(migration_root=root):
        return
    raise ArrPostgresMigrationError(
        "InfiniDysk rollback restored SQLite but could not safely clear the "
        "PostgreSQL cutover authorization. Inspect the controller-owned migration "
        "authorization storage before retrying rollback."
    )


def _infinidysk_namespace_migration_resolved(config: dict) -> tuple[bool, str]:
    """Require canonical identity/namespace state before database migration."""

    try:
        from utils.infinidysk_migration import INFINIDYSK_MIGRATION_MANAGER

        namespace_status = INFINIDYSK_MIGRATION_MANAGER.status(config)
        status = str(namespace_status.get("status") or "pending")
    except Exception:
        return False, "unavailable"
    return status in {"not_needed", "compatibility_completed", "completed"}, status


def _infinidysk_postgres_source_selection(
    instance: dict[str, Any],
) -> tuple[bool, str | None]:
    """Validate the saved selector as it will exist immediately after cutover."""

    candidate = copy.deepcopy(instance)
    candidate["postgres_enabled"] = True
    candidate_env = dict(candidate.get("env") or {})
    candidate_env["DATABASE_PROVIDER"] = "postgres"
    candidate["env"] = candidate_env
    return validate_infinidysk_postgres_source_selection(candidate)


def build_arr_postgres_preflight(
    config_manager,
    process_name: str,
    api_state=None,
    root: str | Path = DEFAULT_ROOT,
) -> dict[str, Any]:
    """Return a non-mutating migration readiness report."""
    key, instance_name, instance = _resolve_instance(config_manager, process_name)
    paths = _source_paths(key, instance)
    postgres_config = config_manager.get("postgres", {}) or {}
    database_names = _database_names(key, instance_name, instance)
    checks: list[dict[str, Any]] = []

    def add_check(check_id, status, message, **details):
        checks.append({"id": check_id, "status": status, "message": message, **details})

    enabled = bool(instance.get("enabled"))
    add_check(
        "service_enabled",
        "pass" if enabled else "fail",
        "Service instance is enabled." if enabled else "Service instance is disabled.",
    )
    already_postgres = instance.get("postgres_enabled") is True
    add_check(
        "sqlite_mode",
        "fail" if already_postgres else "pass",
        (
            "The instance is already configured for PostgreSQL."
            if already_postgres
            else "The instance is currently configured for SQLite."
        ),
    )

    sqlite_payload: dict[str, Any] = {}
    source_schema_fingerprint = None
    source_path_fingerprint = None
    source_migration_history_fingerprint = None
    source_database_contract = None
    launch_config_fingerprint = None
    service_source_commit = None
    for label in database_names:
        path = paths[label]
        safe_regular = _is_regular_file_without_symlink(path)
        exists = path.is_file()
        unsafe_source = key == "infinidysk" and label == "main" and not safe_regular
        size = path.stat().st_size if exists and not unsafe_source else 0
        healthy, message = (
            _sqlite_quick_check(path)
            if exists and not unsafe_source
            else (
                False,
                (
                    "Source must be a regular, non-symlink file."
                    if unsafe_source
                    else "Missing"
                ),
            )
        )
        foreign_key_violations = _sqlite_foreign_key_violations(path) if healthy else []
        if foreign_key_violations:
            healthy = False
            message = (
                "foreign_key_check found "
                f"{len(foreign_key_violations)} or more violations."
            )
        required = label == "main"
        status = "pass" if healthy else ("fail" if required else "warn")
        add_check(
            f"sqlite_{label}",
            status,
            (
                f"{label.capitalize()} SQLite quick check passed."
                if healthy
                else f"{label.capitalize()} SQLite database: {message}"
            ),
            path=str(path),
            bytes=size,
            display_size=_format_bytes(size),
            foreign_key_violations=len(foreign_key_violations),
        )
        sqlite_payload[label] = {
            "path": str(path),
            "exists": exists,
            "bytes": size,
            "display_size": _format_bytes(size),
            "quick_check": message,
            "foreign_key_violations": len(foreign_key_violations),
        }
        if label == "main" and healthy:
            source_schema_fingerprint = _sqlite_schema_fingerprint(
                path,
                set(INFINIDYSK_TRANSIENT_TABLES) if key == "infinidysk" else None,
            )
            if key == "infinidysk":
                source_path_fingerprint = infinidysk_sqlite_source_path_fingerprint(
                    path
                )

    if key == "infinidysk":
        source_safe, source_error = _infinidysk_postgres_source_selection(instance)
        add_check(
            "infinidysk_postgres_source",
            "pass" if source_safe else "fail",
            (
                "The saved InfiniDysk source selection remains supported after "
                "PostgreSQL cutover."
                if source_safe
                else source_error
            ),
        )
        namespace_resolved, namespace_status = _infinidysk_namespace_migration_resolved(
            config_manager.config
        )
        add_check(
            "infinidysk_namespace_ordering",
            "pass" if namespace_resolved else "fail",
            (
                "InfiniDysk identity and namespace migration is already resolved."
                if namespace_resolved
                else "Complete the InfiniDysk identity/namespace migration before "
                "starting its SQLite-to-PostgreSQL migration."
            ),
            namespace_status=namespace_status,
        )
        main_path = paths["main"]
        schema_details = (
            _infinidysk_sqlite_schema_details(main_path)
            if _is_regular_file_without_symlink(main_path)
            else None
        )
        migration_history = (
            schema_details.get("migration_history", ()) if schema_details else ()
        )
        source_migration_history_fingerprint = (
            schema_details.get("migration_history_fingerprint")
            if schema_details
            else None
        )
        terminal_migration = migration_history[-1] if migration_history else None
        actual_sqlite_foreign_keys = set(
            schema_details.get("foreign_keys", ()) if schema_details else ()
        )
        actual_tables = (
            set(schema_details.get("tables", ())) if schema_details else set()
        )
        matched_contract = _infinidysk_sqlite_contract(
            schema_details, source_schema_fingerprint
        )
        terminal_ok = matched_contract is not None
        source_database_contract = (
            matched_contract["id"] if matched_contract is not None else None
        )
        supported_contracts = ", ".join(
            (
                f"{contract['id']} ({contract['sqlite_migration_count']} migrations, "
                f"ending at {contract['sqlite_terminal_migration']})"
            )
            for contract in INFINIDYSK_DATABASE_CONTRACTS
        )
        add_check(
            "infinidysk_sqlite_schema",
            "pass" if terminal_ok else "fail",
            (
                "InfiniDysk SQLite matches DUMB's supported database contract: "
                "23 application tables, both EF metadata tables, and the audited "
                f"{source_database_contract} schema fingerprint."
                if terminal_ok
                else "InfiniDysk SQLite has missing, extra, or changed schema data. "
                f"It must match one of DUMB's supported contracts: {supported_contracts}. "
                "Update DUMB before migrating a newer database contract."
            ),
            detected=terminal_migration,
            required=[
                contract["sqlite_terminal_migration"]
                for contract in INFINIDYSK_DATABASE_CONTRACTS
            ],
            database_contract=source_database_contract,
            table_count=len(actual_tables),
            migration_count=len(migration_history),
            migration_history_fingerprint=source_migration_history_fingerprint,
            schema_fingerprint=source_schema_fingerprint,
            foreign_key_count=len(actual_sqlite_foreign_keys),
        )

    config_path = paths.get("config_xml")
    config_required = key in ARR_SERVICE_KEYS or key in {"bazarr", "altmount"}
    config_exists = bool(config_path and config_path.is_file())
    add_check(
        "service_config",
        "pass" if config_exists or not config_required else "fail",
        (
            "Service configuration is available for guarded switching."
            if config_exists or not config_required
            else "Service configuration file is missing."
        ),
        path=str(config_path) if config_path else None,
    )

    if key == "infinidysk":
        runtime_command, runtime_error = _infinidysk_runtime_command(instance)
        launch_config_fingerprint = infinidysk_launch_config_fingerprint(instance)
        service_source_commit = infinidysk_installed_runtime_commit(paths["config_dir"])
        add_check(
            "infinidysk_runtime",
            "pass" if runtime_command else "fail",
            (
                "Installed InfiniDysk backend runtime is available for exact schema staging."
                if runtime_command
                else "The installed InfiniDysk backend runtime is missing or incomplete. "
                f"{runtime_error or ''}".strip()
            ),
        )
        add_check(
            "infinidysk_runtime_provenance",
            "pass" if service_source_commit else "fail",
            (
                "Installed InfiniDysk runtime has exact commit provenance for the "
                "post-cutover compatibility floor."
                if service_source_commit
                else "Installed InfiniDysk runtime commit provenance is missing or "
                "does not match version.txt. Reinstall the configured official "
                "runtime before migrating so future release and branch ancestry "
                "can be validated safely."
            ),
            commit=(service_source_commit[:12] if service_source_commit else None),
        )

    version = (
        _read_infinidysk_version_marker(paths["config_dir"])
        if key == "infinidysk"
        else _read_arr_version(paths["config_dir"], key)
    )
    if not version and key != "infinidysk":
        for marker in (
            paths["config_dir"] / "version.txt",
            paths["config_dir"] / "VERSION",
        ):
            if marker.is_file():
                try:
                    version = marker.read_text(encoding="utf-8").strip() or None
                except OSError:
                    pass
                if version:
                    break
    minimum = SUPPORTED_SERVICES[key]["minimum_version"]
    if minimum:
        if key == "infinidysk":
            version_ok, version_error = validate_infinidysk_postgres_installed_version(
                paths["config_dir"]
            )
        else:
            version_ok = bool(version and _version_tuple(version) >= minimum)
            version_error = None
        add_check(
            "service_version",
            "pass" if version_ok else ("fail" if key == "infinidysk" else "warn"),
            (
                (
                    f"Detected InfiniDysk {version}; the official stable runtime "
                    "meets the v1.2.0 minimum. Exact source and staged target "
                    "database contracts are validated separately."
                    if key == "infinidysk"
                    else f"Detected {key.capitalize()} {version}."
                )
                if version_ok
                else (
                    version_error
                    if key == "infinidysk"
                    else "Could not confirm the minimum "
                    f"{'.'.join(map(str, minimum))} version."
                )
            ),
            detected=version,
            minimum=".".join(map(str, minimum)),
            compatibility="exact_database_contract" if key == "infinidysk" else None,
        )

    postgres_payload: dict[str, Any] = {
        "enabled": bool(postgres_config.get("enabled")),
        "main_database": database_names["main"],
        "log_database": database_names.get("log"),
        "target_fingerprint": _postgres_target_fingerprint(
            postgres_config, database_names["main"]
        ),
    }
    try:
        role = _postgres_role_summary(postgres_config)
        postgres_payload["role"] = role
        add_check(
            "postgres_connection",
            "pass",
            "DUMB PostgreSQL is reachable.",
        )
        add_check(
            "postgres_role",
            "pass" if role["superuser"] and role["createdb"] else "fail",
            (
                "PostgreSQL role can create databases and suspend triggers during import."
                if role["superuser"] and role["createdb"]
                else "PostgreSQL role requires superuser and CREATEDB for guarded import."
            ),
        )
        targets = {
            label: _postgres_database_summary(postgres_config, database)
            for label, database in database_names.items()
        }
        postgres_payload["targets"] = targets
        populated = any(item["row_count"] for item in targets.values())
        add_check(
            "target_reset",
            "warn" if populated else "pass",
            (
                "Target databases contain schema or data and will be reset only after explicit confirmation."
                if populated
                else "Target databases are absent or empty."
            ),
        )
    except Exception:
        postgres_payload["targets"] = {}
        add_check(
            "postgres_connection",
            "fail",
            "DUMB PostgreSQL is not reachable with the configured credentials.",
        )

    backup_root = Path(root)
    storage_probe = backup_root if backup_root.exists() else backup_root.parent
    try:
        free_bytes = shutil.disk_usage(storage_probe).free
    except OSError:
        free_bytes = 0
    source_bytes = sum(item["bytes"] for item in sqlite_payload.values())
    required_bytes = max(source_bytes * 2, 1024 * 1024 * 1024)
    add_check(
        "backup_space",
        "pass" if free_bytes >= required_bytes else "fail",
        (
            f"Backup storage has {_format_bytes(free_bytes)} free."
            if free_bytes >= required_bytes
            else f"Backup storage needs at least {_format_bytes(required_bytes)} free."
        ),
        free_bytes=free_bytes,
        required_bytes=required_bytes,
    )
    postgres_root = Path(str(postgres_config.get("config_dir") or "/postgres_data"))
    postgres_probe = postgres_root if postgres_root.exists() else postgres_root.parent
    try:
        postgres_free_bytes = shutil.disk_usage(postgres_probe).free
    except OSError:
        postgres_free_bytes = 0
    postgres_required_bytes = max(int(source_bytes * 1.5), 1024 * 1024 * 1024)
    add_check(
        "postgres_space",
        "pass" if postgres_free_bytes >= postgres_required_bytes else "fail",
        (
            f"PostgreSQL storage has {_format_bytes(postgres_free_bytes)} free."
            if postgres_free_bytes >= postgres_required_bytes
            else "PostgreSQL storage does not have enough free space for staging and cutover."
        ),
        free_bytes=postgres_free_bytes,
        required_bytes=postgres_required_bytes,
    )

    running = api_state.get_status(process_name) == "running" if api_state else None
    if key == "infinidysk":
        application_healthy = False
        health_message = "InfiniDysk is stopped; rehearsal remains structural only."
        health_status = "warn"
        if running:
            application_healthy, health_message = _infinidysk_application_health(
                api_state,
                process_name,
                instance,
            )
            health_status = "pass" if application_healthy else "fail"
        add_check(
            "infinidysk_application_health",
            health_status,
            health_message,
            application_healthy=application_healthy,
            cutover_requires_healthy=True,
        )
    failures = [item for item in checks if item["status"] == "fail"]
    warnings = [item for item in checks if item["status"] == "warn"]
    return {
        "process_name": process_name,
        "service_key": key,
        "instance_name": instance_name,
        "supported": True,
        "ready": not failures,
        "running": running,
        "postgres_enabled": already_postgres,
        "service_version": version,
        "service_source_commit": service_source_commit,
        "source_schema_fingerprint": source_schema_fingerprint,
        "source_path_fingerprint": source_path_fingerprint,
        "launch_config_fingerprint": launch_config_fingerprint,
        "source_migration_history_fingerprint": (source_migration_history_fingerprint),
        "database_contract": source_database_contract,
        "sqlite": sqlite_payload,
        "postgres": postgres_payload,
        "checks": checks,
        "failure_count": len(failures),
        "warning_count": len(warnings),
        "confirmation_text": f"MIGRATE {process_name}",
        "migration_notice": (
            "InfiniDysk v1.2.0+ uses a separate PostgreSQL migration history. DUMB "
            "migrates only the main db.sqlite; metrics.sqlite, warden.db, and "
            "usenet-migration.db remain application-owned SQLite stores. DUMB "
            "preserves the main SQLite snapshot for rollback and requires the source "
            "and staged target to match its exact supported schema contract before "
            "validating keys and row counts. Cutover records the exact installed "
            "InfiniDysk commit as the minimum PostgreSQL-compatible runtime; older "
            "or diverged releases, branches, and commits cannot be installed while "
            "PostgreSQL remains selected."
            if key == "infinidysk"
            else (
                (
                    "Servarr treats existing SQLite-to-PostgreSQL migration as unsupported. "
                    if key in ARR_SERVICE_KEYS
                    else "This service supports PostgreSQL but database-engine migration still requires downtime and validation. "
                )
                + "DUMB will preserve SQLite for rollback and validate every imported table."
            )
        ),
        "rehearsal_required": bool(SUPPORTED_SERVICES[key].get("rehearsal_required")),
        "preserved_sqlite_stores": (
            [str(path) for path in paths.get("auxiliary", [])]
            if key == "infinidysk"
            else []
        ),
        "supports_log_migration": "log" in database_names,
        "backup_root": str(Path(root)),
    }


def _backup_sqlite(
    source: Path,
    destination: Path,
    progress: Callable[[int, int], None] | None = None,
    *,
    require_direct_source: bool = False,
) -> None:
    source_identity = None
    if require_direct_source:
        source_identity = _direct_file_identity(source)
    elif not source.is_file():
        raise ArrPostgresMigrationError(f"SQLite source {source.name} is missing.")
    _ensure_private_directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise ArrPostgresMigrationError(
            f"SQLite backup destination already exists for {source.name}."
        )
    temporary_path = None
    temporary_descriptor = None
    source_connection = None
    destination_connection = None
    try:
        temporary_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=str(destination.parent),
        )
        temporary_path = Path(temporary_name)
        os.fchmod(temporary_descriptor, 0o600)
        os.close(temporary_descriptor)
        temporary_descriptor = None
        source_connection = sqlite3.connect(
            f"file:{source}?mode=ro", uri=True, timeout=60
        )
        if require_direct_source and _direct_file_identity(source) != source_identity:
            raise ArrPostgresMigrationError(
                f"SQLite source {source.name} changed while the backup was opening."
            )
        destination_connection = sqlite3.connect(str(temporary_path))

        def report(status, remaining, total):
            del status
            if progress:
                progress(max(total - remaining, 0), total)

        source_connection.backup(
            destination_connection, pages=4096, progress=report, sleep=0.05
        )
        destination_connection.close()
        destination_connection = None
        source_connection.close()
        source_connection = None
        if require_direct_source and _direct_file_identity(source) != source_identity:
            raise ArrPostgresMigrationError(
                f"SQLite source {source.name} changed while the backup was running."
            )
        snapshot_identity = _direct_file_identity(temporary_path)
        healthy, message = _sqlite_quick_check(temporary_path)
        if not healthy:
            raise ArrPostgresMigrationError(
                f"SQLite backup integrity check failed for {source.name}: {message}"
            )
        foreign_key_violations = _sqlite_foreign_key_violations(temporary_path)
        if foreign_key_violations:
            raise ArrPostgresMigrationError(
                f"SQLite backup foreign-key validation failed for {source.name}."
            )
        if _direct_file_identity(temporary_path) != snapshot_identity:
            raise ArrPostgresMigrationError(
                f"SQLite backup {source.name} changed while it was being validated."
            )
        temporary_path.chmod(0o600)
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _postgres_table_columns(connection, table: str) -> dict[str, dict[str, str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name, data_type, is_generated, is_identity, "
            "identity_generation "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s "
            "ORDER BY ordinal_position",
            [table],
        )
        return {
            row[0]: {
                "data_type": row[1],
                "is_generated": row[2],
                "is_identity": row[3],
                "identity_generation": row[4],
            }
            for row in cursor.fetchall()
        }


def _convert_value(value, data_type: str):
    if value is None:
        return None
    if data_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            if value in {0, 1}:
                return bool(value)
            raise ArrPostgresMigrationError(
                "SQLite boolean values must be exactly 0 or 1."
            )
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"0", "false"}:
                return False
            if normalized in {"1", "true"}:
                return True
        raise ArrPostgresMigrationError(
            "SQLite boolean values must be exactly 0/1 or true/false."
        )
    if data_type in {"smallint", "integer", "bigint"} and isinstance(value, bool):
        return int(value)
    if data_type == "bytea" and isinstance(value, memoryview):
        return value.tobytes()
    if data_type.startswith("timestamp"):
        numeric = value
        if isinstance(value, str):
            try:
                numeric = float(value)
            except ValueError:
                numeric = None
        if isinstance(numeric, (int, float)):
            # SQLite applications sometimes store Unix milliseconds.
            if abs(numeric) > 100_000_000_000:
                numeric /= 1000
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
    return value


def _prepare_target_for_import(
    sqlite_path: Path,
    postgres_config: dict[str, Any],
    database: str,
    excluded_tables: set[str] | None = None,
    service_key: str | None = None,
) -> list[str]:
    source = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True, timeout=60)
    target = _pg_connect(postgres_config, database)
    try:
        source_tables = _sqlite_tables(source, excluded_tables)
        if service_key == "infinidysk" and set(source_tables) != set(
            INFINIDYSK_POSTGRES_TABLES
        ):
            raise ArrPostgresMigrationError(
                "InfiniDysk SQLite must contain exactly the 23 application tables "
                "in DUMB's supported database contract."
            )
        missing = [
            table
            for table in source_tables
            if not _postgres_table_columns(target, table)
        ]
        if missing:
            raise ArrPostgresMigrationError(
                "PostgreSQL schema is missing SQLite tables: " + ", ".join(missing[:10])
            )
        if service_key == "infinidysk":
            asymmetric_tables = []
            for table in source_tables:
                source_columns = set(_sqlite_columns(source, table))
                target_columns = set(_postgres_table_columns(target, table))
                if source_columns != target_columns:
                    asymmetric_tables.append(table)
            if asymmetric_tables:
                raise ArrPostgresMigrationError(
                    "InfiniDysk SQLite and official v1.2 PostgreSQL column sets "
                    "differ for: " + ", ".join(asymmetric_tables[:10])
                )
        with target.cursor() as cursor:
            cursor.execute("SET session_replication_role = replica")
            if source_tables:
                identifiers = sql.SQL(", ").join(
                    sql.Identifier(table) for table in source_tables
                )
                cursor.execute(
                    sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(
                        identifiers
                    )
                )
        target.commit()
        return source_tables
    finally:
        source.close()
        target.close()


def _postgres_sequence_specs(connection) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT columns.table_name, columns.column_name, "
            "columns.column_default, columns.is_identity, "
            "columns.identity_generation, "
            "sequence_namespace.nspname, sequence_class.relname, "
            "sequence_class.oid "
            "FROM information_schema.columns AS columns "
            "LEFT JOIN pg_class AS sequence_class ON sequence_class.oid = "
            "pg_get_serial_sequence(format('%I.%I', columns.table_schema, "
            "columns.table_name), columns.column_name)::regclass "
            "LEFT JOIN pg_namespace AS sequence_namespace "
            "ON sequence_namespace.oid = sequence_class.relnamespace "
            "WHERE columns.table_schema = 'public' "
            "AND (columns.column_default LIKE 'nextval(%' "
            "OR columns.is_identity = 'YES') "
            "ORDER BY columns.table_name, columns.column_name"
        )
        return [
            {
                "table": str(table),
                "column": str(column),
                "default": default,
                "is_identity": str(is_identity).upper() == "YES",
                "identity_generation": str(identity_generation or "").upper(),
                "sequence_schema": str(sequence_schema),
                "sequence": str(sequence_name),
                "sequence_oid": int(sequence_oid),
            }
            for (
                table,
                column,
                default,
                is_identity,
                identity_generation,
                sequence_schema,
                sequence_name,
                sequence_oid,
            ) in cursor.fetchall()
            if sequence_schema and sequence_name and sequence_oid
        ]


def _reset_postgres_sequences(connection) -> int:
    entries = _postgres_sequence_specs(connection)
    with connection.cursor() as cursor:
        for entry in entries:
            table = entry["table"]
            column = entry["column"]
            cursor.execute(
                sql.SQL("SELECT MAX({}) FROM {}").format(
                    sql.Identifier(column), sql.Identifier(table)
                )
            )
            maximum = cursor.fetchone()[0]
            if maximum is None:
                cursor.execute(
                    "SELECT pg_catalog.setval(%s::oid::regclass, 1, false)",
                    [entry["sequence_oid"]],
                )
            else:
                cursor.execute(
                    "SELECT pg_catalog.setval(%s::oid::regclass, %s, true)",
                    [entry["sequence_oid"], maximum],
                )
    connection.commit()
    return len(entries)


def _validate_postgres_sequences(
    connection,
    required_identities: tuple[tuple[str, str], ...] = (),
) -> list[dict[str, Any]]:
    specs = _postgres_sequence_specs(connection)
    actual_identities = {
        (entry["table"], entry["column"]) for entry in specs if entry["is_identity"]
    }
    missing = set(required_identities) - actual_identities
    if missing:
        raise ArrPostgresMigrationError(
            "PostgreSQL identity columns are missing: "
            + ", ".join(f"{table}.{column}" for table, column in sorted(missing))
        )

    validated = []
    with connection.cursor() as cursor:
        for entry in specs:
            cursor.execute(
                sql.SQL("SELECT MAX({}) FROM {}").format(
                    sql.Identifier(entry["column"]),
                    sql.Identifier(entry["table"]),
                )
            )
            maximum = cursor.fetchone()[0]
            cursor.execute(
                sql.SQL("SELECT last_value, is_called FROM {}").format(
                    sql.Identifier(entry["sequence_schema"], entry["sequence"])
                )
            )
            last_value, is_called = cursor.fetchone()
            cursor.execute(
                "SELECT increment_by FROM pg_sequences "
                "WHERE schemaname = %s AND sequencename = %s",
                [entry["sequence_schema"], entry["sequence"]],
            )
            increment_row = cursor.fetchone()
            increment = int(increment_row[0] if increment_row else 1)
            next_value = int(last_value) + (increment if is_called else 0)
            if maximum is not None and next_value <= int(maximum):
                raise ArrPostgresMigrationError(
                    "PostgreSQL sequence validation failed for "
                    f"{entry['table']}.{entry['column']}."
                )
            validated.append(
                {
                    "table": entry["table"],
                    "column": entry["column"],
                    "identity": entry["is_identity"],
                    "maximum": maximum,
                    "next_value": next_value,
                }
            )
    return validated


def _postgres_foreign_key_specs(connection) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT constraint_entry.conname, constraint_entry.convalidated, "
            "constraint_entry.confdeltype, "
            "child_table.relname, parent_table.relname, "
            "ARRAY(SELECT child_attribute.attname "
            "FROM unnest(constraint_entry.conkey) WITH ORDINALITY "
            "AS key_entry(attnum, position) "
            "JOIN pg_attribute AS child_attribute "
            "ON child_attribute.attrelid = constraint_entry.conrelid "
            "AND child_attribute.attnum = key_entry.attnum "
            "ORDER BY key_entry.position), "
            "ARRAY(SELECT parent_attribute.attname "
            "FROM unnest(constraint_entry.confkey) WITH ORDINALITY "
            "AS key_entry(attnum, position) "
            "JOIN pg_attribute AS parent_attribute "
            "ON parent_attribute.attrelid = constraint_entry.confrelid "
            "AND parent_attribute.attnum = key_entry.attnum "
            "ORDER BY key_entry.position) "
            "FROM pg_constraint AS constraint_entry "
            "JOIN pg_class AS child_table "
            "ON child_table.oid = constraint_entry.conrelid "
            "JOIN pg_class AS parent_table "
            "ON parent_table.oid = constraint_entry.confrelid "
            "WHERE constraint_entry.contype = 'f' "
            "AND constraint_entry.connamespace = 'public'::regnamespace "
            "ORDER BY constraint_entry.conname"
        )
        return [
            {
                "name": str(name),
                "validated": bool(validated),
                "delete_action": str(delete_action),
                "child_table": str(child_table),
                "parent_table": str(parent_table),
                "child_columns": [str(item) for item in child_columns],
                "parent_columns": [str(item) for item in parent_columns],
            }
            for (
                name,
                validated,
                delete_action,
                child_table,
                parent_table,
                child_columns,
                parent_columns,
            ) in cursor.fetchall()
        ]


def _validate_postgres_foreign_keys(
    connection,
    expected_names: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    specs = _postgres_foreign_key_specs(connection)
    actual_names = {entry["name"] for entry in specs}
    if expected_names and actual_names != set(expected_names):
        raise ArrPostgresMigrationError(
            "PostgreSQL foreign-key catalog does not match InfiniDysk v1.2."
        )
    if any(not entry["validated"] for entry in specs):
        raise ArrPostgresMigrationError(
            "PostgreSQL contains an unvalidated foreign-key constraint."
        )
    if expected_names and any(entry["delete_action"] != "c" for entry in specs):
        raise ArrPostgresMigrationError(
            "InfiniDysk PostgreSQL foreign keys must use cascading deletes."
        )
    if set(expected_names) == set(INFINIDYSK_POSTGRES_FOREIGN_KEYS):
        for entry in specs:
            expected_layout = INFINIDYSK_POSTGRES_FOREIGN_KEY_LAYOUTS.get(entry["name"])
            actual_layout = (
                entry["child_table"],
                tuple(entry["child_columns"]),
                entry["parent_table"],
                tuple(entry["parent_columns"]),
            )
            if actual_layout != expected_layout:
                raise ArrPostgresMigrationError(
                    "InfiniDysk PostgreSQL foreign-key columns do not match v1.2.0."
                )

    validated = []
    with connection.cursor() as cursor:
        for entry in specs:
            pairs = list(zip(entry["child_columns"], entry["parent_columns"]))
            if not pairs:
                raise ArrPostgresMigrationError(
                    f"PostgreSQL foreign key {entry['name']} has no columns."
                )
            join_condition = sql.SQL(" AND ").join(
                sql.SQL("{}.{} = {}.{}").format(
                    sql.Identifier("child_row"),
                    sql.Identifier(child_column),
                    sql.Identifier("parent_row"),
                    sql.Identifier(parent_column),
                )
                for child_column, parent_column in pairs
            )
            non_null_condition = sql.SQL(" AND ").join(
                sql.SQL("{}.{} IS NOT NULL").format(
                    sql.Identifier("child_row"), sql.Identifier(child_column)
                )
                for child_column, _ in pairs
            )
            cursor.execute(
                sql.SQL(
                    "SELECT COUNT(*) FROM {} AS {} WHERE {} AND NOT EXISTS "
                    "(SELECT 1 FROM {} AS {} WHERE {})"
                ).format(
                    sql.Identifier(entry["child_table"]),
                    sql.Identifier("child_row"),
                    non_null_condition,
                    sql.Identifier(entry["parent_table"]),
                    sql.Identifier("parent_row"),
                    join_condition,
                )
            )
            violations = int(cursor.fetchone()[0])
            if violations:
                raise ArrPostgresMigrationError(
                    f"PostgreSQL foreign-key validation failed for {entry['name']}."
                )
            validated.append(
                {
                    "name": entry["name"],
                    "validated": True,
                    "violations": 0,
                }
            )
    return validated


def _normalize_digest_value(value: Any, data_type: str) -> Any:
    """Normalize a value as PostgreSQL stores it without exposing its content."""

    if value is None:
        return None
    normalized_type = str(data_type or "").lower()
    if normalized_type == "boolean":
        return {"boolean": _convert_value(value, "boolean")}
    if normalized_type in {"smallint", "integer", "bigint"}:
        try:
            return {"integer": str(int(value))}
        except (TypeError, ValueError) as error:
            raise ArrPostgresMigrationError(
                "A SQLite integer value could not be normalized for validation."
            ) from error
    if normalized_type == "uuid":
        try:
            return {"uuid": str(uuid.UUID(str(value)))}
        except (AttributeError, TypeError, ValueError) as error:
            raise ArrPostgresMigrationError(
                "A SQLite UUID value could not be normalized for validation."
            ) from error
    if normalized_type == "bytea":
        if isinstance(value, memoryview):
            value = value.tobytes()
        if isinstance(value, bytearray):
            value = bytes(value)
        if not isinstance(value, bytes):
            raise ArrPostgresMigrationError(
                "A SQLite binary value could not be normalized for validation."
            )
        return {"bytea": value.hex()}
    if normalized_type.startswith("timestamp"):
        converted = _convert_value(value, normalized_type)
        if isinstance(converted, str):
            candidate = converted.strip()
            try:
                timestamp_match = _POSTGRES_TIMESTAMP_FRACTION_RE.fullmatch(candidate)
                fraction = (
                    timestamp_match.group("fraction") if timestamp_match else None
                )
                if timestamp_match and fraction and len(fraction) > 6:
                    suffix = timestamp_match.group("suffix") or ""
                    if suffix in {"Z", "z"}:
                        suffix = "+00:00"
                    converted = datetime.fromisoformat(
                        f"{timestamp_match.group('prefix')}{suffix}"
                    )
                    # PostgreSQL parses fractional seconds through binary
                    # double precision, multiplies by USECS_PER_SEC, and uses
                    # rint() to select the stored microsecond. Reproduce that
                    # path instead of decimal half-up/half-even arithmetic:
                    # exact-looking seven-digit decimal ties can sit just
                    # above or below .5 after their binary conversion.
                    microseconds = round(float(f"0.{fraction}") * 1_000_000)
                    converted += timedelta(microseconds=microseconds)
                else:
                    if candidate.endswith(("Z", "z")):
                        candidate = f"{candidate[:-1]}+00:00"
                    converted = datetime.fromisoformat(candidate)
            except ValueError as error:
                raise ArrPostgresMigrationError(
                    "A SQLite timestamp could not be normalized for validation."
                ) from error
        if not isinstance(converted, datetime):
            raise ArrPostgresMigrationError(
                "A SQLite timestamp could not be normalized for validation."
            )
        if "without time zone" in normalized_type:
            if converted.tzinfo is not None:
                converted = converted.astimezone(timezone.utc).replace(tzinfo=None)
            timestamp_value = converted.isoformat(timespec="microseconds")
        else:
            if converted.tzinfo is None:
                converted = converted.replace(tzinfo=timezone.utc)
            converted = converted.astimezone(timezone.utc)
            timestamp_value = converted.isoformat(timespec="microseconds")
        return {"timestamp": timestamp_value}
    return {"text": str(value)}


def _digest_rows(
    rows,
    data_types: list[str],
    *,
    source_values: bool,
) -> dict[str, Any]:
    count = 0
    xor_value = 0
    sum_value = 0
    modulus = 1 << 256
    for row in rows:
        if len(row) != len(data_types):
            raise ArrPostgresMigrationError(
                "A database row did not match the expected validation columns."
            )
        normalized = [
            _normalize_digest_value(
                _convert_value(value, data_type) if source_values else value,
                data_type,
            )
            for value, data_type in zip(row, data_types)
        ]
        digest = hashlib.sha256(
            json.dumps(
                normalized,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).digest()
        numeric = int.from_bytes(digest, "big")
        xor_value ^= numeric
        sum_value = (sum_value + numeric) % modulus
        count += 1
    return {
        "count": count,
        "xor": f"{xor_value:064x}",
        "sum": f"{sum_value:064x}",
    }


def _validate_full_row_digests(
    source: sqlite3.Connection,
    target,
    tables: list[str],
) -> int:
    """Verify every imported value using order-independent per-table digests."""

    validated = 0
    for table in tables:
        columns = _sqlite_columns(source, table)
        target_columns = _postgres_table_columns(target, table)
        missing_columns = [column for column in columns if column not in target_columns]
        if missing_columns:
            raise ArrPostgresMigrationError(
                f"PostgreSQL table {table} is missing columns for content validation."
            )
        data_types = [target_columns[column]["data_type"] for column in columns]
        escaped_table = table.replace('"', '""')
        source_columns = ", ".join(
            f'"{column.replace(chr(34), chr(34) * 2)}"' for column in columns
        )
        source_digest = _digest_rows(
            source.execute(f'SELECT {source_columns} FROM "{escaped_table}"'),
            data_types,
            source_values=True,
        )
        cursor_name = f"dumb_full_{uuid.uuid4().hex}"
        with target.cursor(name=cursor_name) as cursor:
            cursor.itersize = INFINIDYSK_FULL_ROW_DIGEST_ITERSIZE
            cursor.execute(
                sql.SQL("SELECT {} FROM {}").format(
                    sql.SQL(", ").join(sql.Identifier(column) for column in columns),
                    sql.Identifier(table),
                )
            )
            target_digest = _digest_rows(
                cursor,
                data_types,
                source_values=False,
            )
        if target_digest != source_digest:
            raise ArrPostgresMigrationError(
                f"Full-row content validation failed for PostgreSQL table {table}."
            )
        validated += 1
    return validated


def _validate_primary_key_digests(
    source: sqlite3.Connection,
    target,
    tables: list[str],
) -> dict[str, dict[str, Any]]:
    results = {}
    for table in tables:
        escaped_table = table.replace('"', '""')
        source_info = source.execute(f'PRAGMA table_info("{escaped_table}")').fetchall()
        source_keys = [
            str(row[1])
            for row in sorted(source_info, key=lambda item: int(item[5] or 0))
            if int(row[5] or 0) > 0
        ]
        if not source_keys:
            raise ArrPostgresMigrationError(
                f"SQLite table {table} has no primary key for digest validation."
            )
        with target.cursor() as cursor:
            cursor.execute(
                "SELECT attribute.attname "
                "FROM pg_index AS index_entry "
                "JOIN pg_class AS table_entry "
                "ON table_entry.oid = index_entry.indrelid "
                "JOIN pg_namespace AS namespace_entry "
                "ON namespace_entry.oid = table_entry.relnamespace "
                "JOIN unnest(index_entry.indkey) WITH ORDINALITY "
                "AS key_entry(attnum, position) ON TRUE "
                "JOIN pg_attribute AS attribute "
                "ON attribute.attrelid = table_entry.oid "
                "AND attribute.attnum = key_entry.attnum "
                "WHERE namespace_entry.nspname = 'public' "
                "AND table_entry.relname = %s AND index_entry.indisprimary "
                "ORDER BY key_entry.position",
                [table],
            )
            target_keys = [str(row[0]) for row in cursor.fetchall()]
        if target_keys != source_keys:
            raise ArrPostgresMigrationError(
                f"Primary-key definition differs for PostgreSQL table {table}."
            )
        target_columns = _postgres_table_columns(target, table)
        key_data_types = [target_columns[column]["data_type"] for column in source_keys]

        source_columns = ", ".join(
            f'"{column.replace(chr(34), chr(34) * 2)}"' for column in source_keys
        )
        source_digest = _digest_rows(
            source.execute(f'SELECT {source_columns} FROM "{escaped_table}"'),
            key_data_types,
            source_values=True,
        )
        cursor_name = f"dumb_keys_{uuid.uuid4().hex}"
        with target.cursor(name=cursor_name) as cursor:
            cursor.itersize = INFINIDYSK_PRIMARY_KEY_DIGEST_ITERSIZE
            cursor.execute(
                sql.SQL("SELECT {} FROM {}").format(
                    sql.SQL(", ").join(
                        sql.Identifier(column) for column in target_keys
                    ),
                    sql.Identifier(table),
                )
            )
            target_digest = _digest_rows(
                cursor,
                key_data_types,
                source_values=False,
            )
        if target_digest != source_digest:
            raise ArrPostgresMigrationError(
                f"Primary-key digest validation failed for PostgreSQL table {table}."
            )
        results[table] = source_digest
    return results


def _validate_infinidysk_postgres_schema_connection(
    connection,
    expected_contract_id: str | None = None,
) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT "MigrationId" FROM "__EFMigrationsHistory" '
            'ORDER BY "MigrationId"'
        )
        migration_history = tuple(str(row[0]) for row in cursor.fetchall())
        cursor.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> '__EFMigrationsHistory' "
            "ORDER BY tablename"
        )
        tables = tuple(str(row[0]) for row in cursor.fetchall())
        cursor.execute(
            "SELECT procedure_entry.proname FROM pg_proc AS procedure_entry "
            "JOIN pg_namespace AS namespace_entry "
            "ON namespace_entry.oid = procedure_entry.pronamespace "
            "WHERE namespace_entry.nspname = 'public' "
            "ORDER BY procedure_entry.proname"
        )
        functions = tuple(str(row[0]) for row in cursor.fetchall())
        cursor.execute(
            "SELECT trigger_entry.tgname, trigger_entry.tgenabled, "
            "table_entry.relname, procedure_entry.proname "
            "FROM pg_trigger AS trigger_entry "
            "JOIN pg_class AS table_entry "
            "ON table_entry.oid = trigger_entry.tgrelid "
            "JOIN pg_namespace AS namespace_entry "
            "ON namespace_entry.oid = table_entry.relnamespace "
            "JOIN pg_proc AS procedure_entry "
            "ON procedure_entry.oid = trigger_entry.tgfoid "
            "WHERE namespace_entry.nspname = 'public' "
            "AND NOT trigger_entry.tgisinternal "
            "ORDER BY trigger_entry.tgname"
        )
        trigger_rows = [
            (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
            for row in cursor.fetchall()
        ]

    contract = next(
        (
            candidate
            for candidate in INFINIDYSK_DATABASE_CONTRACTS
            if migration_history == candidate["postgres_migrations"]
        ),
        None,
    )
    if contract is None:
        raise ArrPostgresMigrationError(
            "InfiniDysk PostgreSQL migration history does not match a supported "
            "database contract."
        )
    if expected_contract_id and contract["id"] != expected_contract_id:
        raise ArrPostgresMigrationError(
            "InfiniDysk SQLite and staged PostgreSQL database contracts differ "
            f"({expected_contract_id} source, {contract['id']} target). Install the "
            "InfiniDysk runtime matching the source database before migrating."
        )
    if tables != tuple(sorted(INFINIDYSK_POSTGRES_TABLES)):
        raise ArrPostgresMigrationError(
            "InfiniDysk PostgreSQL must contain exactly 23 supported application "
            "tables."
        )
    if functions != tuple(sorted(INFINIDYSK_POSTGRES_FUNCTIONS)):
        raise ArrPostgresMigrationError(
            "InfiniDysk PostgreSQL trigger functions do not match the supported "
            "database contract."
        )
    trigger_names = tuple(name for name, _, _, _ in trigger_rows)
    if (
        trigger_names != tuple(sorted(INFINIDYSK_POSTGRES_TRIGGERS))
        or any(enabled != "O" for _, enabled, _, _ in trigger_rows)
        or any(
            (table, function) != INFINIDYSK_POSTGRES_TRIGGER_BINDINGS.get(name)
            for name, _, table, function in trigger_rows
        )
    ):
        raise ArrPostgresMigrationError(
            "InfiniDysk PostgreSQL must contain all eight supported enabled triggers."
        )

    foreign_keys = _postgres_foreign_key_specs(connection)
    if {entry["name"] for entry in foreign_keys} != set(
        INFINIDYSK_POSTGRES_FOREIGN_KEYS
    ) or any(
        not entry["validated"]
        or entry["delete_action"] != "c"
        or (
            entry["child_table"],
            tuple(entry["child_columns"]),
            entry["parent_table"],
            tuple(entry["parent_columns"]),
        )
        != INFINIDYSK_POSTGRES_FOREIGN_KEY_LAYOUTS.get(entry["name"])
        for entry in foreign_keys
    ):
        raise ArrPostgresMigrationError(
            "InfiniDysk PostgreSQL foreign-key catalog does not match the supported "
            "database contract."
        )
    sequences = _postgres_sequence_specs(connection)
    identities = {
        (entry["table"], entry["column"]) for entry in sequences if entry["is_identity"]
    }
    if identities != set(INFINIDYSK_POSTGRES_IDENTITIES) or any(
        entry["identity_generation"] != "BY DEFAULT"
        for entry in sequences
        if entry["is_identity"]
    ):
        raise ArrPostgresMigrationError(
            "InfiniDysk PostgreSQL identity columns do not match the supported "
            "database contract."
        )

    catalog = {
        "migration_history": list(migration_history),
        "tables": list(tables),
        "functions": list(functions),
        "triggers": [
            {
                "name": name,
                "enabled": enabled,
                "table": table,
                "function": function,
            }
            for name, enabled, table, function in trigger_rows
        ],
        "foreign_keys": [
            {
                "name": entry["name"],
                "child_table": entry["child_table"],
                "child_columns": entry["child_columns"],
                "parent_table": entry["parent_table"],
                "parent_columns": entry["parent_columns"],
                "delete_action": entry["delete_action"],
                "validated": entry["validated"],
            }
            for entry in foreign_keys
        ],
        "identities": [
            {
                "column": f"{entry['table']}.{entry['column']}",
                "generation": entry["identity_generation"],
            }
            for entry in sequences
            if entry["is_identity"]
        ],
    }
    catalog["fingerprint"] = hashlib.sha256(
        json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if catalog["fingerprint"] != contract["postgres_schema_fingerprint"]:
        raise ArrPostgresMigrationError(
            "InfiniDysk PostgreSQL catalog fingerprint does not match the audited "
            "database contract. Update DUMB before migrating this runtime."
        )
    catalog["database_contract"] = contract["id"]
    catalog["adapter_schema"] = contract["adapter_schema"]
    return catalog


def _validate_infinidysk_postgres_schema(
    postgres_config: dict[str, Any],
    database: str,
    expected_contract_id: str | None = None,
) -> dict[str, Any]:
    connection = _pg_connect(postgres_config, database)
    try:
        return _validate_infinidysk_postgres_schema_connection(
            connection,
            expected_contract_id=expected_contract_id,
        )
    finally:
        connection.close()


def _estimated_import_value_bytes(value: Any) -> int:
    if value is None:
        return 1
    if isinstance(value, memoryview):
        return value.nbytes
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8", errors="surrogatepass"))
    if isinstance(value, datetime):
        return len(value.isoformat().encode("ascii"))
    return len(str(value).encode("utf-8", errors="replace"))


def _converted_import_batches(
    source_cursor,
    columns: list[str],
    target_columns: dict[str, dict[str, str]],
    *,
    batch_size: int,
    max_batch_bytes: int | None,
):
    """Yield converted rows bounded by aggregate bytes between atomic rows.

    A single SQLite row is never split and may therefore exceed the aggregate
    target on its own; the following batch starts empty.
    """

    batch_size = max(1, int(batch_size))

    def convert(row):
        return tuple(
            _convert_value(value, target_columns[column]["data_type"])
            for column, value in zip(columns, row)
        )

    if max_batch_bytes is None:
        while True:
            rows = source_cursor.fetchmany(batch_size)
            if not rows:
                return
            yield [convert(row) for row in rows]

    byte_limit = max(1, int(max_batch_bytes))
    batch = []
    batch_bytes = 0
    while True:
        row = source_cursor.fetchone()
        if row is None:
            break
        converted = convert(row)
        row_bytes = 16 * len(converted) + sum(
            _estimated_import_value_bytes(value) for value in converted
        )
        if batch and (len(batch) >= batch_size or batch_bytes + row_bytes > byte_limit):
            yield batch
            batch = []
            batch_bytes = 0
        batch.append(converted)
        batch_bytes += row_bytes
        if batch_bytes >= byte_limit:
            yield batch
            batch = []
            batch_bytes = 0
    if batch:
        yield batch


def import_sqlite_to_postgres(
    sqlite_path: str | Path,
    postgres_config: dict[str, Any],
    database: str,
    progress: Callable[[dict[str, Any]], None] | None = None,
    batch_size: int = 500,
    excluded_tables: set[str] | None = None,
    service_key: str | None = None,
) -> dict[str, Any]:
    """Import data into an application-created PostgreSQL schema and validate counts."""
    sqlite_path = Path(sqlite_path)
    tables = _prepare_target_for_import(
        sqlite_path,
        postgres_config,
        database,
        excluded_tables,
        service_key=service_key,
    )
    source = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True, timeout=60)
    target = _pg_connect(postgres_config, database)
    imported: dict[str, int] = {}
    try:
        target.autocommit = False
        with target.cursor() as cursor:
            cursor.execute("SET session_replication_role = replica")
        total_rows = 0
        source_counts = {}
        for table in tables:
            escaped = table.replace('"', '""')
            count = int(
                source.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0]
            )
            source_counts[table] = count
            total_rows += count

        processed_rows = 0
        for table_index, table in enumerate(tables, start=1):
            source_columns = _sqlite_columns(source, table)
            target_columns = _postgres_table_columns(target, table)
            columns = [
                column
                for column in source_columns
                if column in target_columns
                and target_columns[column]["is_generated"] != "ALWAYS"
            ]
            missing_columns = [
                column for column in source_columns if column not in target_columns
            ]
            if missing_columns:
                raise ArrPostgresMigrationError(
                    f"PostgreSQL table {table} is missing importable columns: "
                    + ", ".join(missing_columns[:10])
                )
            escaped = table.replace('"', '""')
            select_columns = ", ".join(
                f'"{column.replace(chr(34), chr(34) * 2)}"' for column in columns
            )
            source_cursor = source.execute(f'SELECT {select_columns} FROM "{escaped}"')
            imported_count = 0
            insert_query = sql.SQL("INSERT INTO {} ({}) VALUES %s").format(
                sql.Identifier(table),
                sql.SQL(", ").join(sql.Identifier(column) for column in columns),
            )
            max_batch_bytes = (
                INFINIDYSK_IMPORT_BATCH_BYTES if service_key == "infinidysk" else None
            )
            for converted in _converted_import_batches(
                source_cursor,
                columns,
                target_columns,
                batch_size=batch_size,
                max_batch_bytes=max_batch_bytes,
            ):
                with target.cursor() as cursor:
                    execute_values(
                        cursor,
                        insert_query,
                        converted,
                        page_size=len(converted),
                    )
                target.commit()
                imported_count += len(converted)
                processed_rows += len(converted)
                if progress:
                    progress(
                        {
                            "table": table,
                            "table_index": table_index,
                            "table_count": len(tables),
                            "processed_rows": processed_rows,
                            "total_rows": total_rows,
                        }
                    )
            imported[table] = imported_count

        sequence_count = _reset_postgres_sequences(target)
        with target.cursor() as cursor:
            cursor.execute("SET session_replication_role = origin")
        target.commit()

        mismatches = []
        with target.cursor() as cursor:
            for table, expected in source_counts.items():
                cursor.execute(
                    sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))
                )
                actual = int(cursor.fetchone()[0])
                if actual != expected:
                    mismatches.append(
                        {"table": table, "sqlite": expected, "postgres": actual}
                    )
        if mismatches:
            raise ArrPostgresMigrationError(
                "PostgreSQL row-count validation failed for: "
                + ", ".join(item["table"] for item in mismatches[:10])
            )
        required_identities = (
            INFINIDYSK_POSTGRES_IDENTITIES if service_key == "infinidysk" else ()
        )
        sequence_validation = _validate_postgres_sequences(
            target,
            required_identities=required_identities,
        )
        primary_key_digests = {}
        full_row_digests_validated = 0
        foreign_key_validation = []
        schema_validation = None
        if service_key == "infinidysk":
            primary_key_digests = _validate_primary_key_digests(
                source,
                target,
                tables,
            )
            full_row_digests_validated = _validate_full_row_digests(
                source,
                target,
                tables,
            )
            foreign_key_validation = _validate_postgres_foreign_keys(
                target,
                expected_names=INFINIDYSK_POSTGRES_FOREIGN_KEYS,
            )
            schema_validation = _validate_infinidysk_postgres_schema_connection(target)
        return {
            "database": database,
            "tables": len(tables),
            "rows": sum(imported.values()),
            "sequences_reset": sequence_count,
            "sequences_validated": len(sequence_validation),
            "primary_key_digests_validated": len(primary_key_digests),
            "full_row_digests_validated": full_row_digests_validated,
            "foreign_keys_validated": len(foreign_key_validation),
            "postgres_schema_fingerprint": (
                schema_validation["fingerprint"] if schema_validation else None
            ),
            "row_counts": imported,
            "validated": True,
        }
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()


def _set_database_entries(config_manager, database_names: list[str]) -> None:
    postgres_config = config_manager.get("postgres", {}) or {}
    postgres_config["enabled"] = True
    databases = postgres_config.setdefault("databases", [])
    for name in database_names:
        entry = next(
            (
                item
                for item in databases
                if isinstance(item, dict) and str(item.get("name")) == name
            ),
            None,
        )
        if entry is not None:
            entry["enabled"] = True
        else:
            databases.append({"name": name, "enabled": True})
    config_manager.save_config()


def _restore_database_entries(
    config_manager,
    database_names: list[str],
    original_postgres_config: dict[str, Any],
) -> None:
    postgres_config = config_manager.get("postgres", {}) or {}
    target_names = set(database_names)
    original_entries = {
        str(entry.get("name")): copy.deepcopy(entry)
        for entry in original_postgres_config.get("databases", [])
        if isinstance(entry, dict) and str(entry.get("name")) in target_names
    }
    retained = [
        entry
        for entry in postgres_config.get("databases", [])
        if not isinstance(entry, dict) or str(entry.get("name")) not in target_names
    ]
    retained.extend(original_entries[name] for name in sorted(original_entries))
    postgres_config["databases"] = retained
    original_enabled = bool(original_postgres_config.get("enabled"))
    unrelated_enabled = any(
        entry.get("enabled") is True
        for entry in retained
        if isinstance(entry, dict) and str(entry.get("name")) not in target_names
    )
    current_config = getattr(config_manager, "config", {})
    other_service_enabled = False
    if isinstance(current_config, dict):
        for service_key, service_value in current_config.items():
            if service_key == "postgres" or not isinstance(service_value, dict):
                continue
            candidates = [service_value]
            instances = service_value.get("instances")
            if isinstance(instances, dict):
                candidates.extend(
                    item for item in instances.values() if isinstance(item, dict)
                )
            if any(item.get("postgres_enabled") is True for item in candidates):
                other_service_enabled = True
                break
    postgres_config["enabled"] = (
        original_enabled or unrelated_enabled or other_service_enabled
    )
    config_manager.save_config()


def _initialize_database_names(
    postgres_config: dict[str, Any], database_names: list[str]
) -> None:
    params = _postgres_params(postgres_config)
    success, error = initialize_postgres_databases(
        params["host"],
        params["port"],
        params["user"],
        params["password"],
        [{"name": name, "enabled": True} for name in database_names],
    )
    if not success:
        raise ArrPostgresMigrationError(
            "PostgreSQL database initialization failed. Check PostgreSQL logs."
        ) from (RuntimeError(error) if error else None)


def _schema_ready(postgres_config: dict[str, Any], databases: list[str]) -> bool:
    for database in databases:
        if not _database_exists(postgres_config, database):
            return False
        summary = _postgres_database_summary(postgres_config, database)
        if summary["table_count"] < 2:
            return False
    return True


def _repair_altmount_postgres_migration_010(
    postgres_config: dict[str, Any], database: str
) -> bool:
    """Apply AltMount's intended v10 index when its bundled SQL cannot parse."""
    connection = None
    try:
        connection = _pg_connect(postgres_config, database)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT version_id FROM goose_db_version "
                "WHERE is_applied = TRUE ORDER BY id DESC LIMIT 1"
            )
            current = cursor.fetchone()
            if not current or int(current[0]) != 9:
                connection.rollback()
                return False

            cursor.execute(
                "SELECT EXISTS ("
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "AND table_name = 'import_queue' AND column_name = 'metadata'"
                ")"
            )
            column_exists = cursor.fetchone()
            if not column_exists or not column_exists[0]:
                connection.rollback()
                return False

            # AltMount v0.3.2 wraps only the cast operand, which PostgreSQL
            # rejects at ->>. Parenthesize the complete expression instead.
            # https://github.com/javi11/altmount/blob/main/internal/database/migrations/postgres/010_add_nzbdav_id_index.sql
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_import_queue_nzbdav_id "
                "ON import_queue ((metadata::jsonb ->> 'nzbdav_id'))"
            )
            cursor.execute(
                "INSERT INTO goose_db_version (version_id, is_applied) "
                "SELECT 10, TRUE WHERE NOT EXISTS ("
                "SELECT 1 FROM goose_db_version "
                "WHERE version_id = 10 AND is_applied = TRUE"
                ")"
            )
        connection.commit()
        return True
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                pass
        return False
    finally:
        if connection is not None:
            connection.close()


def _start_process(
    process_handler,
    process_name: str,
    service_config: dict[str, Any] | None = None,
) -> None:
    runtime_env = None
    if isinstance(service_config, dict):
        runtime_env = copy.deepcopy(service_config.get("env") or {})
    result = process_handler.start_process(process_name, env=runtime_env)
    if isinstance(result, tuple):
        success, error = result
    else:
        success, error = result, None
    if not success:
        raise ArrPostgresMigrationError(
            f"{process_name} failed to start. Check the service logs."
        ) from (RuntimeError(str(error)) if error else None)


def _ensure_bazarr_postgres_driver(process_handler, install_path: str):
    from utils.setup import _ensure_bazarr_postgres_driver as ensure_driver

    return ensure_driver(process_handler, install_path)


def _prepare_service_schema(
    key: str,
    instance: dict[str, Any],
    process_handler,
    *,
    postgres_config: dict[str, Any] | None = None,
    database: str | None = None,
    staging_config_path: Path | None = None,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
    helper_suffix: str = "",
    expected_contract_id: str | None = None,
) -> dict[str, Any] | None:
    """Run application-specific schema initialization before staging startup."""
    if key == "infinidysk":
        if postgres_config is None or not database or staging_config_path is None:
            raise ArrPostgresMigrationError(
                "InfiniDysk PostgreSQL schema staging was not fully configured."
            )
        runtime_command, runtime_error = _infinidysk_runtime_command(instance)
        if not runtime_command:
            raise ArrPostgresMigrationError(
                "InfiniDysk v1.2 PostgreSQL runtime artifacts are missing. "
                f"{runtime_error or ''}".strip()
            )
        _ensure_private_directory(staging_config_path)
        if owner_uid is not None and owner_gid is not None:
            staging_stat = staging_config_path.stat()
            if (staging_stat.st_uid, staging_stat.st_gid) != (owner_uid, owner_gid):
                os.chown(staging_config_path, owner_uid, owner_gid)

        migration_env = os.environ.copy()
        migration_env.update(instance.get("env") or {})
        migration_env["CONFIG_PATH"] = str(staging_config_path)
        migration_env["ASPNETCORE_URLS"] = "http://127.0.0.1:0"
        helper_name = "infinidysk_db_migration"
        if helper_suffix:
            helper_name = f"{helper_name}_{_safe_slug(helper_suffix)}"
        output_dir = str(
            instance.get("backend_output_dir")
            or os.path.join(str(instance.get("config_dir") or "/infinidysk"), "app")
        )
        result = process_handler.start_process(
            helper_name,
            output_dir,
            [*runtime_command, "--db-migration"],
            env=migration_env,
        )
        if isinstance(result, tuple):
            success, error = result
        else:
            success, error = result, None
        if not success:
            raise ArrPostgresMigrationError(
                "InfiniDysk v1.2 failed to initialize its isolated PostgreSQL schema."
            ) from (RuntimeError(str(error)) if error else None)
        _wait_for_schema_helper(process_handler, helper_name)
        if process_handler.returncode != 0:
            raise ArrPostgresMigrationError(
                "InfiniDysk v1.2 --db-migration failed in the isolated staging "
                "environment. Check the InfiniDysk service logs."
            )
        return _validate_infinidysk_postgres_schema(
            postgres_config,
            database,
            expected_contract_id=expected_contract_id,
        )

    if key == "bazarr":
        install_path = str(instance.get("config_dir") or "/opt/bazarr")
        success, error = _ensure_bazarr_postgres_driver(process_handler, install_path)
        if not success:
            raise ArrPostgresMigrationError(
                f"Bazarr PostgreSQL driver setup failed: {error}"
            )
        return None
    if key != "pulsarr":
        return None

    config_dir = str(instance.get("config_dir") or "/pulsarr")
    migration_script = os.path.join(config_dir, "migrations", "migrate.ts")
    if not os.path.isfile(migration_script):
        raise ArrPostgresMigrationError(
            f"Pulsarr migration script was not found at {migration_script}."
        )
    bun_bin = os.path.join(os.getenv("BUN_INSTALL", "/config/.bun"), "bin", "bun")
    migration_env = os.environ.copy()
    migration_env.update(instance.get("env") or {})
    migration_env["BUN_INSTALL"] = os.getenv("BUN_INSTALL", "/config/.bun")
    migration_env["PATH"] = (
        f"{os.path.dirname(bun_bin)}:{migration_env.get('PATH', '')}"
    )
    result = process_handler.start_process(
        "bun_migrate",
        config_dir,
        [bun_bin, "run", "--bun", "migrations/migrate.ts"],
        env=migration_env,
    )
    if isinstance(result, tuple):
        success, error = result
    else:
        success, error = result, None
    if not success:
        raise ArrPostgresMigrationError(
            "Pulsarr failed to initialize its PostgreSQL staging schema."
        ) from (RuntimeError(str(error)) if error else None)
    process_handler.wait("bun_migrate")
    if process_handler.returncode != 0:
        detail = (
            process_handler.stderr
            or process_handler.stdout
            or "migration command failed"
        )
        raise ArrPostgresMigrationError(
            f"Pulsarr failed to initialize its PostgreSQL staging schema: {detail}"
        )
    return None


def _stop_process(process_handler, process_name: str) -> None:
    process_handler.stop_process(process_name)


def _tracked_process_identity(
    process_handler,
    process_name: str,
    *,
    required: bool,
):
    process_names = getattr(process_handler, "process_names", None)
    prefixed_name = getattr(process_handler, "_prefixed_name", None)
    if not isinstance(process_names, dict) or not callable(prefixed_name):
        if not required:
            return None
        raise ArrPostgresMigrationError(
            "InfiniDysk's managed process could not be bound to the migration."
        )
    internal_name = (
        process_name if process_name in process_names else prefixed_name(process_name)
    )
    process = process_names.get(internal_name)
    if process is None or process.poll() is not None:
        if not required:
            return None
        raise ArrPostgresMigrationError(
            "InfiniDysk exited before DUMB could bind the migration operation."
        )
    try:
        process_group = os.getpgid(process.pid)
    except OSError:
        process_group = process.pid
    return process, process_group


def _stop_exact_tracked_process(
    process_handler,
    process_name: str,
    process,
    process_group: int,
    *,
    failure_message: str,
) -> None:
    current = _tracked_process_identity(process_handler, process_name, required=True)
    if current[0] is not process or current[1] != process_group:
        raise ArrPostgresMigrationError(
            "InfiniDysk's managed process changed after the health check; migration "
            "was refused."
        )

    process_handler.stop_process(process_name)
    group_alive = getattr(process_handler, "_process_group_alive", None)
    if (
        process.poll() is None
        or not callable(group_alive)
        or group_alive(process_group)
    ):
        raise ArrPostgresMigrationError(failure_message)


def _stop_tracked_infinidysk_process(
    process_handler,
    process_name: str,
    instance: dict[str, Any],
    expected_identity=None,
) -> None:
    """Stop and prove exit of the exact managed process that passed health."""

    process, process_group = expected_identity or _tracked_process_identity(
        process_handler,
        process_name,
        required=True,
    )
    _stop_exact_tracked_process(
        process_handler,
        process_name,
        process,
        process_group,
        failure_message=(
            "InfiniDysk or its process group remained active; the cold backup was "
            "refused."
        ),
    )
    _require_infinidysk_listener_stopped(
        instance,
        "InfiniDysk still owns or shares its backend port after the managed "
        "process group stopped; the cold backup was refused.",
    )


def _require_infinidysk_listener_stopped(
    instance: dict[str, Any], failure_message: str
) -> None:
    """Fail closed unless the configured backend port has no listener."""

    try:
        port = int(instance.get("backend_port") or 8080)
    except (TypeError, ValueError) as error:
        raise ArrPostgresMigrationError(
            "InfiniDysk has an invalid backend port; stopped state cannot be proven."
        ) from error
    if not is_port_available(port):
        raise ArrPostgresMigrationError(failure_message)


def _stop_infinidysk_if_running(
    process_handler,
    process_name: str,
    *,
    api_state=None,
    instance: dict[str, Any] | None = None,
) -> None:
    identity = _tracked_process_identity(
        process_handler,
        process_name,
        required=False,
    )
    if identity is None:
        api_reports_running = False
        get_status = getattr(api_state, "get_status", None)
        if callable(get_status):
            try:
                api_reports_running = get_status(process_name) == "running"
            except Exception:
                api_reports_running = True
        endpoint_healthy, _ = _infinidysk_loopback_health(instance or {})
        try:
            port = int((instance or {}).get("backend_port") or 8080)
        except (TypeError, ValueError) as error:
            raise ArrPostgresMigrationError(
                "InfiniDysk has an invalid backend port; SQLite rollback was refused."
            ) from error
        listener_present = not is_port_available(port)
        if api_reports_running or endpoint_healthy or listener_present:
            raise ArrPostgresMigrationError(
                "InfiniDysk may still be running without a bound DUMB process; "
                "SQLite rollback was refused."
            )
        return
    _stop_exact_tracked_process(
        process_handler,
        process_name,
        identity[0],
        identity[1],
        failure_message=(
            "InfiniDysk's PostgreSQL process group remained active; SQLite rollback "
            "was refused."
        ),
    )
    _require_infinidysk_listener_stopped(
        instance or {},
        "InfiniDysk still owns or shares its backend port after its PostgreSQL "
        "process group stopped; SQLite rollback was refused.",
    )
    endpoint_healthy, _ = _infinidysk_loopback_health(instance or {})
    if endpoint_healthy:
        raise ArrPostgresMigrationError(
            "InfiniDysk still answered after its tracked process group stopped; "
            "SQLite rollback was refused."
        )


def _wait_for_schema_helper(
    process_handler,
    process_name: str,
    timeout: int = 300,
) -> None:
    """Bound the migration-only helper and prove cleanup after a timeout."""

    identity = _tracked_process_identity(
        process_handler,
        process_name,
        required=False,
    )
    if identity is None:
        process_handler.wait(process_name)
        return
    process, process_group = identity
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _stop_exact_tracked_process(
            process_handler,
            process_name,
            process,
            process_group,
            failure_message=(
                "InfiniDysk's isolated --db-migration helper did not stop after its "
                "timeout."
            ),
        )
        raise ArrPostgresMigrationError(
            "InfiniDysk's isolated --db-migration helper exceeded its timeout."
        ) from error
    process_handler.returncode = process.returncode
    process_handler.wait(process_name)


def _managed_process_is_running(process_handler, process_name: str) -> bool:
    process_names = getattr(process_handler, "process_names", None)
    if not isinstance(process_names, dict):
        return True
    internal_name = process_name
    prefixed_name = getattr(process_handler, "_prefixed_name", None)
    if callable(prefixed_name):
        internal_name = prefixed_name(process_name)
    process = process_names.get(internal_name) or process_names.get(process_name)
    if process is None:
        return False
    try:
        return process.poll() is None
    except Exception:
        return False


def _wait_for_schema(
    postgres_config: dict[str, Any],
    databases: list[str],
    timeout: int = 180,
    *,
    process_handler=None,
    process_name: str | None = None,
    progress: Callable[[int, list[dict[str, Any]]], None] | None = None,
) -> None:
    started_at = time.time()
    deadline = time.time() + timeout
    next_progress_at = started_at
    while time.time() < deadline:
        summaries = []
        try:
            ready = True
            for database in databases:
                summary = _postgres_database_summary(postgres_config, database)
                summaries.append(summary)
                if not summary.get("exists") or summary.get("table_count", 0) < 2:
                    ready = False
            if ready:
                return
        except Exception:
            pass
        if (
            process_handler is not None
            and process_name
            and not _managed_process_is_running(process_handler, process_name)
        ):
            raise ArrPostgresMigrationError(
                f"{process_name} exited while initializing its PostgreSQL schema. "
                "Check the service logs."
            )
        now = time.time()
        if progress and now >= next_progress_at:
            progress(int(now - started_at), summaries)
            next_progress_at = now + 10
        time.sleep(2)
    raise ArrPostgresMigrationError(
        "The service did not initialize its PostgreSQL schema before the timeout."
    )


def _wait_for_running_service(
    api_state,
    process_name: str,
    timeout: int = 60,
    *,
    require_application_health: bool = False,
    instance: dict[str, Any] | None = None,
) -> None:
    if not api_state:
        if require_application_health:
            raise ArrPostgresMigrationError(
                "InfiniDysk application health could not be verified after startup."
            )
        time.sleep(3)
        return
    deadline = time.time() + timeout
    stable_since = None
    while time.time() < deadline:
        running = api_state.get_status(process_name) == "running"
        application_healthy = True
        if require_application_health:
            application_healthy, _ = _infinidysk_application_health(
                api_state,
                process_name,
                instance or {},
            )
        if running and application_healthy:
            stable_since = stable_since or time.time()
            if time.time() - stable_since >= 5:
                return
        else:
            stable_since = None
        time.sleep(1)
    raise ArrPostgresMigrationError(
        "Service did not become application-healthy after PostgreSQL cutover."
        if require_application_health
        else "Service did not remain running after PostgreSQL cutover."
    )


def _clone_database(
    postgres_config: dict[str, Any], source_database: str, target_database: str
) -> None:
    connection = _pg_connect(postgres_config, "postgres")
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname IN (%s, %s) AND pid <> pg_backend_pid()",
                [source_database, target_database],
            )
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(target_database)
                )
            )
            cursor.execute(
                sql.SQL("CREATE DATABASE {} WITH TEMPLATE {} OWNER {}").format(
                    sql.Identifier(target_database),
                    sql.Identifier(source_database),
                    sql.Identifier(postgres_config.get("user", "DUMB")),
                )
            )
    finally:
        connection.close()


def _drop_database(postgres_config: dict[str, Any], database: str) -> None:
    connection = _pg_connect(postgres_config, "postgres")
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                [database],
            )
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database))
            )
    finally:
        connection.close()


class ArrPostgresMigrationManager:
    """Persistent job coordinator used by the process API."""

    def __init__(self, root: str | Path = DEFAULT_ROOT):
        self.root = Path(root)
        self.jobs_dir = self.root / "jobs"
        self.backups_dir = self.root / "backups"
        self._lock = threading.Lock()
        self._worker_id = secrets.token_hex(16)
        self._active_processes: set[str] = set()
        self._last_progress_write: dict[str, float] = {}

    @staticmethod
    def _normalize_job_id(job_id: str) -> str:
        normalized_job_id = str(job_id or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", normalized_job_id):
            raise ArrPostgresMigrationError("Invalid migration job ID.")
        return normalized_job_id

    def _new_job_path(self, job_id: uuid.UUID) -> Path:
        """Return the path for a server-generated job ID."""

        jobs_root = self.jobs_dir.resolve(strict=False)
        return jobs_root / f"{job_id.hex}.json"

    def _create_job(self, job_id: uuid.UUID, payload: dict[str, Any]) -> None:
        """Persist a new job whose identifier was generated by the server."""

        if payload.get("job_id") != job_id.hex:
            raise ArrPostgresMigrationError("Migration job ID does not match payload.")
        _ensure_private_directory(self.root)
        _ensure_private_child_directory(self.root, self.jobs_dir)
        _ensure_private_child_directory(self.root, self.backups_dir)
        payload["updated_at"] = int(time.time())
        _atomic_json(self._new_job_path(job_id), payload)

    def _find_job_path(self, job_id: str) -> Path | None:
        """Find a validated job by enumerating the fixed jobs directory."""

        normalized_job_id = self._normalize_job_id(job_id)
        jobs_root = self.jobs_dir.resolve(strict=False)
        if not jobs_root.is_dir():
            return None
        try:
            jobs_stat = jobs_root.lstat()
        except OSError:
            return None
        if (
            stat.S_ISLNK(jobs_stat.st_mode)
            or not stat.S_ISDIR(jobs_stat.st_mode)
            or (jobs_stat.st_uid, jobs_stat.st_gid) != (os.geteuid(), os.getegid())
            or stat.S_IMODE(jobs_stat.st_mode) != 0o700
        ):
            return None
        expected_name = f"{normalized_job_id}.json"
        try:
            candidates = jobs_root.iterdir()
            for candidate in candidates:
                if candidate.name != expected_name or candidate.is_symlink():
                    continue
                candidate_stat = candidate.lstat()
                if (
                    not stat.S_ISREG(candidate_stat.st_mode)
                    or candidate_stat.st_nlink != 1
                    or candidate_stat.st_size > MAX_MIGRATION_JOB_BYTES
                    or (candidate_stat.st_uid, candidate_stat.st_gid)
                    != (os.geteuid(), os.getegid())
                    or stat.S_IMODE(candidate_stat.st_mode) != 0o600
                ):
                    continue
                resolved = candidate.resolve(strict=True)
                if resolved.parent == jobs_root and resolved.is_file():
                    return resolved
        except OSError:
            return None
        return None

    def _save(self, payload: dict[str, Any]) -> None:
        payload["updated_at"] = int(time.time())
        path = self._find_job_path(payload["job_id"])
        if path is None:
            raise ArrPostgresMigrationError("Migration job file was not found.")
        _atomic_json(path, payload)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        try:
            normalized_job_id = self._normalize_job_id(job_id)
            path = self._find_job_path(normalized_job_id)
        except ArrPostgresMigrationError:
            return None
        if path is None:
            return None
        try:
            with path.open("rb") as handle:
                opened_stat = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(opened_stat.st_mode)
                    or opened_stat.st_nlink != 1
                    or opened_stat.st_size > MAX_MIGRATION_JOB_BYTES
                    or (opened_stat.st_uid, opened_stat.st_gid)
                    != (os.geteuid(), os.getegid())
                    or stat.S_IMODE(opened_stat.st_mode) != 0o600
                ):
                    return None
                raw_payload = handle.read(MAX_MIGRATION_JOB_BYTES + 1)
                if len(raw_payload) > MAX_MIGRATION_JOB_BYTES:
                    return None
                payload = json.loads(raw_payload)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if payload.get("job_id") != normalized_job_id:
            return None
        if payload.get("status") in ACTIVE_JOB_STATUSES:
            worker_id = str(payload.get("worker_id") or "")
            if not worker_id or not secrets.compare_digest(worker_id, self._worker_id):
                payload["status"] = "interrupted"
                payload["error"] = {
                    "message": "The DUMB API restarted while this migration was active. "
                    "Use rollback before retrying."
                }
                self._save(payload)
        return payload

    def latest_job(self, process_name: str) -> dict[str, Any] | None:
        if not self.jobs_dir.is_dir():
            return None
        candidates = []
        for path in self.jobs_dir.glob("*.json"):
            payload = self.get_job(path.stem)
            if payload and payload.get("process_name") == process_name:
                candidates.append(payload)
        if not candidates:
            return None
        return max(candidates, key=lambda item: int(item.get("updated_at") or 0))

    def has_active_infinidysk_job(self) -> bool:
        """Return whether an InfiniDysk job blocks config/lifecycle admission."""

        if not self.jobs_dir.is_dir():
            return False
        for path in self.jobs_dir.glob("*.json"):
            payload = self.get_job(path.stem)
            if (
                payload
                and payload.get("service_key") == "infinidysk"
                and (
                    payload.get("status") in ACTIVE_JOB_STATUSES
                    or payload.get("status") == "rollback_failed"
                    or (
                        payload.get("status") in {"failed", "interrupted"}
                        and payload.get("rollback_available") is True
                    )
                )
            ):
                return True
        return False

    def has_infinidysk_recovery_pending_job(self) -> bool:
        """Return whether a terminal InfiniDysk job requires recovery attention."""

        if not self.jobs_dir.is_dir():
            return False
        for path in self.jobs_dir.glob("*.json"):
            payload = self.get_job(path.stem)
            if not payload or payload.get("service_key") != "infinidysk":
                continue
            status = payload.get("status")
            if status == "rollback_failed" or (
                status in {"failed", "interrupted"}
                and payload.get("rollback_available") is True
            ):
                return True
        return False

    def has_namespace_conflicting_job(self) -> bool:
        """Return whether any service migration conflicts with namespace work."""

        if not self.jobs_dir.is_dir():
            return False
        for path in self.jobs_dir.glob("*.json"):
            payload = self.get_job(path.stem)
            if not payload:
                continue
            status = payload.get("status")
            if (
                status in ACTIVE_JOB_STATUSES
                or status == "rollback_failed"
                or (
                    status in {"failed", "interrupted"}
                    and payload.get("rollback_available") is True
                )
            ):
                return True
        return False

    def _matching_rehearsal(
        self,
        process_name: str,
        binding: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self.jobs_dir.is_dir():
            return None
        candidates = []
        for path in self.jobs_dir.glob("*.json"):
            payload = self.get_job(path.stem)
            result = (payload or {}).get("result") or {}
            main_import = (result.get("imports") or {}).get("main") or {}
            infini_contract_valid = True
            if (payload or {}).get("service_key") == "infinidysk":
                database_contract = _infinidysk_contract_for_binding(
                    (payload or {}).get("binding")
                )
                infini_contract_valid = (
                    database_contract is not None
                    and result.get("adapter_schema")
                    == database_contract["adapter_schema"]
                    and result.get("postgres_schema_fingerprint")
                    == database_contract["postgres_schema_fingerprint"]
                    and result.get("postgres_schema_fingerprint")
                    == main_import.get("postgres_schema_fingerprint")
                    and main_import.get("validated") is True
                    and main_import.get("tables") == len(INFINIDYSK_POSTGRES_TABLES)
                    and main_import.get("primary_key_digests_validated")
                    == len(INFINIDYSK_POSTGRES_TABLES)
                    and main_import.get("full_row_digests_validated")
                    == len(INFINIDYSK_POSTGRES_TABLES)
                    and main_import.get("foreign_keys_validated")
                    == len(INFINIDYSK_POSTGRES_FOREIGN_KEYS)
                    and main_import.get("sequences_validated")
                    == len(INFINIDYSK_POSTGRES_IDENTITIES)
                )
            if (
                payload
                and payload.get("process_name") == process_name
                and payload.get("status") == "completed"
                and payload.get("mode") == "rehearsal"
                and payload.get("binding") == binding
                and result.get("validated") is True
                and result.get("binding") == binding
                and infini_contract_valid
            ):
                candidates.append(payload)
        if not candidates:
            return None
        return max(candidates, key=lambda item: int(item.get("updated_at") or 0))

    def _progress(
        self,
        payload: dict[str, Any],
        stage: str,
        message: str,
        percent: int,
        **details,
    ) -> None:
        now = time.monotonic()
        job_id = payload["job_id"]
        if (
            stage in {"backup", "import"}
            and now - self._last_progress_write.get(job_id, 0) < 0.75
        ):
            return
        self._last_progress_write[job_id] = now
        event = {
            "at": int(time.time()),
            "stage": stage,
            "message": message,
            "percent": max(0, min(100, int(percent))),
        }
        if details:
            event["details"] = details
        events = payload.setdefault("events", [])
        events.append(event)
        payload["events"] = events[-100:]
        payload["progress"] = event
        self._save(payload)

    def create_job(
        self,
        config_manager,
        process_handler,
        api_state,
        logger,
        process_name: str,
        mode: str,
        include_logs: bool,
        confirmation: str,
        acknowledge_unsupported: bool,
        acknowledge_backup: bool,
        acknowledge_target_reset: bool,
    ) -> dict[str, Any]:
        finder = getattr(config_manager, "find_key_for_process", None)
        service_key = None
        if callable(finder):
            try:
                service_key, _ = finder(process_name)
            except Exception:
                service_key = None
        with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
            # Import lazily so the two managers share an admission boundary without
            # creating a module-import cycle.
            from utils.infinidysk_migration import INFINIDYSK_MIGRATION_MANAGER

            if INFINIDYSK_MIGRATION_MANAGER.has_blocking_job():
                raise ArrPostgresMigrationError(ACTIVE_NAMESPACE_MIGRATION_BLOCKER)
            if service_key == "infinidysk" and infinidysk_external_mutation_active():
                raise ArrPostgresMigrationError(EXTERNAL_MUTATION_BLOCKER)
            if service_key == "infinidysk" and self.has_active_infinidysk_job():
                raise ArrPostgresMigrationError(
                    "An InfiniDysk PostgreSQL migration is active or has an unused "
                    "guarded rollback. Resolve it before starting another job."
                )
            return self._create_job_admitted(
                config_manager,
                process_handler,
                api_state,
                logger,
                process_name,
                mode,
                include_logs,
                confirmation,
                acknowledge_unsupported,
                acknowledge_backup,
                acknowledge_target_reset,
            )

    def _create_job_admitted(
        self,
        config_manager,
        process_handler,
        api_state,
        logger,
        process_name: str,
        mode: str,
        include_logs: bool,
        confirmation: str,
        acknowledge_unsupported: bool,
        acknowledge_backup: bool,
        acknowledge_target_reset: bool,
    ) -> dict[str, Any]:
        if mode not in {"rehearsal", "cutover"}:
            raise ArrPostgresMigrationError("mode must be rehearsal or cutover")
        expected = f"MIGRATE {process_name}"
        if confirmation != expected:
            raise ArrPostgresMigrationError(f"Type '{expected}' to authorize the job.")
        if not all(
            [acknowledge_unsupported, acknowledge_backup, acknowledge_target_reset]
        ):
            raise ArrPostgresMigrationError(
                "All migration risk and backup confirmations are required."
            )
        preflight = build_arr_postgres_preflight(
            config_manager, process_name, api_state, self.root
        )
        if not preflight["ready"]:
            raise ArrPostgresMigrationError(
                "Migration preflight has blocking failures. Resolve them before starting."
            )
        if mode == "cutover" and preflight["service_key"] == "infinidysk":
            _, _, current_instance = _resolve_instance(config_manager, process_name)
            application_healthy, health_message = _infinidysk_application_health(
                api_state,
                process_name,
                current_instance,
            )
            if not application_healthy:
                raise ArrPostgresMigrationError(
                    "InfiniDysk cutover requires the existing SQLite service to be "
                    f"running and application-healthy: {health_message}"
                )
        binding = _preflight_binding(preflight)
        rehearsal = None
        if mode == "cutover" and SUPPORTED_SERVICES[preflight["service_key"]].get(
            "rehearsal_required"
        ):
            rehearsal = self._matching_rehearsal(process_name, binding)
            if rehearsal is None:
                raise ArrPostgresMigrationError(
                    "InfiniDysk cutover requires a successful rehearsal for the "
                    "same installed version, SQLite schema, and target database."
                )
        with self._lock:
            latest = self.latest_job(process_name)
            if process_name in self._active_processes or (
                latest and latest.get("status") in ACTIVE_JOB_STATUSES
            ):
                raise ArrPostgresMigrationError(
                    "A migration job is already active for this service."
                )
            self._active_processes.add(process_name)

        job_uuid = uuid.uuid4()
        job_id = job_uuid.hex
        payload = {
            "job_id": job_id,
            "process_name": process_name,
            "service_key": preflight["service_key"],
            "instance_name": preflight["instance_name"],
            "mode": mode,
            "include_logs": bool(
                include_logs and preflight.get("supports_log_migration")
            ),
            "status": "queued",
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
            "worker_pid": os.getpid(),
            "worker_id": self._worker_id,
            "progress": None,
            "events": [],
            "result": None,
            "error": None,
            "rollback": None,
            "rollback_available": False,
            "preflight": preflight,
            "binding": binding,
            "rehearsal_job_id": rehearsal.get("job_id") if rehearsal else None,
        }
        self._create_job(job_uuid, payload)

        thread = threading.Thread(
            target=self._run_job,
            args=(
                payload,
                config_manager,
                process_handler,
                api_state,
                logger,
            ),
            daemon=True,
            name=f"postgres-migration-{_safe_slug(process_name)}",
        )
        thread.start()
        return {
            "status": "queued",
            "job_id": job_id,
            "process_name": process_name,
            "mode": mode,
        }

    def _restore_sqlite_runtime(
        self,
        payload,
        config_manager,
        process_handler,
        key,
        instance_name,
        instance,
        paths,
        config_backup: Path | None,
        postgres_config,
        was_running: bool,
        *,
        sqlite_backups: dict[str, Path] | None = None,
        binding: dict[str, Any] | None = None,
        api_state=None,
        original_postgres_config: dict[str, Any] | None = None,
        rollback_state: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        rollback_state = rollback_state if rollback_state is not None else {}
        rollback_state.setdefault("mutation_started", False)
        if key == "infinidysk":
            _stop_infinidysk_if_running(
                process_handler,
                payload["process_name"],
                api_state=api_state,
                instance=instance,
            )
            main_snapshot = (sqlite_backups or {}).get("main")
            if payload.get("rollback_checkpoint") == "cold_backup_verified":
                if main_snapshot is None:
                    raise ArrPostgresMigrationError(
                        "The verified InfiniDysk SQLite rollback snapshot is missing."
                    )
                backup_dir = _validate_private_child_directory(
                    self.backups_dir,
                    Path(str(payload.get("backup_dir") or "")),
                )
                main_snapshot = _validate_private_backup_file(main_snapshot, backup_dir)
                _validate_infinidysk_snapshot(main_snapshot, binding or {})
                rollback_state["mutation_started"] = True
                _restore_infinidysk_sqlite_snapshot(
                    main_snapshot,
                    paths["main"],
                    binding or {},
                )
            else:
                _direct_file_identity(paths["main"])
                _validate_infinidysk_snapshot(paths["main"], binding or {})
                healthy, message = _sqlite_quick_check(paths["main"])
                if not healthy or _sqlite_foreign_key_violations(paths["main"]):
                    raise ArrPostgresMigrationError(
                        "The untouched InfiniDysk SQLite source could not be safely "
                        f"restarted after backup failure: {message}"
                    )
        else:
            try:
                _stop_process(process_handler, payload["process_name"])
            except Exception:
                pass
        config_path = paths.get("config_xml")
        if config_backup and config_backup.is_file() and config_path:
            rollback_state["mutation_started"] = True
            shutil.copy2(config_backup, config_path)
        rollback_state["mutation_started"] = True
        instance["postgres_enabled"] = False
        databases = _database_names(key, instance_name, instance)
        _apply_database_config(
            key,
            instance_name,
            instance,
            paths,
            postgres_config,
            databases,
            enabled=False,
        )
        config_manager.save_config(payload["process_name"])
        if original_postgres_config is not None:
            _restore_database_entries(
                config_manager,
                list(databases.values()),
                original_postgres_config,
            )
        if key == "infinidysk":
            _clear_infinidysk_rollback_authorization(self.root)
        restarted = False
        if was_running:
            _start_process(process_handler, payload["process_name"], instance)
            _wait_for_running_service(
                api_state,
                payload["process_name"],
                require_application_health=(key == "infinidysk"),
                instance=instance if key == "infinidysk" else None,
            )
            restarted = True
        return {
            "restored_config": str(config_path) if config_path else None,
            "sqlite_preserved": True,
            "service_restarted": restarted,
        }

    def _run_job(
        self, payload, config_manager, process_handler, api_state, logger
    ) -> None:
        process_name = payload["process_name"]
        key, instance_name, instance = _resolve_instance(config_manager, process_name)
        paths = _source_paths(key, instance)
        spec = SUPPORTED_SERVICES[key]
        postgres_config = config_manager.get("postgres", {}) or {}
        original_postgres_config = copy.deepcopy(postgres_config)
        database_map = _database_names(key, instance_name, instance)
        database_names = list(database_map.values())
        original_instance = copy.deepcopy(instance)
        was_running = (
            api_state.get_status(process_name) == "running" if api_state else True
        )
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup_dir = (
            self.backups_dir
            / _safe_slug(process_name)
            / f"{timestamp}-{payload['job_id'][:8]}"
        )
        config_path = paths.get("config_xml")
        config_backup = (
            backup_dir / config_path.name
            if config_path and config_path.is_file()
            else None
        )
        sqlite_backups = {
            label: backup_dir / paths[label].name for label in database_map
        }
        stage_suffix = payload["job_id"][:8]
        stage_database_map = {
            label: f"dumb_stage_{key}_{stage_suffix}_{label}" for label in database_map
        }
        stage_databases = list(stage_database_map.values())
        staging_config_path = None
        runtime_restored = False
        infinidysk_stop_attempted = False
        payload["status"] = "running"
        payload["started_at"] = int(time.time())
        payload["worker_pid"] = os.getpid()
        payload["worker_id"] = self._worker_id
        payload["backup_dir"] = str(backup_dir)
        payload["app_config_backup"] = str(config_backup) if config_backup else None
        payload["was_running"] = was_running
        self._save(payload)
        try:
            if key == "infinidysk" and payload["mode"] == "cutover":
                _tracked_process_identity(
                    process_handler,
                    process_name,
                    required=True,
                )
                application_healthy, health_message = _infinidysk_application_health(
                    api_state,
                    process_name,
                    instance,
                )
                if not application_healthy:
                    raise ArrPostgresMigrationError(
                        "InfiniDysk stopped being application-healthy before "
                        f"cutover: {health_message}"
                    )
                was_running = True
                payload["was_running"] = True
            if key == "infinidysk":
                if infinidysk_launch_config_fingerprint(instance) != (
                    payload.get("binding") or {}
                ).get("launch_config_fingerprint"):
                    raise ArrPostgresMigrationError(
                        "InfiniDysk's launch configuration changed after preflight. "
                        "Run rehearsal again before migration."
                    )
                current_fingerprint = _sqlite_schema_fingerprint(
                    paths["main"], set(INFINIDYSK_TRANSIENT_TABLES)
                )
                if current_fingerprint != (payload.get("binding") or {}).get(
                    "source_schema_fingerprint"
                ):
                    raise ArrPostgresMigrationError(
                        "The SQLite schema changed after preflight. Run preflight and "
                        "rehearsal again before migration."
                    )
            self._progress(payload, "backup", "Creating rollback backup.", 5)
            _ensure_private_child_directory(self.root, self.backups_dir)
            _ensure_private_child_directory(self.root, backup_dir.parent)
            _ensure_private_child_directory(self.root, backup_dir)
            if config_backup and config_path:
                _copy_private_file(config_path, config_backup)
            config_file = Path(str(getattr(config_manager, "file_path", "")))
            if config_file.is_file():
                _copy_private_file(config_file, backup_dir / "dumb_config.json")
            elif payload["mode"] == "cutover":
                raise ArrPostgresMigrationError(
                    "DUMB configuration could not be backed up before cutover."
                )
            if payload["mode"] == "cutover":
                _validate_private_backup_file(
                    backup_dir / "dumb_config.json", backup_dir
                )

            backup_labels = ["main"]
            if payload["include_logs"] and "log" in database_map:
                backup_labels.append("log")
            if payload["mode"] == "cutover" and was_running:
                self._progress(
                    payload,
                    "stopping",
                    f"Stopping {process_name} for a cold backup.",
                    8,
                )
                if key == "infinidysk":
                    healthy_process = _tracked_process_identity(
                        process_handler,
                        process_name,
                        required=True,
                    )
                    application_healthy, health_message = (
                        _infinidysk_application_health(
                            api_state,
                            process_name,
                            instance,
                        )
                    )
                    if not application_healthy:
                        raise ArrPostgresMigrationError(
                            "InfiniDysk stopped being application-healthy immediately "
                            f"before shutdown: {health_message}"
                        )
                    if (
                        _tracked_process_identity(
                            process_handler,
                            process_name,
                            required=True,
                        )
                        != healthy_process
                    ):
                        raise ArrPostgresMigrationError(
                            "InfiniDysk's process changed during its final health "
                            "check; cutover was refused."
                        )
                    if infinidysk_launch_config_fingerprint(instance) != (
                        payload.get("binding") or {}
                    ).get("launch_config_fingerprint"):
                        raise ArrPostgresMigrationError(
                            "InfiniDysk's launch configuration changed during the "
                            "final health check; cutover was refused."
                        )
                    infinidysk_stop_attempted = True
                    _stop_tracked_infinidysk_process(
                        process_handler,
                        process_name,
                        instance,
                        expected_identity=healthy_process,
                    )
                else:
                    _stop_process(process_handler, process_name)
            for index, label in enumerate(backup_labels):

                def backup_progress(done, total, current_label=label):
                    fraction = (done / total) if total else 0
                    percent = 10 + int(
                        ((index + fraction) / max(len(backup_labels), 1)) * 15
                    )
                    self._progress(
                        payload,
                        "backup",
                        f"Backing up {current_label} SQLite database.",
                        percent,
                        pages_done=done,
                        pages_total=total,
                    )

                _backup_sqlite(
                    paths[label],
                    sqlite_backups[label],
                    backup_progress,
                    require_direct_source=(key == "infinidysk"),
                )
                if key == "infinidysk" and label == "main":
                    _validate_infinidysk_snapshot(
                        sqlite_backups[label],
                        payload.get("binding") or {},
                    )

            if payload["mode"] == "cutover":
                _validate_private_backup_file(
                    backup_dir / "dumb_config.json", backup_dir
                )
                _validate_private_backup_file(sqlite_backups["main"], backup_dir)
                payload["rollback_available"] = True
                payload["rollback_checkpoint"] = "cold_backup_verified"
                self._save(payload)

            self._progress(
                payload,
                "postgres",
                "Creating isolated PostgreSQL staging databases.",
                28,
            )
            # The remaining-stage list is mutated as each database is removed.
            # Keep the initializer's argument stable for callers and diagnostics.
            _initialize_database_names(postgres_config, list(stage_databases))

            self._progress(
                payload,
                "schema",
                "Initializing the service's current PostgreSQL schema.",
                32,
            )
            if key != "infinidysk" and was_running and payload["mode"] == "rehearsal":
                _stop_process(process_handler, process_name)
            schema_instance = (
                copy.deepcopy(original_instance) if key == "infinidysk" else instance
            )
            _apply_database_config(
                key,
                instance_name,
                schema_instance,
                paths,
                postgres_config,
                stage_database_map,
                enabled=True,
            )
            schema_catalog = None
            if key == "infinidysk":
                staging_config_path = Path(
                    tempfile.mkdtemp(
                        prefix=f"dumb-infinidysk-pg-{payload['job_id'][:8]}-",
                        dir="/tmp",
                    )
                )
            try:
                owner_uid = int(config_manager.get("puid", os.geteuid()))
                owner_gid = int(config_manager.get("pgid", os.getegid()))
                schema_catalog = _prepare_service_schema(
                    key,
                    schema_instance,
                    process_handler,
                    postgres_config=postgres_config,
                    database=stage_database_map["main"],
                    staging_config_path=staging_config_path,
                    owner_uid=owner_uid,
                    owner_gid=owner_gid,
                    helper_suffix=payload["job_id"][:8],
                    expected_contract_id=(
                        (payload.get("binding") or {}).get("database_contract")
                        if key == "infinidysk"
                        else None
                    ),
                )
                if schema_catalog is None:
                    try:
                        _start_process(process_handler, process_name, instance)
                    except ArrPostgresMigrationError:
                        repaired = (
                            key == "altmount"
                            and _repair_altmount_postgres_migration_010(
                                postgres_config, stage_database_map["main"]
                            )
                        )
                        if not repaired:
                            raise
                        if logger:
                            logger.warning(
                                "Applied the AltMount PostgreSQL migration 010 compatibility repair."
                            )
                        self._progress(
                            payload,
                            "schema",
                            "Repaired AltMount's PostgreSQL expression index; retrying schema initialization.",
                            33,
                        )
                        _start_process(process_handler, process_name, instance)
                    _wait_for_schema(
                        postgres_config,
                        stage_databases,
                        process_handler=process_handler,
                        process_name=process_name,
                        progress=lambda elapsed, summaries: self._progress(
                            payload,
                            "schema",
                            "Waiting for the service to create its PostgreSQL tables.",
                            min(38, 32 + elapsed // 30),
                            elapsed_seconds=elapsed,
                            databases=[
                                {
                                    "name": summary.get("name"),
                                    "table_count": summary.get("table_count", 0),
                                }
                                for summary in summaries
                            ],
                        ),
                    )
                    _stop_process(process_handler, process_name)
            finally:
                if staging_config_path is not None:
                    shutil.rmtree(staging_config_path)
                    staging_config_path = None

            instance.clear()
            instance.update(copy.deepcopy(original_instance))
            if config_backup and config_path:
                shutil.copyfile(config_backup, config_path)

            import_databases = database_map
            if payload["mode"] == "rehearsal":
                import_databases = stage_database_map
                if key == "infinidysk" and was_running:
                    _wait_for_running_service(
                        api_state,
                        process_name,
                        require_application_health=True,
                        instance=instance,
                    )
                elif was_running:
                    _start_process(process_handler, process_name, instance)
                    _wait_for_running_service(api_state, process_name)
                runtime_restored = True
            else:
                _set_database_entries(config_manager, database_names)
                for label, target_db in database_map.items():
                    _clone_database(
                        postgres_config, stage_database_map[label], target_db
                    )

            results = {}
            import_labels = ["main"]
            if payload["include_logs"] and "log" in database_map:
                import_labels.append("log")
            excluded_tables = set(spec.get("excluded_tables") or ())
            for import_index, label in enumerate(import_labels):
                lower = 40 + import_index * int(45 / len(import_labels))
                span = int(45 / len(import_labels))

                def import_progress(event, current_label=label):
                    total = int(event.get("total_rows") or 0)
                    done = int(event.get("processed_rows") or 0)
                    fraction = done / total if total else 1
                    self._progress(
                        payload,
                        "import",
                        f"Importing {current_label}: {event.get('table')}",
                        lower + int(span * fraction),
                        database=import_databases[current_label],
                        **event,
                    )

                results[label] = import_sqlite_to_postgres(
                    sqlite_backups[label],
                    postgres_config,
                    import_databases[label],
                    import_progress,
                    excluded_tables=excluded_tables,
                    service_key=key,
                )

            self._progress(payload, "validation", "Validating imported data.", 88)
            key_counts = {}
            source_counts = _sqlite_row_counts(sqlite_backups["main"], excluded_tables)
            for table in spec["key_tables"]:
                if table in source_counts:
                    key_counts[table] = source_counts[table]

            postgres_schema_fingerprint = None
            postgres_adapter_schema = None
            if key == "infinidysk":
                main_result = results.get("main") or {}
                postgres_schema_fingerprint = main_result.get(
                    "postgres_schema_fingerprint"
                )
                expected_schema_fingerprint = (schema_catalog or {}).get("fingerprint")
                postgres_adapter_schema = (schema_catalog or {}).get("adapter_schema")
                if (
                    main_result.get("validated") is not True
                    or main_result.get("tables") != len(INFINIDYSK_POSTGRES_TABLES)
                    or main_result.get("primary_key_digests_validated")
                    != len(INFINIDYSK_POSTGRES_TABLES)
                    or main_result.get("full_row_digests_validated")
                    != len(INFINIDYSK_POSTGRES_TABLES)
                    or main_result.get("foreign_keys_validated")
                    != len(INFINIDYSK_POSTGRES_FOREIGN_KEYS)
                    or main_result.get("sequences_validated")
                    != len(INFINIDYSK_POSTGRES_IDENTITIES)
                    or not postgres_schema_fingerprint
                    or postgres_schema_fingerprint != expected_schema_fingerprint
                ):
                    raise ArrPostgresMigrationError(
                        "InfiniDysk PostgreSQL import did not satisfy the exact "
                        "supported schema and content-validation contract."
                    )

            self._progress(
                payload,
                "cleanup",
                "Removing isolated PostgreSQL staging databases.",
                90,
            )
            for stage_database in tuple(stage_databases):
                _drop_database(postgres_config, stage_database)
                stage_databases.remove(stage_database)

            if payload["mode"] == "rehearsal":
                payload["status"] = "completed"
                payload["result"] = {
                    "mode": "rehearsal",
                    "validated": True,
                    "binding": copy.deepcopy(payload.get("binding") or {}),
                    "adapter_schema": (
                        postgres_adapter_schema if key == "infinidysk" else None
                    ),
                    "postgres_schema_fingerprint": postgres_schema_fingerprint,
                    "imports": results,
                    "key_row_counts": key_counts,
                    "cutover_performed": False,
                    "sqlite_runtime_restored": runtime_restored,
                    "runtime_probe_performed": False,
                }
                self._progress(
                    payload,
                    "completed",
                    "Rehearsal completed; the service remains on SQLite.",
                    100,
                )
            else:
                self._progress(
                    payload, "cutover", "Persisting PostgreSQL configuration.", 92
                )
                _apply_database_config(
                    key,
                    instance_name,
                    instance,
                    paths,
                    postgres_config,
                    database_map,
                    enabled=True,
                )
                config_manager.save_config(process_name)
                if key == "infinidysk":
                    if not was_running:
                        raise ArrPostgresMigrationError(
                            "InfiniDysk cutover cannot start from a stopped service."
                        )
                    with authorize_infinidysk_postgres_migration():
                        _start_process(process_handler, process_name, instance)
                    _wait_for_running_service(
                        api_state,
                        process_name,
                        require_application_health=True,
                        instance=instance,
                    )
                elif was_running:
                    _start_process(process_handler, process_name, instance)
                    _wait_for_running_service(api_state, process_name)
                postgres_physical_identity = None
                if key == "infinidysk":
                    postgres_physical_identity = infinidysk_postgres_physical_identity(
                        postgres_config, database_names[0]
                    )
                    if postgres_physical_identity is None:
                        raise ArrPostgresMigrationError(
                            "InfiniDysk cutover could not bind the live PostgreSQL "
                            "cluster and database identity."
                        )
                payload["status"] = "finalizing" if key == "infinidysk" else "completed"
                payload["rollback_available"] = True
                payload["result"] = {
                    "mode": "cutover",
                    "validated": True,
                    "binding": copy.deepcopy(payload.get("binding") or {}),
                    "adapter_schema": (
                        postgres_adapter_schema if key == "infinidysk" else None
                    ),
                    "postgres_schema_fingerprint": postgres_schema_fingerprint,
                    "rehearsal_job_id": payload.get("rehearsal_job_id"),
                    "application_health_verified": key == "infinidysk",
                    "imports": results,
                    "key_row_counts": key_counts,
                    "cutover_performed": True,
                    "postgres_databases": database_names,
                    "postgres_physical_identity": postgres_physical_identity,
                    "sqlite_backups": {
                        label: str(path)
                        for label, path in sqlite_backups.items()
                        if path.is_file()
                    },
                }
                self._progress(
                    payload,
                    "finalizing" if key == "infinidysk" else "completed",
                    (
                        "Finalizing durable PostgreSQL cutover authorization."
                        if key == "infinidysk"
                        else "PostgreSQL cutover completed and SQLite rollback was preserved."
                    ),
                    99 if key == "infinidysk" else 100,
                )
            if key == "infinidysk" and payload["mode"] == "cutover":
                # Keep the persisted job active until the controller-owned
                # authorization marker is durable. Rollback/lifecycle admission
                # must not reopen while this final write can still race it.
                self._save(payload)
                completion_payload = copy.deepcopy(payload)
                completion_payload["status"] = "completed"
                completion_payload["finished_at"] = int(time.time())
                record_infinidysk_postgres_migration_completion(
                    completion_payload,
                    migration_root=self.root,
                )
                payload["status"] = "completed"
                self._progress(
                    payload,
                    "completed",
                    "PostgreSQL cutover completed and SQLite rollback was preserved.",
                    100,
                )
            payload["finished_at"] = int(time.time())
            self._save(payload)
        except Exception as exc:
            logger.error("PostgreSQL migration failed for %s: %s", process_name, exc)
            rollback = None
            if (backup_dir / "dumb_config.json").is_file():
                payload["status"] = "rolling_back"
                self._save(payload)
                try:
                    if key == "infinidysk" and payload["mode"] == "rehearsal":
                        _restore_database_entries(
                            config_manager,
                            database_names,
                            original_postgres_config,
                        )
                        rollback = {
                            "restored": True,
                            "sqlite_preserved": True,
                            "service_unchanged": True,
                        }
                        runtime_restored = True
                    elif (
                        key == "infinidysk"
                        and payload["mode"] == "cutover"
                        and not infinidysk_stop_attempted
                        and payload.get("rollback_checkpoint") != "cold_backup_verified"
                    ):
                        rollback = {
                            "restored": True,
                            "sqlite_preserved": True,
                            "service_unchanged": True,
                        }
                        runtime_restored = True
                    else:
                        instance.clear()
                        instance.update(copy.deepcopy(original_instance))
                        rollback = self._restore_sqlite_runtime(
                            payload,
                            config_manager,
                            process_handler,
                            key,
                            instance_name,
                            instance,
                            paths,
                            config_backup,
                            postgres_config,
                            was_running,
                            sqlite_backups=sqlite_backups,
                            binding=payload.get("binding") or {},
                            api_state=api_state,
                            original_postgres_config=original_postgres_config,
                        )
                        runtime_restored = True
                except Exception as rollback_error:
                    logger.error(
                        "Automatic SQLite rollback failed for %s: %s",
                        process_name,
                        rollback_error,
                    )
                    rollback = {
                        "restored": False,
                        "message": "Automatic rollback failed. Restore the service and DUMB configuration from the job backup before restarting.",
                    }
            rollback_backup_valid = True
            if (
                key == "infinidysk"
                and payload.get("rollback_checkpoint") == "cold_backup_verified"
            ):
                try:
                    safe_backup_dir = _validate_private_child_directory(
                        self.backups_dir, backup_dir
                    )
                    _validate_private_backup_file(
                        safe_backup_dir / "dumb_config.json", safe_backup_dir
                    )
                    safe_snapshot = _validate_private_backup_file(
                        sqlite_backups["main"], safe_backup_dir
                    )
                    _validate_infinidysk_snapshot(
                        safe_snapshot, payload.get("binding") or {}
                    )
                except ArrPostgresMigrationError:
                    rollback_backup_valid = False
            payload["status"] = (
                "failed_rolled_back" if rollback and runtime_restored else "failed"
            )
            payload["error"] = {"message": str(exc)}
            payload["rollback"] = rollback
            payload["rollback_available"] = bool(
                payload.get("mode") == "cutover"
                and not runtime_restored
                and (
                    key != "infinidysk"
                    or payload.get("rollback_checkpoint") == "cold_backup_verified"
                )
                and (backup_dir / "dumb_config.json").is_file()
                and sqlite_backups["main"].is_file()
                and rollback_backup_valid
            )
            payload["finished_at"] = int(time.time())
            self._progress(
                payload,
                "failed",
                (
                    "Migration failed; the SQLite runtime was restored."
                    if runtime_restored
                    else "Migration failed and requires manual recovery."
                ),
                100,
            )
            self._save(payload)
        finally:
            for database in stage_databases:
                try:
                    _drop_database(postgres_config, database)
                except Exception as exc:
                    logger.warning(
                        "Failed to remove migration staging database %s: %s",
                        database,
                        exc,
                    )
            with self._lock:
                self._active_processes.discard(process_name)
            self._last_progress_write.pop(payload["job_id"], None)

    def rollback_job(
        self,
        job_id: str,
        confirmation: str,
        config_manager,
        process_handler,
        api_state,
    ) -> dict[str, Any]:
        preliminary = self.get_job(job_id)
        if not preliminary:
            raise ArrPostgresMigrationError("Migration job was not found.")
        service_key = preliminary.get("service_key")
        if service_key == "infinidysk":
            with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
                if infinidysk_namespace_migration_active():
                    raise ArrPostgresMigrationError(
                        "Cannot roll back InfiniDysk while a namespace migration job is active."
                    )
                reservation = self._reserve_rollback(
                    job_id,
                    confirmation,
                    config_manager,
                    api_state,
                    expected_service_key=service_key,
                )
        else:
            reservation = self._reserve_rollback(
                job_id,
                confirmation,
                config_manager,
                api_state,
                expected_service_key=service_key,
            )

        payload = reservation["payload"]
        process_name = reservation["process_name"]
        key = reservation["key"]
        instance_name = reservation["instance_name"]
        instance = reservation["instance"]
        paths = reservation["paths"]
        config_backup = reservation["config_backup"]
        original_postgres_config = reservation["original_postgres_config"]
        was_running = reservation["was_running"]
        sqlite_backups = reservation["sqlite_backups"]
        rollback_state = {"mutation_started": False}
        try:
            try:
                result = self._restore_sqlite_runtime(
                    payload,
                    config_manager,
                    process_handler,
                    key,
                    instance_name,
                    instance,
                    paths,
                    config_backup,
                    config_manager.get("postgres", {}) or {},
                    was_running,
                    sqlite_backups=sqlite_backups,
                    binding=payload.get("binding") or {},
                    api_state=api_state,
                    original_postgres_config=original_postgres_config,
                    rollback_state=rollback_state,
                )
            except Exception as error:
                retry_safe = not rollback_state["mutation_started"]
                payload["status"] = "rollback_failed"
                payload["rollback_available"] = retry_safe
                payload["rollback"] = {
                    "restored": False,
                    "retry_safe": retry_safe,
                    "message": (
                        "Rollback stopped before changing saved data and may be retried."
                        if retry_safe
                        else "Rollback did not complete after changes began; inspect the private backup before taking further action."
                    ),
                }
                payload["error"] = {
                    "message": "Guarded SQLite rollback did not complete."
                }
                payload["finished_at"] = int(time.time())
                self._progress(
                    payload,
                    "rollback_failed",
                    payload["rollback"]["message"],
                    100,
                )
                raise ArrPostgresMigrationError(
                    payload["rollback"]["message"]
                ) from error
            result["warning"] = (
                "Changes made after PostgreSQL cutover are not copied back into SQLite."
            )
            payload["status"] = "rolled_back"
            payload["rollback"] = result
            payload["rollback_available"] = False
            payload["finished_at"] = int(time.time())
            self._progress(
                payload, "rolled_back", "SQLite configuration restored.", 100
            )
            self._save(payload)
            return payload
        finally:
            with self._lock:
                self._active_processes.discard(process_name)

    def _reserve_rollback(
        self,
        job_id: str,
        confirmation: str,
        config_manager,
        api_state,
        *,
        expected_service_key: str | None,
    ) -> dict[str, Any]:
        """Validate and persist one exclusive rollback reservation."""

        with self._lock:
            payload = self.get_job(job_id)
            if not payload:
                raise ArrPostgresMigrationError("Migration job was not found.")
            if payload.get("service_key") != expected_service_key:
                raise ArrPostgresMigrationError(
                    "The migration job identity changed; retry the rollback request."
                )
            process_name = payload["process_name"]
            if confirmation != f"ROLLBACK {process_name}":
                raise ArrPostgresMigrationError(
                    f"Type 'ROLLBACK {process_name}' to authorize rollback."
                )
            status = str(payload.get("status") or "")
            if status in ACTIVE_JOB_STATUSES:
                raise ArrPostgresMigrationError(
                    "Cannot roll back while the job is active."
                )
            if (
                payload.get("mode") != "cutover"
                or payload.get("rollback_available") is not True
                or (
                    payload.get("service_key") == "infinidysk"
                    and payload.get("rollback_checkpoint") != "cold_backup_verified"
                )
            ):
                raise ArrPostgresMigrationError(
                    "This job does not have an unused guarded cutover rollback."
                )
            if status not in {
                "completed",
                "failed",
                "interrupted",
                "rollback_failed",
            }:
                raise ArrPostgresMigrationError(
                    "This migration state is not eligible for guarded rollback."
                )
            if (
                payload.get("service_key") == "infinidysk"
                and status == "completed"
                and not infinidysk_postgres_completed_job_valid(payload)
            ):
                raise ArrPostgresMigrationError(
                    "The completed InfiniDysk cutover evidence is invalid; rollback was refused."
                )
            key, instance_name, instance = _resolve_instance(
                config_manager, process_name
            )
            if key != payload.get("service_key") or instance_name != payload.get(
                "instance_name"
            ):
                raise ArrPostgresMigrationError(
                    "The current service identity does not match this migration job."
                )
            paths = _source_paths(key, instance)
            if key == "infinidysk":
                expected_path = str(
                    (
                        ((payload.get("preflight") or {}).get("sqlite") or {}).get(
                            "main"
                        )
                        or {}
                    ).get("path")
                    or ""
                )
                current_path = os.path.realpath(os.fspath(paths["main"]))
                if (
                    not expected_path
                    or os.path.realpath(expected_path) != current_path
                    or (payload.get("binding") or {}).get("source_path_fingerprint")
                    != infinidysk_sqlite_source_path_fingerprint(paths["main"])
                ):
                    raise ArrPostgresMigrationError(
                        "InfiniDysk's SQLite source path changed after cutover; rollback was refused."
                    )
            backup_dir = _validate_private_child_directory(
                self.backups_dir, Path(str(payload.get("backup_dir") or ""))
            )
            config_backup_value = payload.get("app_config_backup")
            config_backup = (
                Path(str(config_backup_value)) if config_backup_value else None
            )
            legacy_config_backup = backup_dir / "config.xml"
            if not config_backup and legacy_config_backup.is_file():
                config_backup = legacy_config_backup
            dumb_config_backup = _validate_private_backup_file(
                backup_dir / "dumb_config.json", backup_dir
            )
            if config_backup:
                config_backup = _validate_private_backup_file(config_backup, backup_dir)
            try:
                if dumb_config_backup.stat().st_size > MAX_MIGRATION_JOB_BYTES:
                    raise ValueError("configuration backup is too large")
                original_full_config = json.loads(dumb_config_backup.read_bytes())
                original_postgres_config = copy.deepcopy(
                    original_full_config.get("postgres") or {}
                )
            except (OSError, ValueError) as error:
                raise ArrPostgresMigrationError(
                    "The job's DUMB configuration backup is invalid."
                ) from error
            was_running = (
                api_state.get_status(process_name) == "running" if api_state else True
            )
            sqlite_backup_labels = ["main"]
            if payload.get("include_logs") and "log" in paths:
                sqlite_backup_labels.append("log")
            sqlite_backups = {
                label: _validate_private_backup_file(
                    backup_dir / paths[label].name, backup_dir
                )
                for label in sqlite_backup_labels
            }
            payload["status"] = "rolling_back"
            payload["worker_pid"] = os.getpid()
            payload["worker_id"] = self._worker_id
            self._save(payload)
            self._active_processes.add(process_name)
            return {
                "payload": payload,
                "process_name": process_name,
                "key": key,
                "instance_name": instance_name,
                "instance": instance,
                "paths": paths,
                "config_backup": config_backup,
                "original_postgres_config": original_postgres_config,
                "was_running": was_running,
                "sqlite_backups": sqlite_backups,
            }


# Generic names are canonical; Arr-prefixed exports remain for callers from the
# original Sonarr/Radarr-only implementation.
PostgresMigrationError = ArrPostgresMigrationError
PostgresMigrationManager = ArrPostgresMigrationManager
build_postgres_preflight = build_arr_postgres_preflight
POSTGRES_MIGRATION_MANAGER = PostgresMigrationManager()
ARR_POSTGRES_MIGRATION_MANAGER = POSTGRES_MIGRATION_MANAGER
