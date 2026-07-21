"""Guarded SQLite-to-PostgreSQL migration workflow for supported services.

The normal ``postgres_enabled`` option only changes the configured database
backend.  This module provides the deliberately separate, observable migration
workflow used by the API and frontend.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg2
import yaml
from psycopg2 import sql
from psycopg2.extras import execute_values

from utils.arr_postgres import apply_arr_postgres_config, arr_postgres_database_names
from utils.postgres import initialize_postgres_databases
from utils.service_postgres import (
    apply_service_postgres_config,
    service_postgres_database_name,
)

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
}
ARR_SERVICE_KEYS = {"sonarr", "radarr", "lidarr", "prowlarr", "whisparr"}
TERMINAL_JOB_STATUSES = {
    "completed",
    "failed",
    "failed_rolled_back",
    "rolled_back",
    "interrupted",
}
ACTIVE_JOB_STATUSES = {"queued", "running", "rolling_back"}
# Keep the original location so existing Sonarr/Radarr jobs and rollback backups
# remain available after the workflow expands beyond Arr applications.
DEFAULT_ROOT = "/config/arr-postgres-migration"


class ArrPostgresMigrationError(RuntimeError):
    """Expected migration failure safe to report without a traceback."""


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug or "service"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.replace(temporary, path)


def _version_tuple(value: str | None) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", str(value or ""))
    return tuple(int(item) for item in numbers[:4])


def _format_bytes(value: int | None) -> str:
    size = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


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


def _source_paths(key: str, instance: dict[str, Any]) -> dict[str, Path | None]:
    config_dir = Path(str(instance.get("config_dir") or ""))
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
    for label in database_names:
        path = paths[label]
        exists = path.is_file()
        size = path.stat().st_size if exists else 0
        healthy, message = _sqlite_quick_check(path) if exists else (False, "Missing")
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
        )
        sqlite_payload[label] = {
            "path": str(path),
            "exists": exists,
            "bytes": size,
            "display_size": _format_bytes(size),
            "quick_check": message,
        }

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

    version = _read_arr_version(paths["config_dir"], key)
    if not version:
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
        version_ok = bool(version and _version_tuple(version) >= minimum)
        add_check(
            "service_version",
            "pass" if version_ok else "warn",
            (
                f"Detected {key.capitalize()} {version}."
                if version_ok
                else "Could not confirm the minimum "
                f"{'.'.join(map(str, minimum))} version."
            ),
            detected=version,
            minimum=".".join(map(str, minimum)),
        )

    postgres_payload: dict[str, Any] = {
        "enabled": bool(postgres_config.get("enabled")),
        "main_database": database_names["main"],
        "log_database": database_names.get("log"),
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
        "sqlite": sqlite_payload,
        "postgres": postgres_payload,
        "checks": checks,
        "failure_count": len(failures),
        "warning_count": len(warnings),
        "confirmation_text": f"MIGRATE {process_name}",
        "migration_notice": (
            (
                "Servarr treats existing SQLite-to-PostgreSQL migration as unsupported. "
                if key in ARR_SERVICE_KEYS
                else "This service supports PostgreSQL but database-engine migration still requires downtime and validation. "
            )
            + "DUMB will preserve SQLite for rollback and validate every imported table."
        ),
        "supports_log_migration": "log" in database_names,
        "backup_root": str(Path(root)),
    }


def _backup_sqlite(
    source: Path,
    destination: Path,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60)
    destination_connection = sqlite3.connect(str(destination))
    try:

        def report(status, remaining, total):
            del status
            if progress:
                progress(max(total - remaining, 0), total)

        source_connection.backup(
            destination_connection, pages=4096, progress=report, sleep=0.05
        )
    finally:
        destination_connection.close()
        source_connection.close()
    healthy, message = _sqlite_quick_check(destination)
    if not healthy:
        raise ArrPostgresMigrationError(
            f"SQLite backup integrity check failed for {source.name}: {message}"
        )


def _postgres_table_columns(connection, table: str) -> dict[str, dict[str, str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name, data_type, is_generated, is_identity "
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
            }
            for row in cursor.fetchall()
        }


def _convert_value(value, data_type: str):
    if value is None:
        return None
    if data_type == "boolean":
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
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
) -> list[str]:
    source = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True, timeout=60)
    target = _pg_connect(postgres_config, database)
    try:
        source_tables = _sqlite_tables(source, excluded_tables)
        missing = [
            table
            for table in source_tables
            if not _postgres_table_columns(target, table)
        ]
        if missing:
            raise ArrPostgresMigrationError(
                "PostgreSQL schema is missing SQLite tables: " + ", ".join(missing[:10])
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


def _reset_postgres_sequences(connection) -> int:
    reset_count = 0
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name, column_name, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_default LIKE 'nextval(%'"
        )
        entries = cursor.fetchall()
        for table, column, default in entries:
            match = re.search(r"nextval\('([^']+)'::regclass\)", default or "")
            if not match:
                continue
            sequence = match.group(1)
            cursor.execute(
                sql.SQL("SELECT MAX({}) FROM {}").format(
                    sql.Identifier(column), sql.Identifier(table)
                )
            )
            maximum = cursor.fetchone()[0]
            if maximum is None:
                cursor.execute("SELECT setval(%s, 1, false)", [sequence])
            else:
                cursor.execute("SELECT setval(%s, %s, true)", [sequence, maximum])
            reset_count += 1
    connection.commit()
    return reset_count


def import_sqlite_to_postgres(
    sqlite_path: str | Path,
    postgres_config: dict[str, Any],
    database: str,
    progress: Callable[[dict[str, Any]], None] | None = None,
    batch_size: int = 500,
    excluded_tables: set[str] | None = None,
) -> dict[str, Any]:
    """Import data into an application-created PostgreSQL schema and validate counts."""
    sqlite_path = Path(sqlite_path)
    tables = _prepare_target_for_import(
        sqlite_path, postgres_config, database, excluded_tables
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
            while True:
                rows = source_cursor.fetchmany(batch_size)
                if not rows:
                    break
                converted = [
                    tuple(
                        _convert_value(value, target_columns[column]["data_type"])
                        for column, value in zip(columns, row)
                    )
                    for row in rows
                ]
                with target.cursor() as cursor:
                    execute_values(
                        cursor, insert_query, converted, page_size=batch_size
                    )
                target.commit()
                imported_count += len(rows)
                processed_rows += len(rows)
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
        return {
            "database": database,
            "tables": len(tables),
            "rows": sum(imported.values()),
            "sequences_reset": sequence_count,
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
) -> None:
    """Run application-specific schema initialization before staging startup."""
    if key == "bazarr":
        install_path = str(instance.get("config_dir") or "/opt/bazarr")
        success, error = _ensure_bazarr_postgres_driver(process_handler, install_path)
        if not success:
            raise ArrPostgresMigrationError(
                f"Bazarr PostgreSQL driver setup failed: {error}"
            )
        return
    if key != "pulsarr":
        return

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


def _stop_process(process_handler, process_name: str) -> None:
    process_handler.stop_process(process_name)


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


def _wait_for_running_service(api_state, process_name: str, timeout: int = 60) -> None:
    if not api_state:
        time.sleep(3)
        return
    deadline = time.time() + timeout
    stable_since = None
    while time.time() < deadline:
        if api_state.get_status(process_name) == "running":
            stable_since = stable_since or time.time()
            if time.time() - stable_since >= 5:
                return
        else:
            stable_since = None
        time.sleep(1)
    raise ArrPostgresMigrationError(
        "Service did not remain running after PostgreSQL cutover."
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
        self._active_processes: set[str] = set()
        self._last_progress_write: dict[str, float] = {}

    def _job_path(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", str(job_id or "")):
            raise ArrPostgresMigrationError("Invalid migration job ID.")
        return self.jobs_dir / f"{job_id}.json"

    def _save(self, payload: dict[str, Any]) -> None:
        payload["updated_at"] = int(time.time())
        _atomic_json(self._job_path(payload["job_id"]), payload)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        try:
            path = self._job_path(job_id)
        except ArrPostgresMigrationError:
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if payload.get("status") in ACTIVE_JOB_STATUSES:
            worker_pid = payload.get("worker_pid")
            if worker_pid and int(worker_pid) != os.getpid():
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
        with self._lock:
            latest = self.latest_job(process_name)
            if process_name in self._active_processes or (
                latest and latest.get("status") in ACTIVE_JOB_STATUSES
            ):
                raise ArrPostgresMigrationError(
                    "A migration job is already active for this service."
                )
            self._active_processes.add(process_name)

        job_id = uuid.uuid4().hex
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
            "progress": None,
            "events": [],
            "result": None,
            "error": None,
            "rollback": None,
            "rollback_available": False,
            "preflight": preflight,
        }
        self._save(payload)

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
    ) -> dict[str, Any]:
        try:
            _stop_process(process_handler, payload["process_name"])
        except Exception:
            pass
        config_path = paths.get("config_xml")
        if config_backup and config_backup.is_file() and config_path:
            shutil.copy2(config_backup, config_path)
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
        restarted = False
        if was_running:
            _start_process(process_handler, payload["process_name"], instance)
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
        runtime_restored = False
        payload["status"] = "running"
        payload["started_at"] = int(time.time())
        payload["backup_dir"] = str(backup_dir)
        payload["app_config_backup"] = str(config_backup) if config_backup else None
        payload["was_running"] = was_running
        self._save(payload)
        try:
            self._progress(payload, "backup", "Creating rollback backup.", 5)
            backup_dir.mkdir(parents=True, exist_ok=True)
            if config_backup and config_path:
                shutil.copy2(config_path, config_backup)
            config_file = Path(str(getattr(config_manager, "file_path", "")))
            if config_file.is_file():
                shutil.copy2(config_file, backup_dir / "dumb_config.json")

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

                _backup_sqlite(paths[label], sqlite_backups[label], backup_progress)

            self._progress(
                payload,
                "postgres",
                "Creating isolated PostgreSQL staging databases.",
                28,
            )
            _initialize_database_names(postgres_config, stage_databases)

            self._progress(
                payload,
                "schema",
                "Initializing the service's current PostgreSQL schema.",
                32,
            )
            if was_running and payload["mode"] == "rehearsal":
                _stop_process(process_handler, process_name)
            _apply_database_config(
                key,
                instance_name,
                instance,
                paths,
                postgres_config,
                stage_database_map,
                enabled=True,
            )
            _prepare_service_schema(key, instance, process_handler)
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

            instance.clear()
            instance.update(copy.deepcopy(original_instance))
            if config_backup and config_path:
                shutil.copy2(config_backup, config_path)

            import_databases = database_map
            if payload["mode"] == "rehearsal":
                import_databases = stage_database_map
                if was_running:
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
                )

            self._progress(payload, "validation", "Validating imported data.", 88)
            key_counts = {}
            source_counts = _sqlite_row_counts(sqlite_backups["main"], excluded_tables)
            for table in spec["key_tables"]:
                if table in source_counts:
                    key_counts[table] = source_counts[table]

            if payload["mode"] == "rehearsal":
                payload["status"] = "completed"
                payload["result"] = {
                    "mode": "rehearsal",
                    "validated": True,
                    "imports": results,
                    "key_row_counts": key_counts,
                    "cutover_performed": False,
                    "sqlite_runtime_restored": runtime_restored,
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
                if was_running:
                    _start_process(process_handler, process_name, instance)
                    _wait_for_running_service(api_state, process_name)
                payload["status"] = "completed"
                payload["rollback_available"] = True
                payload["result"] = {
                    "mode": "cutover",
                    "validated": True,
                    "imports": results,
                    "key_row_counts": key_counts,
                    "cutover_performed": True,
                    "postgres_databases": database_names,
                    "sqlite_backups": {
                        label: str(path)
                        for label, path in sqlite_backups.items()
                        if path.is_file()
                    },
                }
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
            payload["status"] = (
                "failed_rolled_back" if rollback and runtime_restored else "failed"
            )
            payload["error"] = {"message": str(exc)}
            payload["rollback"] = rollback
            payload["rollback_available"] = (
                backup_dir / "dumb_config.json"
            ).is_file() and sqlite_backups["main"].is_file()
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
        payload = self.get_job(job_id)
        if not payload:
            raise ArrPostgresMigrationError("Migration job was not found.")
        process_name = payload["process_name"]
        if confirmation != f"ROLLBACK {process_name}":
            raise ArrPostgresMigrationError(
                f"Type 'ROLLBACK {process_name}' to authorize rollback."
            )
        if payload.get("status") in ACTIVE_JOB_STATUSES:
            raise ArrPostgresMigrationError("Cannot roll back while the job is active.")
        key, instance_name, instance = _resolve_instance(config_manager, process_name)
        paths = _source_paths(key, instance)
        backup_dir = Path(str(payload.get("backup_dir") or ""))
        config_backup_value = payload.get("app_config_backup")
        config_backup = Path(str(config_backup_value)) if config_backup_value else None
        legacy_config_backup = backup_dir / "config.xml"
        if not config_backup and legacy_config_backup.is_file():
            config_backup = legacy_config_backup
        if not (backup_dir / "dumb_config.json").is_file():
            raise ArrPostgresMigrationError(
                "The job's DUMB configuration backup is missing."
            )
        if config_backup and not config_backup.is_file():
            raise ArrPostgresMigrationError(
                "The job's application configuration backup is missing."
            )
        was_running = (
            api_state.get_status(process_name) == "running" if api_state else True
        )
        payload["status"] = "rolling_back"
        self._save(payload)
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
        )
        result["warning"] = (
            "Changes made after PostgreSQL cutover are not copied back into SQLite."
        )
        payload["status"] = "rolled_back"
        payload["rollback"] = result
        payload["rollback_available"] = False
        payload["finished_at"] = int(time.time())
        self._progress(payload, "rolled_back", "SQLite configuration restored.", 100)
        self._save(payload)
        return payload


# Generic names are canonical; Arr-prefixed exports remain for callers from the
# original Sonarr/Radarr-only implementation.
PostgresMigrationError = ArrPostgresMigrationError
PostgresMigrationManager = ArrPostgresMigrationManager
build_postgres_preflight = build_arr_postgres_preflight
POSTGRES_MIGRATION_MANAGER = PostgresMigrationManager()
ARR_POSTGRES_MIGRATION_MANAGER = POSTGRES_MIGRATION_MANAGER
