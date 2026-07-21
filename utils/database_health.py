"""Service-aware, read-only database/store health collection for DUMB metrics.

The collector intentionally observes SQL databases and application-owned
persistent stores without performing maintenance. Standard mode reads files,
storage placement/capacity/inodes, and new log messages. Enhanced mode adds
bounded, read-only SQLite/PostgreSQL metadata probes where supported. It never
runs integrity checks, VACUUM, checkpoints, ANALYZE, or application SQL queries.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import yaml

ARR_DATABASE_FILES = {
    "sonarr": "sonarr.db",
    "radarr": "radarr.db",
    "lidarr": "lidarr.db",
    "prowlarr": "prowlarr.db",
    "whisparr": "whisparr.db",
}
SQLITE_SERVICE_KEYS = {
    "altmount",
    "bazarr",
    "cli_battery",
    "cli_debrid",
    "emby",
    "jellyfin",
    "maintainerr",
    "nzbdav",
    "plex",
    "profilarr",
    "pulsarr",
    "seerr",
    "tautulli",
}
POSTGRES_SERVICE_DATABASES = {
    "pgadmin": "pgadmin",
    "mediastorm": "mediastorm",
    "riven_backend": "riven",
    "traefik_proxy_admin": "traefik_proxy_admin",
    "zilean": "zilean",
}
CUSTOM_STORE_PROVIDERS = {
    "decypharr": "append-log",
    "phalanx_db": "hyperbee",
    "zurg": "zurg-state",
}
SUPPORTED_SERVICE_KEYS = (
    set(ARR_DATABASE_FILES)
    | SQLITE_SERVICE_KEYS
    | set(POSTGRES_SERVICE_DATABASES)
    | set(CUSTOM_STORE_PROVIDERS)
    | {"postgres"}
)
NETWORK_FILESYSTEMS = {
    "9p",
    "ceph",
    "cifs",
    "fuse.ceph",
    "fuse.glusterfs",
    "fuse.sshfs",
    "glusterfs",
    "nfs",
    "nfs4",
    "smb3",
}
LOG_PATTERNS = {
    "locked": re.compile(
        r"database\s+is\s+locked|database\s+table\s+is\s+locked", re.I
    ),
    "busy": re.compile(r"SQLITE_BUSY|busy\s+timeout", re.I),
    "timeout": re.compile(
        r"database.*(?:timed?\s*out|timeout)|(?:timed?\s*out|timeout).*database", re.I
    ),
    "io_error": re.compile(
        r"SQLITE_IOERR|disk\s+i/o\s+error|database\s+disk\s+image\s+is\s+malformed",
        re.I,
    ),
    "deadlock": re.compile(r"deadlock\s+detected", re.I),
}


class DatabaseHealthCollector:
    """Collect cached database pressure indicators for configured services."""

    def __init__(self, logger=None, clock=time.time):
        self.logger = logger
        self.clock = clock
        self._cache: dict[str, dict[str, Any]] = {}
        self._log_states: dict[str, dict[str, Any]] = {}
        self._postgres_previous: dict[tuple[str, str], dict[str, int]] = {}
        self._lock = threading.Lock()

    def invalidate(self, service_id: str | None = None) -> None:
        """Drop cached probes so the next snapshot performs a fresh collection."""
        with self._lock:
            if service_id:
                self._cache.pop(service_id, None)
            else:
                self._cache.clear()

    def service_id_for_process(
        self, config: dict[str, Any], process_name: str
    ) -> str | None:
        for candidate in self._discover_services(config):
            if candidate["process_name"] == process_name:
                return candidate["id"]
        return None

    def snapshot(
        self,
        config: dict[str, Any],
        details: bool = True,
        refresh_if_stale: bool = True,
        process_name: str | None = None,
    ) -> dict[str, Any]:
        metrics_cfg = (config.get("dumb") or {}).get("metrics") or {}
        health_cfg = metrics_cfg.get("database_health") or {}
        globally_enabled = health_cfg.get("enabled") is True
        interval = _bounded_int(health_cfg.get("interval_sec"), 60, 15, 3600)
        log_tail_bytes = _bounded_int(
            health_cfg.get("log_tail_bytes"), 262_144, 16_384, 4_194_304
        )
        configured_services = health_cfg.get("services") or {}
        if not isinstance(configured_services, dict):
            configured_services = {}

        candidates = self._discover_services(config)
        if process_name:
            candidates = [
                candidate
                for candidate in candidates
                if candidate["process_name"] == process_name
            ]
        now = self.clock()
        services = []
        with self._lock:
            for candidate in candidates:
                service_cfg = configured_services.get(candidate["id"]) or {}
                if not isinstance(service_cfg, dict):
                    service_cfg = {"enabled": bool(service_cfg)}
                monitoring_enabled = (
                    globally_enabled and service_cfg.get("enabled") is True
                )
                mode = str(service_cfg.get("mode") or "standard").lower()
                if mode not in {"standard", "enhanced"}:
                    mode = "standard"
                ignore_network_storage = (
                    service_cfg.get("ignore_network_storage") is True
                )

                if not monitoring_enabled:
                    disabled = self._disabled_result(candidate, globally_enabled, mode)
                    services.append(
                        disabled if details else self._compact_result(disabled)
                    )
                    continue

                cached = self._cache.get(candidate["id"])
                cache_matches_config = bool(
                    cached
                    and cached.get("mode") == mode
                    and bool(cached.get("ignore_network_storage"))
                    == ignore_network_storage
                )
                if (
                    cache_matches_config
                    and now - float(cached.get("collected_at") or 0) < interval
                ):
                    result = dict(cached)
                elif not refresh_if_stale:
                    result = (
                        dict(cached)
                        if cache_matches_config
                        else self._waiting_result(candidate, mode)
                    )
                else:
                    result = self._collect_service(
                        candidate,
                        config,
                        mode=mode,
                        ignore_network_storage=ignore_network_storage,
                        log_tail_bytes=log_tail_bytes,
                        now=now,
                    )
                    self._cache[candidate["id"]] = result
                result["monitoring_enabled"] = True
                result["mode"] = mode
                result["ignore_network_storage"] = ignore_network_storage
                services.append(result if details else self._compact_result(result))

        monitored = [item for item in services if item.get("monitoring_enabled")]
        if not details:
            services = monitored
        pressure_counts: dict[str, int] = {}
        for item in monitored:
            pressure = str(item.get("pressure") or "unknown")
            pressure_counts[pressure] = pressure_counts.get(pressure, 0) + 1
        return {
            "enabled": globally_enabled,
            "interval_sec": interval,
            "supported_count": len(candidates),
            "monitored_count": len(monitored),
            "pressure_counts": pressure_counts,
            "services": services,
        }

    def _discover_services(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        candidates = []
        for key in sorted(SUPPORTED_SERVICE_KEYS):
            root = config.get(key) or {}
            if not isinstance(root, dict):
                continue
            instances = root.get("instances")
            if isinstance(instances, dict):
                for instance_name, instance in instances.items():
                    if isinstance(instance, dict) and instance.get("enabled") is True:
                        candidates.append(
                            self._candidate(key, instance, str(instance_name))
                        )
            elif root.get("enabled") is True:
                candidates.append(self._candidate(key, root, None))
        return sorted(candidates, key=lambda item: item["process_name"].lower())

    @staticmethod
    def _candidate(key: str, service: dict[str, Any], instance_name: str | None):
        identifier = f"{key}:{instance_name}" if instance_name else key
        return {
            "id": identifier,
            "config_key": key,
            "instance_name": instance_name,
            "process_name": service.get("process_name") or instance_name or key.title(),
            "provider": _provider_for_service(key, service),
            "service_config": service,
        }

    @staticmethod
    def _disabled_result(candidate, globally_enabled, mode):
        reason = (
            "Database Health Monitoring is disabled for this service."
            if globally_enabled
            else "Database Health Monitoring is globally disabled."
        )
        return {
            "id": candidate["id"],
            "config_key": candidate["config_key"],
            "instance_name": candidate["instance_name"],
            "process_name": candidate["process_name"],
            "provider": candidate["provider"],
            "monitoring_enabled": False,
            "mode": mode,
            "pressure": "disabled",
            "score": 0,
            "recommendation": reason,
            "databases": [],
        }

    @staticmethod
    def _waiting_result(candidate, mode):
        return {
            "id": candidate["id"],
            "config_key": candidate["config_key"],
            "instance_name": candidate["instance_name"],
            "process_name": candidate["process_name"],
            "provider": candidate["provider"],
            "monitoring_enabled": True,
            "mode": mode,
            "pressure": "collecting",
            "score": 0,
            "recommendation": "Waiting for the slower database-health collection interval.",
            "databases": [],
        }

    def _collect_service(
        self,
        candidate,
        config,
        mode,
        ignore_network_storage,
        log_tail_bytes,
        now,
    ):
        service = candidate["service_config"]
        log_signals = self._collect_log_signals(service.get("log_file"), log_tail_bytes)
        result = {
            "id": candidate["id"],
            "config_key": candidate["config_key"],
            "instance_name": candidate["instance_name"],
            "process_name": candidate["process_name"],
            "provider": candidate["provider"],
            "monitoring_enabled": True,
            "mode": mode,
            "ignore_network_storage": ignore_network_storage,
            "collected_at": now,
            "log_signals": log_signals,
            "databases": [],
        }
        if candidate["provider"] == "postgresql":
            result["databases"] = self._collect_postgres_databases(
                candidate, config, enhanced=mode == "enhanced"
            )
        elif candidate["provider"] == "sqlite":
            paths = self._sqlite_paths(candidate)
            result["databases"] = [
                self._collect_sqlite_file(
                    role,
                    path,
                    enhanced=mode == "enhanced" and candidate["config_key"] != "plex",
                )
                for role, path in paths
            ]
            if candidate["config_key"] == "plex" and mode == "enhanced":
                result["probe_notice"] = (
                    "Plex stays in passive mode because its customized SQLite build "
                    "should not be probed continuously while the media server is running."
                )
        else:
            result["databases"] = [
                self._collect_persistent_store(role, path)
                for role, path in self._persistent_store_paths(candidate)
            ]
            if mode == "enhanced":
                result["probe_notice"] = (
                    f"{candidate['provider']} is not a SQL provider. Enhanced mode "
                    "keeps passive file, directory, filesystem, and log telemetry; "
                    "DUMB does not open or query this application-owned store."
                )

        score, reasons = self._score(result)
        result["score"] = score
        result["pressure"] = self._pressure_label(result, score)
        result["reasons"] = reasons
        result["recommendation"] = self._recommendation(result, reasons)
        return result

    def _sqlite_paths(self, candidate) -> list[tuple[str, str]]:
        key = candidate["config_key"]
        service = candidate["service_config"]
        config_dir = str(service.get("config_dir") or "")
        if key in ARR_DATABASE_FILES:
            return [
                ("main", os.path.join(config_dir, ARR_DATABASE_FILES[key])),
                ("logs", os.path.join(config_dir, "logs.db")),
            ]
        if key == "nzbdav":
            env = service.get("env") or {}
            base = str(env.get("CONFIG_PATH") or config_dir or "/nzbdav")
            return [
                ("main", os.path.join(base, "db.sqlite")),
                ("metrics", os.path.join(base, "metrics.sqlite")),
            ]
        if key == "bazarr":
            candidates = [
                "/bazarr/data/db/bazarr.db",
                os.path.join(config_dir, "data", "db", "bazarr.db"),
                os.path.join(config_dir, "db", "bazarr.db"),
            ]
            path = next(
                (item for item in candidates if os.path.exists(item)), candidates[0]
            )
            return [("main", path)]
        if key == "plex":
            dbrepair = service.get("dbrepair") or {}
            main = str(
                dbrepair.get("db_path")
                or os.path.join(
                    config_dir,
                    "Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db",
                )
            )
            blobs = os.path.join(
                os.path.dirname(main), "com.plexapp.plugins.library.blobs.db"
            )
            return [("library", main), ("blobs", blobs)]
        if key == "tautulli":
            return [("main", os.path.join(config_dir, "data", "tautulli.db"))]
        if key == "profilarr":
            env = service.get("env") or {}
            data_dir = str(
                env.get("PROFILARR_CONFIG_DIR") or os.path.join(config_dir, "config")
            )
            return [("main", os.path.join(data_dir, "profilarr.db"))]
        if key == "pulsarr":
            env = service.get("env") or {}
            return [
                (
                    "main",
                    str(
                        env.get("dbPath")
                        or os.path.join(config_dir, "data", "db", "pulsarr.db")
                    ),
                )
            ]
        if key == "maintainerr":
            env = service.get("env") or {}
            data_dir = str(env.get("DATA_DIR") or os.path.join(config_dir, "data"))
            return [("main", os.path.join(data_dir, "maintainerr.sqlite"))]
        if key == "seerr":
            env = service.get("env") or {}
            data_dir = str(
                env.get("CONFIG_DIRECTORY") or os.path.join(config_dir, "config")
            )
            return [("main", os.path.join(data_dir, "db", "db.sqlite3"))]
        if key == "altmount":
            database = _altmount_database_config(service)
            return [
                (
                    "main",
                    str(
                        database.get("path") or os.path.join(config_dir, "altmount.db")
                    ),
                )
            ]
        if key in {"cli_debrid", "cli_battery"}:
            env = service.get("env") or {}
            data_dir = str(
                env.get("USER_DB_CONTENT")
                or os.path.join(config_dir, "data", "db_content")
            )
            if key == "cli_battery":
                return [("battery", os.path.join(data_dir, "cli_battery.db"))]
            return [
                ("media", os.path.join(data_dir, "media_items.db")),
                ("watch history", os.path.join(data_dir, "watch_history.db")),
            ]
        if key == "jellyfin":
            return _discovered_sqlite_paths(
                os.path.join(config_dir, "data"),
                fallback=("library", os.path.join(config_dir, "data", "library.db")),
            )
        if key == "emby":
            return _discovered_sqlite_paths(
                os.path.join(config_dir, "data"),
                fallback=("library", os.path.join(config_dir, "data", "library.db")),
            )
        return []

    @staticmethod
    def _persistent_store_paths(candidate) -> list[tuple[str, str]]:
        key = candidate["config_key"]
        service = candidate["service_config"]
        config_dir = str(service.get("config_dir") or "")
        if key == "decypharr":
            database_dir = os.path.join(config_dir, "db")
            return [
                (name.replace("_", " "), os.path.join(database_dir, f"{name}.db"))
                for name in (
                    "entries",
                    "queue",
                    "items",
                    "repair_state",
                    "repair_runs",
                )
            ]
        if key == "phalanx_db":
            return [
                (
                    "autobase",
                    os.path.join(config_dir, "data", "autobase_storage_v4"),
                )
            ]
        if key == "zurg":
            return [("state directory", os.path.join(config_dir, "data"))]
        return []

    @staticmethod
    def _collect_persistent_store(role: str, path: str):
        exists = os.path.exists(path)
        result: dict[str, Any] = {
            "role": role,
            "path": path,
            "exists": exists,
            "enhanced_probe": False,
            "store_kind": "directory" if os.path.isdir(path) else "file",
        }
        if not exists:
            result["notice"] = "Persistent store is not present yet."
            return result
        try:
            if os.path.isdir(path):
                size, file_count, truncated = _bounded_directory_size(path)
                result["size_bytes"] = size
                result["file_count"] = file_count
                result["scan_truncated"] = truncated
            else:
                result["size_bytes"] = os.path.getsize(path)
            result["storage"] = _storage_for_path(path)
        except OSError as exc:
            result["error"] = (
                f"Unable to inspect persistent store: {exc.strerror or exc}"
            )
        return result

    def _collect_sqlite_file(self, role: str, path: str, enhanced: bool):
        result: dict[str, Any] = {
            "role": role,
            "path": path,
            "exists": os.path.isfile(path),
            "enhanced_probe": enhanced,
        }
        if not result["exists"]:
            result["error"] = "Database file is not present yet."
            return result
        try:
            result["size_bytes"] = os.path.getsize(path)
            result["wal_size_bytes"] = _file_size(path + "-wal")
            result["shm_size_bytes"] = _file_size(path + "-shm")
            storage = _storage_for_path(path)
            result["storage"] = storage
        except OSError as exc:
            result["error"] = f"Unable to inspect database file: {exc.strerror or exc}"
            return result

        if not enhanced:
            return result

        started = time.monotonic()
        try:
            uri = Path(path).resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=2.0)
            try:
                connection.execute("PRAGMA query_only = ON")
                result["journal_mode"] = _first_value(connection, "PRAGMA journal_mode")
                result["page_size"] = _int_value(connection, "PRAGMA page_size")
                result["page_count"] = _int_value(connection, "PRAGMA page_count")
                result["freelist_count"] = _int_value(
                    connection, "PRAGMA freelist_count"
                )
                result["schema_version"] = _int_value(
                    connection, "PRAGMA schema_version"
                )
            finally:
                connection.close()
            result["probe_ms"] = round((time.monotonic() - started) * 1000, 2)
        except (sqlite3.Error, OSError) as exc:
            result["probe_ms"] = round((time.monotonic() - started) * 1000, 2)
            result["probe_error"] = _safe_error(exc)
        return result

    def _collect_postgres_databases(self, candidate, config, enhanced):
        specs = self._postgres_specs(candidate, config)
        storage = (
            self._postgres_storage(config)
            if any(_uses_local_postgres(spec.get("connection")) for spec in specs)
            else None
        )
        if not enhanced:
            return [
                {
                    "role": spec["role"],
                    "name": spec["name"],
                    "exists": None,
                    "enhanced_probe": False,
                    **(
                        {"storage": storage}
                        if storage and _uses_local_postgres(spec.get("connection"))
                        else {}
                    ),
                    "notice": "Enable enhanced mode for bounded PostgreSQL statistics queries.",
                }
                for spec in specs
            ]
        databases = [
            self._collect_postgres_database(
                candidate["id"],
                spec["role"],
                spec["name"],
                config,
                connection_settings=spec.get("connection"),
            )
            for spec in specs
        ]
        if storage:
            for database, spec in zip(databases, specs):
                if _uses_local_postgres(spec.get("connection")):
                    database["storage"] = storage
        return databases

    @staticmethod
    def _postgres_specs(candidate, config):
        key = candidate["config_key"]
        service = candidate["service_config"]
        if key in ARR_DATABASE_FILES:
            from utils.arr_postgres import arr_postgres_database_names

            names = arr_postgres_database_names(
                key, candidate["instance_name"] or "Default", service
            )
            return [
                {"role": role, "name": name, "connection": None}
                for role, name in zip(("main", "logs"), names)
            ]
        if key == "postgres":
            databases = service.get("databases") or []
            specs = [
                {
                    "role": "cluster database",
                    "name": str(database.get("name") or "").strip(),
                    "connection": None,
                }
                for database in databases
                if isinstance(database, dict)
                and database.get("enabled") is True
                and str(database.get("name") or "").strip()
            ]
            return specs or [
                {"role": "cluster database", "name": "postgres", "connection": None}
            ]
        if key in POSTGRES_SERVICE_DATABASES:
            return [
                {
                    "role": "main",
                    "name": POSTGRES_SERVICE_DATABASES[key],
                    "connection": None,
                }
            ]

        connection = _service_postgres_connection(key, service, config)
        return [
            {
                "role": "main",
                "name": str(connection.get("database") or key),
                "connection": connection,
            }
        ]

    @staticmethod
    def _postgres_storage(config):
        postgres = config.get("postgres") or {}
        host = str(postgres.get("host") or "127.0.0.1").strip().lower()
        if host not in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}:
            return None
        config_dir = str(postgres.get("config_dir") or "/postgres_data")
        if not os.path.exists(config_dir):
            return None
        return _storage_for_path(config_dir)

    def _collect_postgres_database(
        self, service_id, role, database, config, connection_settings=None
    ):
        result: dict[str, Any] = {
            "role": role,
            "name": database,
            "exists": None,
            "enhanced_probe": True,
        }
        started = time.monotonic()
        try:
            import psycopg2

            pg = config.get("postgres") or {}
            settings = connection_settings or {}
            if settings.get("dsn"):
                connection = psycopg2.connect(
                    settings["dsn"],
                    dbname=database,
                    connect_timeout=2,
                    application_name="dumb_database_health",
                )
            else:
                connection = psycopg2.connect(
                    dbname=database,
                    user=settings.get("user") or pg.get("user", "DUMB"),
                    password=settings.get("password") or pg.get("password", "postgres"),
                    host=settings.get("host") or pg.get("host", "127.0.0.1"),
                    port=int(settings.get("port") or pg.get("port", 5432)),
                    connect_timeout=2,
                    application_name="dumb_database_health",
                )
            try:
                connection.autocommit = True
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = 2000")
                    cursor.execute(
                        "SELECT numbackends, xact_commit, xact_rollback, blks_read, "
                        "blks_hit, temp_files, temp_bytes, deadlocks, conflicts, "
                        "pg_database_size(datname) FROM pg_stat_database WHERE datname = %s",
                        [database],
                    )
                    row = cursor.fetchone()
                    if row is None:
                        result["exists"] = False
                    else:
                        result["exists"] = True
                        keys = (
                            "connections",
                            "transactions_committed",
                            "transactions_rolled_back",
                            "blocks_read",
                            "blocks_hit",
                            "temp_files",
                            "temp_bytes",
                            "deadlocks",
                            "conflicts",
                            "size_bytes",
                        )
                        stats = {
                            name: int(value or 0) for name, value in zip(keys, row)
                        }
                        previous_key = (service_id, database)
                        previous = self._postgres_previous.get(previous_key) or {}
                        stats["deadlocks_delta"] = _counter_delta(
                            stats["deadlocks"], previous.get("deadlocks")
                        )
                        stats["rollbacks_delta"] = _counter_delta(
                            stats["transactions_rolled_back"],
                            previous.get("transactions_rolled_back"),
                        )
                        stats["temp_bytes_delta"] = _counter_delta(
                            stats["temp_bytes"], previous.get("temp_bytes")
                        )
                        total_blocks = stats["blocks_hit"] + stats["blocks_read"]
                        stats["cache_hit_percent"] = (
                            round(stats["blocks_hit"] / total_blocks * 100, 2)
                            if total_blocks
                            else None
                        )
                        self._postgres_previous[previous_key] = stats
                        result.update(stats)
                    cursor.execute(
                        "SELECT COUNT(*) FILTER (WHERE wait_event_type = 'Lock'), "
                        "COALESCE(MAX(EXTRACT(EPOCH FROM (NOW() - xact_start))), 0) "
                        "FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                        [database],
                    )
                    active = cursor.fetchone() or (0, 0)
                    result["lock_waiters"] = int(active[0] or 0)
                    result["oldest_transaction_seconds"] = round(
                        float(active[1] or 0), 2
                    )
            finally:
                connection.close()
            result["probe_ms"] = round((time.monotonic() - started) * 1000, 2)
        except Exception as exc:
            result["probe_ms"] = round((time.monotonic() - started) * 1000, 2)
            result["probe_error"] = _safe_error(exc)
        return result

    def _collect_log_signals(self, path, tail_bytes):
        empty = {name: 0 for name in LOG_PATTERNS}
        if not path:
            return {**empty, "available": False}
        path = str(path)
        try:
            stat = os.stat(path)
        except OSError:
            return {**empty, "available": False, "path": path}

        state = self._log_states.get(path)
        same_file = (
            state
            and state.get("inode") == stat.st_ino
            and state.get("offset", 0) <= stat.st_size
        )
        start = (
            int(state.get("offset", 0))
            if same_file
            else max(0, stat.st_size - tail_bytes)
        )
        counts = dict(state.get("counts", empty)) if same_file else dict(empty)
        last_event_at = state.get("last_event_at") if same_file else None
        last_seen = dict(state.get("last_seen", {})) if same_file else {}
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                content = handle.read(tail_bytes).decode("utf-8", errors="replace")
                offset = handle.tell()
        except OSError as exc:
            return {
                **counts,
                "available": False,
                "path": path,
                "error": _safe_error(exc),
            }

        for name, pattern in LOG_PATTERNS.items():
            found = len(pattern.findall(content))
            if found:
                counts[name] = counts.get(name, 0) + found
                last_event_at = self.clock()
                last_seen[name] = last_event_at
        self._log_states[path] = {
            "inode": stat.st_ino,
            "offset": offset,
            "counts": counts,
            "last_event_at": last_event_at,
            "last_seen": last_seen,
        }
        return {
            **counts,
            "available": True,
            "path": path,
            "last_event_at": last_event_at,
            "last_seen": last_seen,
            "scanned_through": offset,
        }

    @staticmethod
    def _score(result):
        score = 0
        reasons = []
        logs = result.get("log_signals") or {}
        collected_at = float(result.get("collected_at") or 0)

        def active(signal):
            last_seen_at = (logs.get("last_seen") or {}).get(signal)
            return bool(
                logs.get(signal)
                and last_seen_at is not None
                and collected_at - float(last_seen_at) <= 3600
            )

        if active("io_error"):
            score += 60
            reasons.append(
                "Database I/O or corruption-like errors were observed in service logs."
            )
        if active("locked"):
            score += 45
            reasons.append("SQLite lock errors were observed in service logs.")
        if active("busy"):
            score += 35
            reasons.append("SQLite busy errors or busy timeouts were observed.")
        if active("timeout"):
            score += 25
            reasons.append("Database timeout messages were observed.")
        if active("deadlock"):
            score += 45
            reasons.append("PostgreSQL deadlock messages were observed.")

        storage_groups = {}
        for database in result.get("databases") or []:
            storage = database.get("storage") or {}
            if not storage:
                continue
            storage_key = (
                storage.get("mount_point"),
                storage.get("fs_type"),
                storage.get("source"),
            )
            group = storage_groups.setdefault(
                storage_key, {"storage": storage, "roles": []}
            )
            group["roles"].append(str(database.get("role") or "database"))

        for group in storage_groups.values():
            storage = group["storage"]
            roles = group["roles"]
            mount_point = storage.get("mount_point") or "an unknown mount point"
            role_label = ", ".join(dict.fromkeys(roles))
            if storage.get("network") and not result.get("ignore_network_storage"):
                score += 35
                reasons.append(
                    f"Database storage ({role_label}) is on "
                    f"{storage.get('fs_type') or 'a network filesystem'} at {mount_point}."
                )
            if storage.get("read_only") is True:
                score += 60
                reasons.append(f"Database storage at {mount_point} is read-only.")

            used_percent = storage.get("used_percent")
            if isinstance(used_percent, (int, float)):
                if used_percent >= 98:
                    score += 35
                    reasons.append(
                        f"Database storage at {mount_point} is at least 98% full."
                    )
                elif used_percent >= 95:
                    score += 25
                    reasons.append(
                        f"Database storage at {mount_point} is at least 95% full."
                    )
                elif used_percent >= 90:
                    score += 10
                    reasons.append(
                        f"Database storage at {mount_point} is at least 90% full."
                    )

            inode_used_percent = storage.get("inode_used_percent")
            if isinstance(inode_used_percent, (int, float)):
                if inode_used_percent >= 98:
                    score += 45
                    reasons.append(
                        f"Database storage at {mount_point} has at least 98% of its inodes in use."
                    )
                elif inode_used_percent >= 95:
                    score += 30
                    reasons.append(
                        f"Database storage at {mount_point} has at least 95% of its inodes in use."
                    )
                elif inode_used_percent >= 90:
                    score += 15
                    reasons.append(
                        f"Database storage at {mount_point} has at least 90% of its inodes in use."
                    )

        for database in result.get("databases") or []:
            wal_size = int(database.get("wal_size_bytes") or 0)
            db_size = int(database.get("size_bytes") or 0)
            if wal_size >= 256 * 1024 * 1024:
                score += 25
                reasons.append("A SQLite WAL file is at least 256 MiB.")
            elif wal_size >= 64 * 1024 * 1024:
                score += 10
                reasons.append("A SQLite WAL file is at least 64 MiB.")
            if db_size and wal_size > db_size:
                score += 10
                reasons.append("A SQLite WAL file is larger than its database file.")
            probe_ms = database.get("probe_ms")
            if isinstance(probe_ms, (int, float)):
                if probe_ms >= 1000:
                    score += 30
                    reasons.append(
                        "A bounded read-only database probe took at least one second."
                    )
                elif probe_ms >= 250:
                    score += 20
                    reasons.append(
                        "A bounded read-only database probe took at least 250 ms."
                    )
                elif probe_ms >= 100:
                    score += 10
                    reasons.append(
                        "A bounded read-only database probe took at least 100 ms."
                    )
            if database.get("probe_error"):
                score += 20
                reasons.append("A bounded read-only database probe failed.")
            if int(database.get("deadlocks_delta") or 0) > 0:
                score += 45
                reasons.append(
                    "New PostgreSQL deadlocks were recorded during this interval."
                )
            if int(database.get("lock_waiters") or 0) > 0:
                score += 25
                reasons.append("PostgreSQL sessions are currently waiting on locks.")
            if float(database.get("oldest_transaction_seconds") or 0) >= 300:
                score += 20
                reasons.append(
                    "A PostgreSQL transaction has remained open for at least five minutes."
                )
        return min(score, 100), list(dict.fromkeys(reasons))

    @staticmethod
    def _pressure_label(result, score):
        if score >= 70:
            return "critical"
        if score >= 45:
            return "high"
        if score >= 20:
            return "moderate"
        databases = result.get("databases") or []
        if databases and not any(db.get("exists") is True for db in databases):
            if (
                result.get("provider") == "postgresql"
                and result.get("mode") == "standard"
            ):
                return "observing"
            if result.get("provider") in set(CUSTOM_STORE_PROVIDERS.values()):
                return "observing"
            return "unavailable"
        return "healthy"

    @staticmethod
    def _recommendation(result, reasons):
        if result.get("pressure") == "unavailable":
            return "The configured database is unavailable or has not been created yet."
        if not reasons:
            if (
                result.get("provider") == "postgresql"
                and result.get("mode") == "standard"
            ):
                return "Passive monitoring is active. Enable enhanced mode for bounded PostgreSQL statistics queries."
            if result.get("provider") in set(CUSTOM_STORE_PROVIDERS.values()):
                return "Passive store monitoring is active. DUMB will report file, directory, filesystem, and log pressure without opening the application-owned store."
            return "No database pressure indicators have been observed. Keep the current provider and continue collecting a representative workload."
        storage_entries = [
            database.get("storage") or {} for database in result.get("databases") or []
        ]
        if any(storage.get("read_only") is True for storage in storage_entries):
            return "Restore writable database storage before troubleshooting database-engine performance."
        if any(
            float(storage.get("inode_used_percent") or 0) >= 90
            for storage in storage_entries
        ):
            return "Free inodes on the database filesystem before evaluating database-engine changes."
        if any(
            float(storage.get("used_percent") or 0) >= 90 for storage in storage_entries
        ):
            return "Free space on the database filesystem before evaluating database-engine changes."
        if not result.get("ignore_network_storage") and any(
            (database.get("storage") or {}).get("network")
            for database in result.get("databases") or []
        ):
            if result.get("provider") == "sqlite":
                return "Move SQLite to local storage before treating PostgreSQL as the first performance fix."
            return "Move the persistent store to local storage before evaluating provider-specific performance changes."
        if result.get("provider") == "sqlite" and any(
            token in " ".join(reasons).lower() for token in ("lock", "busy", "timeout")
        ):
            return "SQLite contention is visible. Continue collection through peak workload and evaluate PostgreSQL support or reduced write concurrency."
        return "Review the recorded indicators and correlate them with imports, scans, maintenance, and playback before changing providers."

    @staticmethod
    def _compact_result(result):
        databases = []
        for db in result.get("databases") or []:
            databases.append(
                {
                    key: db.get(key)
                    for key in (
                        "role",
                        "name",
                        "exists",
                        "size_bytes",
                        "wal_size_bytes",
                        "file_count",
                        "scan_truncated",
                        "store_kind",
                        "probe_ms",
                        "lock_waiters",
                        "deadlocks_delta",
                        "temp_bytes_delta",
                    )
                    if key in db
                }
            )
        return {
            key: result.get(key)
            for key in (
                "id",
                "config_key",
                "instance_name",
                "process_name",
                "provider",
                "monitoring_enabled",
                "mode",
                "ignore_network_storage",
                "pressure",
                "score",
                "collected_at",
            )
        } | {
            "log_signals": {
                key: (result.get("log_signals") or {}).get(key, 0)
                for key in LOG_PATTERNS
            },
            "databases": databases,
        }


def _provider_for_service(key: str, service: dict[str, Any]) -> str:
    if key in ARR_DATABASE_FILES:
        return "postgresql" if service.get("postgres_enabled") is True else "sqlite"
    if key in POSTGRES_SERVICE_DATABASES or key == "postgres":
        return "postgresql"
    if key == "bazarr":
        env = service.get("env") or {}
        return (
            "postgresql"
            if service.get("postgres_enabled") is True
            or str(env.get("POSTGRES_ENABLED") or "false").strip().lower() == "true"
            else "sqlite"
        )
    if key == "pulsarr":
        env = service.get("env") or {}
        return (
            "postgresql"
            if str(env.get("dbType") or "sqlite").strip().lower() == "postgres"
            else "sqlite"
        )
    if key == "seerr":
        env = service.get("env") or {}
        return (
            "postgresql"
            if str(env.get("DB_TYPE") or "sqlite").strip().lower() == "postgres"
            else "sqlite"
        )
    if key == "altmount":
        database = _altmount_database_config(service)
        return (
            "postgresql"
            if str(database.get("type") or "sqlite").strip().lower() == "postgres"
            else "sqlite"
        )
    return CUSTOM_STORE_PROVIDERS.get(key, "sqlite")


def _altmount_database_config(service: dict[str, Any]) -> dict[str, Any]:
    config_file = str(service.get("config_file") or "")
    if config_file and os.path.isfile(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as handle:
                parsed = yaml.safe_load(handle) or {}
            database = parsed.get("database") or {}
            if isinstance(database, dict):
                return database
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            pass
    return {}


def _service_postgres_connection(key, service, config):
    del config
    env = service.get("env") or {}
    if key == "bazarr":
        return {
            "host": env.get("POSTGRES_HOST") or "127.0.0.1",
            "port": env.get("POSTGRES_PORT") or 5432,
            "user": env.get("POSTGRES_USERNAME"),
            "password": env.get("POSTGRES_PASSWORD"),
            "database": env.get("POSTGRES_DATABASE") or "bazarr",
        }
    if key == "pulsarr":
        dsn = str(env.get("dbConnectionString") or "").strip()
        if dsn:
            return {"dsn": dsn, "database": _dsn_database(dsn) or "pulsarr"}
        return {
            "host": env.get("dbHost") or "127.0.0.1",
            "port": env.get("dbPort") or 5432,
            "user": env.get("dbUser"),
            "password": env.get("dbPassword"),
            "database": env.get("dbName") or "pulsarr",
        }
    if key == "seerr":
        return {
            "host": env.get("DB_HOST") or env.get("DB_SOCKET_PATH") or "127.0.0.1",
            "port": env.get("DB_PORT") or 5432,
            "user": env.get("DB_USER"),
            "password": env.get("DB_PASS"),
            "database": env.get("DB_NAME") or "seerr",
        }
    if key == "altmount":
        database = _altmount_database_config(service)
        dsn = str(database.get("dsn") or "").strip()
        return {"dsn": dsn, "database": _dsn_database(dsn) or "altmount"}
    return {"database": key}


def _dsn_database(dsn: str) -> str | None:
    try:
        parsed = urlparse(dsn)
        database = unquote(parsed.path.lstrip("/"))
        return database or None
    except (TypeError, ValueError):
        return None


def _uses_local_postgres(connection: dict[str, Any] | None) -> bool:
    if not connection:
        return True
    host = connection.get("host")
    if connection.get("dsn"):
        try:
            host = urlparse(str(connection["dsn"])).hostname
        except ValueError:
            return False
    return str(host or "127.0.0.1").strip().lower() in {
        "127.0.0.1",
        "localhost",
        "0.0.0.0",
        "::1",
    }


def _discovered_sqlite_paths(
    root: str,
    fallback: tuple[str, str],
    limit: int = 32,
    directory_limit: int = 256,
) -> list[tuple[str, str]]:
    paths = []
    visited = 0
    for current, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        visited += 1
        if visited >= directory_limit:
            directories.clear()
        for filename in sorted(files):
            if filename.lower().endswith((".db", ".sqlite", ".sqlite3")):
                paths.append(os.path.join(current, filename))
                if len(paths) >= limit:
                    break
        if len(paths) >= limit:
            break
    if not paths:
        return [fallback]
    return [
        (
            os.path.splitext(os.path.relpath(path, root))[0].replace(os.sep, " / "),
            path,
        )
        for path in paths
    ]


def _bounded_directory_size(path: str, max_files: int = 5000):
    total = 0
    file_count = 0
    pending = [path]
    truncated = False
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                total += entry.stat(follow_symlinks=False).st_size
                file_count += 1
                if file_count >= max_files:
                    return total, file_count, True
    return total, file_count, truncated


def _storage_for_path(path: str) -> dict[str, Any]:
    resolved = os.path.realpath(path)
    best = {"mount_point": "/", "fs_type": None, "source": None}
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as handle:
            for line in handle:
                left, right = line.rstrip("\n").split(" - ", 1)
                left_fields = left.split()
                right_fields = right.split()
                mount_point = _decode_mount_field(left_fields[4])
                if resolved == mount_point or resolved.startswith(
                    mount_point.rstrip("/") + "/"
                ):
                    if len(mount_point) >= len(best["mount_point"]):
                        best = {
                            "mount_point": mount_point,
                            "fs_type": right_fields[0] if right_fields else None,
                            "source": (
                                _decode_mount_field(right_fields[1])
                                if len(right_fields) > 1
                                else None
                            ),
                        }
    except (OSError, ValueError, IndexError):
        pass
    fs_type = str(best.get("fs_type") or "").lower()
    best["network"] = fs_type in NETWORK_FILESYSTEMS or fs_type.startswith("fuse.sshfs")
    try:
        filesystem = os.statvfs(resolved)
        block_size = filesystem.f_frsize or filesystem.f_bsize
        total_bytes = int(filesystem.f_blocks * block_size)
        free_bytes = max(int(filesystem.f_bavail * block_size), 0)
        used_bytes = max(
            int((filesystem.f_blocks - filesystem.f_bfree) * block_size), 0
        )
        usable_bytes = used_bytes + free_bytes
        total_inodes = int(filesystem.f_files)
        inode_counts_available = (
            total_inodes > 0
            and int(filesystem.f_ffree) >= 0
            and int(filesystem.f_favail) >= 0
        )
        free_inodes = int(filesystem.f_favail) if inode_counts_available else None
        used_inodes = (
            max(total_inodes - int(filesystem.f_ffree), 0)
            if inode_counts_available
            else None
        )
        usable_inodes = used_inodes + free_inodes if inode_counts_available else None
        best.update(
            {
                "total_bytes": total_bytes,
                "free_bytes": free_bytes,
                "used_percent": (
                    round(used_bytes / usable_bytes * 100, 2) if usable_bytes else None
                ),
                "total_inodes": total_inodes if inode_counts_available else None,
                "free_inodes": free_inodes,
                "inode_used_percent": (
                    round(used_inodes / usable_inodes * 100, 2)
                    if usable_inodes
                    else None
                ),
                "read_only": bool(filesystem.f_flag & getattr(os, "ST_RDONLY", 1)),
            }
        )
    except OSError:
        pass
    return best


def _decode_mount_field(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _first_value(connection, query):
    row = connection.execute(query).fetchone()
    return row[0] if row else None


def _int_value(connection, query):
    value = _first_value(connection, query)
    return int(value) if value is not None else None


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _counter_delta(current, previous):
    if previous is None or current < previous:
        return 0
    return current - previous


def _safe_error(exc):
    message = str(exc).replace("\n", " ").strip()
    message = re.sub(r"(?i)(postgres(?:ql)?://[^:\s/]+:)[^@\s]+@", r"\1***@", message)
    message = re.sub(r"(?i)(password\s*[=:]\s*)[^;\s]+", r"\1***", message)
    return message[:240] if message else exc.__class__.__name__


def _bounded_int(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))
