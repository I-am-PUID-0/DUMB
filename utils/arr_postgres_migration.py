"""Guarded SQLite-to-PostgreSQL migration workflow for supported Arr services.

The normal ``postgres_enabled`` option only changes the configured database
backend.  This module provides the deliberately separate, observable migration
workflow used by the API and frontend.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values

from utils.arr_postgres import apply_arr_postgres_config, arr_postgres_database_names
from utils.postgres import initialize_postgres_databases

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
}
TERMINAL_JOB_STATUSES = {
    "completed",
    "failed",
    "failed_rolled_back",
    "rolled_back",
    "interrupted",
}
ACTIVE_JOB_STATUSES = {"queued", "running", "rolling_back"}
DEFAULT_ROOT = "/config/arr-postgres-migration"


class ArrPostgresMigrationError(RuntimeError):
    """Expected migration failure safe to report without a traceback."""


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug or "arr"


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


def _sqlite_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _sqlite_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    escaped = table.replace('"', '""')
    return [
        str(row[1]) for row in connection.execute(f'PRAGMA table_info("{escaped}")')
    ]


def _sqlite_row_counts(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        counts = {}
        for table in _sqlite_tables(connection):
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
    if key not in SUPPORTED_SERVICES or not instance_name:
        raise ArrPostgresMigrationError(
            "SQLite-to-PostgreSQL migration currently supports Sonarr and Radarr "
            "instances only."
        )
    instance = config_manager.get_instance(instance_name, key)
    if not isinstance(instance, dict):
        raise ArrPostgresMigrationError("Service configuration was not found.")
    return key, instance_name, instance


def _source_paths(key: str, instance: dict[str, Any]) -> dict[str, Path]:
    config_dir = Path(str(instance.get("config_dir") or ""))
    return {
        "config_dir": config_dir,
        "config_xml": Path(
            str(instance.get("config_file") or config_dir / "config.xml")
        ),
        "main": config_dir / SUPPORTED_SERVICES[key]["main_file"],
        "log": config_dir / "logs.db",
    }


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
    main_db, log_db = arr_postgres_database_names(key, instance_name, instance)
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
    for label in ("main", "log"):
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

    config_exists = paths["config_xml"].is_file()
    add_check(
        "config_xml",
        "pass" if config_exists else "fail",
        (
            "config.xml is available for guarded switching."
            if config_exists
            else "config.xml is missing."
        ),
        path=str(paths["config_xml"]),
    )

    version = _read_arr_version(paths["config_dir"], key)
    minimum = SUPPORTED_SERVICES[key]["minimum_version"]
    version_ok = bool(version and _version_tuple(version) >= minimum)
    add_check(
        "arr_version",
        "pass" if version_ok else "warn",
        (
            f"Detected {key.capitalize()} {version}."
            if version_ok
            else f"Could not confirm the minimum {'.'.join(map(str, minimum))} version."
        ),
        detected=version,
        minimum=".".join(map(str, minimum)),
    )

    postgres_payload: dict[str, Any] = {
        "enabled": bool(postgres_config.get("enabled")),
        "main_database": main_db,
        "log_database": log_db,
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
            "main": _postgres_database_summary(postgres_config, main_db),
            "log": _postgres_database_summary(postgres_config, log_db),
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
            "Servarr treats existing SQLite-to-PostgreSQL migration as unsupported. "
            "DUMB will preserve SQLite for rollback and validate every imported table."
        ),
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
        return bool(value)
    if data_type in {"smallint", "integer", "bigint"} and isinstance(value, bool):
        return int(value)
    if data_type == "bytea" and isinstance(value, memoryview):
        return value.tobytes()
    return value


def _prepare_target_for_import(
    sqlite_path: Path,
    postgres_config: dict[str, Any],
    database: str,
) -> list[str]:
    source = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True, timeout=60)
    target = _pg_connect(postgres_config, database)
    try:
        source_tables = _sqlite_tables(source)
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
) -> dict[str, Any]:
    """Import data into an Arr-created PostgreSQL schema and validate counts."""
    sqlite_path = Path(sqlite_path)
    tables = _prepare_target_for_import(sqlite_path, postgres_config, database)
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
                column for column in source_columns if column not in columns
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
    existing = {
        item.get("name")
        for item in databases
        if isinstance(item, dict) and item.get("name")
    }
    for name in database_names:
        if name not in existing:
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


def _start_process(process_handler, process_name: str) -> None:
    result = process_handler.start_process(process_name)
    if isinstance(result, tuple):
        success, error = result
    else:
        success, error = result, None
    if not success:
        raise ArrPostgresMigrationError(
            f"{process_name} failed to start. Check the service logs."
        ) from (RuntimeError(str(error)) if error else None)


def _stop_process(process_handler, process_name: str) -> None:
    process_handler.stop_process(process_name)


def _wait_for_schema(
    postgres_config: dict[str, Any], databases: list[str], timeout: int = 180
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if _schema_ready(postgres_config, databases):
                return
        except Exception:
            pass
        time.sleep(2)
    raise ArrPostgresMigrationError(
        "Arr did not initialize its PostgreSQL schema before the timeout."
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
            "include_logs": bool(include_logs),
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
            name=f"arr-postgres-migration-{_safe_slug(process_name)}",
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
        instance,
        config_xml: Path,
        config_backup: Path,
        was_running: bool,
    ) -> dict[str, Any]:
        try:
            _stop_process(process_handler, payload["process_name"])
        except Exception:
            pass
        if config_backup.is_file():
            shutil.copy2(config_backup, config_xml)
        instance["postgres_enabled"] = False
        config_manager.save_config(payload["process_name"])
        restarted = False
        if was_running:
            _start_process(process_handler, payload["process_name"])
            restarted = True
        return {
            "restored_config": str(config_xml),
            "sqlite_preserved": True,
            "service_restarted": restarted,
        }

    def _run_job(
        self, payload, config_manager, process_handler, api_state, logger
    ) -> None:
        process_name = payload["process_name"]
        key, instance_name, instance = _resolve_instance(config_manager, process_name)
        paths = _source_paths(key, instance)
        postgres_config = config_manager.get("postgres", {}) or {}
        main_db, log_db = arr_postgres_database_names(key, instance_name, instance)
        database_names = [main_db, log_db]
        was_running = (
            api_state.get_status(process_name) == "running" if api_state else True
        )
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup_dir = (
            self.backups_dir
            / _safe_slug(process_name)
            / f"{timestamp}-{payload['job_id'][:8]}"
        )
        config_backup = backup_dir / "config.xml"
        sqlite_backups = {
            "main": backup_dir / paths["main"].name,
            "log": backup_dir / paths["log"].name,
        }
        stage_suffix = payload["job_id"][:8]
        stage_databases = [
            f"dumb_stage_{key}_{stage_suffix}_{label}" for label in ("main", "log")
        ]
        runtime_restored = False
        payload["status"] = "running"
        payload["started_at"] = int(time.time())
        payload["backup_dir"] = str(backup_dir)
        payload["was_running"] = was_running
        self._save(payload)
        try:
            self._progress(payload, "backup", "Creating rollback backup.", 5)
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(paths["config_xml"], config_backup)
            config_file = Path(str(getattr(config_manager, "file_path", "")))
            if config_file.is_file():
                shutil.copy2(config_file, backup_dir / "dumb_config.json")

            backup_labels = ["main"] + (["log"] if payload["include_logs"] else [])
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
            _set_database_entries(config_manager, database_names)
            _initialize_database_names(postgres_config, stage_databases)

            self._progress(
                payload, "schema", "Initializing the current Arr PostgreSQL schema.", 32
            )
            if was_running and payload["mode"] == "rehearsal":
                _stop_process(process_handler, process_name)
            postgres_instance = dict(instance)
            postgres_instance["postgres_enabled"] = True
            postgres_instance["postgres_main_db"] = stage_databases[0]
            postgres_instance["postgres_log_db"] = stage_databases[1]
            apply_arr_postgres_config(
                key,
                instance_name,
                postgres_instance,
                str(paths["config_xml"]),
                postgres_config,
            )
            _start_process(process_handler, process_name)
            _wait_for_schema(postgres_config, stage_databases)
            _stop_process(process_handler, process_name)

            import_databases = {"main": main_db, "log": log_db}
            if payload["mode"] == "rehearsal":
                import_databases = {
                    "main": stage_databases[0],
                    "log": stage_databases[1],
                }
                shutil.copy2(config_backup, paths["config_xml"])
                if was_running:
                    _start_process(process_handler, process_name)
                    _wait_for_running_service(api_state, process_name)
                runtime_restored = True
            else:
                for stage_db, target_db in zip(stage_databases, database_names):
                    _clone_database(postgres_config, stage_db, target_db)

            results = {}
            import_labels = ["main"] + (["log"] if payload["include_logs"] else [])
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
                )

            self._progress(payload, "validation", "Validating imported data.", 88)
            key_counts = {}
            source_counts = _sqlite_row_counts(sqlite_backups["main"])
            for table in SUPPORTED_SERVICES[key]["key_tables"]:
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
                instance["postgres_enabled"] = True
                instance["postgres_main_db"] = main_db
                instance["postgres_log_db"] = log_db
                config_manager.save_config(process_name)
                apply_arr_postgres_config(
                    key,
                    instance_name,
                    instance,
                    str(paths["config_xml"]),
                    postgres_config,
                )
                if was_running:
                    _start_process(process_handler, process_name)
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
            logger.error(
                "Arr PostgreSQL migration failed for %s: %s", process_name, exc
            )
            rollback = None
            if config_backup.is_file():
                payload["status"] = "rolling_back"
                self._save(payload)
                try:
                    rollback = self._restore_sqlite_runtime(
                        payload,
                        config_manager,
                        process_handler,
                        instance,
                        paths["config_xml"],
                        config_backup,
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
                        "message": "Automatic rollback failed. Restore config.xml from the job backup before restarting.",
                    }
            payload["status"] = (
                "failed_rolled_back" if rollback and runtime_restored else "failed"
            )
            payload["error"] = {"message": str(exc)}
            payload["rollback"] = rollback
            payload["rollback_available"] = config_backup.is_file()
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
        key, _, instance = _resolve_instance(config_manager, process_name)
        paths = _source_paths(key, instance)
        config_backup = Path(str(payload.get("backup_dir") or "")) / "config.xml"
        if not config_backup.is_file():
            raise ArrPostgresMigrationError("The job's config.xml backup is missing.")
        was_running = (
            api_state.get_status(process_name) == "running" if api_state else True
        )
        payload["status"] = "rolling_back"
        self._save(payload)
        result = self._restore_sqlite_runtime(
            payload,
            config_manager,
            process_handler,
            instance,
            paths["config_xml"],
            config_backup,
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


ARR_POSTGRES_MIGRATION_MANAGER = ArrPostgresMigrationManager()
