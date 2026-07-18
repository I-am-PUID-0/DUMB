import json
import os
import re
import sqlite3
import threading
import time
import zlib

import psycopg2
from psycopg2 import sql

from utils.metrics_history_reader import _list_history_files, _read_history_file

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAINTENANCE_INTERVAL_SECONDS = 300


def _encode_snapshot(snapshot):
    raw = json.dumps(snapshot, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return zlib.compress(raw, level=6), len(raw)


def _decode_snapshot(payload):
    if isinstance(payload, memoryview):
        payload = payload.tobytes()
    return json.loads(zlib.decompress(bytes(payload)).decode("utf-8"))


def _safe_identifier(value, fallback):
    value = str(value or fallback).strip()
    return value if _IDENTIFIER_RE.fullmatch(value) else fallback


class SQLiteMetricsHistoryStore:
    def __init__(self, path, logger=None):
        self.path = str(path)
        self.logger = logger
        self._lock = threading.RLock()
        self._ensure_schema()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _ensure_schema(self):
        directory = os.path.dirname(self.path) or "."
        directory_created = not os.path.exists(directory)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if directory_created:
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA auto_vacuum = FULL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS metrics_snapshots (
                    timestamp REAL PRIMARY KEY,
                    payload BLOB NOT NULL,
                    raw_size INTEGER NOT NULL,
                    stored_size INTEGER NOT NULL,
                    created_at REAL NOT NULL
                )
                """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS metrics_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_metrics_created_at "
                "ON metrics_snapshots(created_at)"
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def write(self, snapshot):
        self.write_many([snapshot])
        timestamp = snapshot.get("timestamp")
        return float(time.time() if timestamp is None else timestamp)

    def write_many(self, snapshots):
        rows = []
        for snapshot in snapshots:
            timestamp_value = snapshot.get("timestamp")
            timestamp = float(
                time.time() if timestamp_value is None else timestamp_value
            )
            snapshot = dict(snapshot)
            snapshot["timestamp"] = timestamp
            payload, raw_size = _encode_snapshot(snapshot)
            rows.append((timestamp, payload, raw_size, len(payload), time.time()))
        if not rows:
            return 0
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO metrics_snapshots
                    (timestamp, payload, raw_size, stored_size, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(timestamp) DO UPDATE SET
                    payload = excluded.payload,
                    raw_size = excluded.raw_size,
                    stored_size = excluded.stored_size
                """,
                rows,
            )
        return len(rows)

    def read(self, since=None, limit=5000):
        params = []
        where = ""
        if since is not None:
            where = "WHERE timestamp >= ?"
            params.append(float(since))
        limit_sql = ""
        if limit and limit > 0:
            limit_sql = "LIMIT ?"
            params.append(int(limit))
        query = (
            "SELECT payload FROM metrics_snapshots "
            f"{where} ORDER BY timestamp DESC {limit_sql}"
        )
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        items = [_decode_snapshot(row[0]) for row in reversed(rows)]
        return items

    def read_forward(self, since=None, limit=5000):
        params = []
        where = ""
        if since is not None:
            where = "WHERE timestamp >= ?"
            params.append(float(since))
        limit_sql = ""
        if limit and limit > 0:
            limit_sql = "LIMIT ?"
            params.append(int(limit))
        query = (
            "SELECT payload FROM metrics_snapshots "
            f"{where} ORDER BY timestamp ASC {limit_sql}"
        )
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_decode_snapshot(row[0]) for row in rows]

    def latest_timestamp(self):
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(timestamp) FROM metrics_snapshots"
            ).fetchone()
        return row[0] if row and row[0] is not None else None

    def prune(self, retention_days=0, max_total_mb=0):
        deleted = 0
        with self._lock, self._connect() as connection:
            if retention_days and retention_days > 0:
                cutoff = time.time() - (float(retention_days) * 86400)
                cursor = connection.execute(
                    "DELETE FROM metrics_snapshots WHERE timestamp < ?", (cutoff,)
                )
                deleted += max(cursor.rowcount, 0)
            if max_total_mb and max_total_mb > 0:
                max_bytes = int(float(max_total_mb) * 1024 * 1024)
                row = connection.execute(
                    "SELECT COALESCE(SUM(stored_size), 0) FROM metrics_snapshots"
                ).fetchone()
                stored_bytes = int(row[0] or 0)
                while stored_bytes > max_bytes:
                    rows = connection.execute(
                        "SELECT timestamp, stored_size FROM metrics_snapshots "
                        "ORDER BY timestamp ASC LIMIT 1000"
                    ).fetchall()
                    if not rows:
                        break
                    timestamps = [row[0] for row in rows]
                    reclaimed = sum(int(row[1] or 0) for row in rows)
                    connection.executemany(
                        "DELETE FROM metrics_snapshots WHERE timestamp = ?",
                        [(timestamp,) for timestamp in timestamps],
                    )
                    stored_bytes -= reclaimed
                    deleted += len(rows)
        return deleted

    def metadata(self, key, default=None):
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM metrics_metadata WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else default

    def set_metadata(self, key, value):
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO metrics_metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )

    def status(self):
        with self._lock, self._connect() as connection:
            row = connection.execute("""
                SELECT COUNT(*), COALESCE(SUM(raw_size), 0),
                       COALESCE(SUM(stored_size), 0), MIN(timestamp), MAX(timestamp)
                FROM metrics_snapshots
                """).fetchone()
        file_size = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                file_size += os.path.getsize(f"{self.path}{suffix}")
            except OSError:
                pass
        raw_size = int(row[1] or 0)
        stored_size = int(row[2] or 0)
        return {
            "path": self.path,
            "samples": int(row[0] or 0),
            "raw_bytes": raw_size,
            "compressed_bytes": stored_size,
            "file_bytes": file_size,
            "compression_ratio": (
                round(stored_size / raw_size, 4) if raw_size else None
            ),
            "oldest_timestamp": row[3],
            "newest_timestamp": row[4],
        }


class PostgreSQLMetricsHistoryStore:
    def __init__(self, postgres_config, database, schema="public", logger=None):
        self.postgres_config = postgres_config or {}
        self.database = _safe_identifier(database, "dumb_metrics")
        self.schema = _safe_identifier(schema, "public")
        self.logger = logger
        self._ready = False
        self._lock = threading.RLock()

    def _connection_kwargs(self, database=None):
        return {
            "dbname": database or self.database,
            "user": self.postgres_config.get("user", "DUMB"),
            "password": self.postgres_config.get("password", "postgres"),
            "host": self.postgres_config.get("host", "127.0.0.1"),
            "port": int(self.postgres_config.get("port", 5432)),
            "connect_timeout": 5,
        }

    def _ensure_schema(self):
        with self._lock:
            if self._ready:
                return
            admin = psycopg2.connect(**self._connection_kwargs(database="postgres"))
            admin.autocommit = True
            try:
                with admin.cursor() as cursor:
                    cursor.execute(
                        "SELECT 1 FROM pg_database WHERE datname = %s",
                        (self.database,),
                    )
                    if cursor.fetchone() is None:
                        cursor.execute(
                            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                                sql.Identifier(self.database),
                                sql.Identifier(
                                    self.postgres_config.get("user", "DUMB")
                                ),
                            )
                        )
            finally:
                admin.close()
            connection = psycopg2.connect(**self._connection_kwargs())
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                            sql.Identifier(self.schema)
                        )
                    )
                    cursor.execute(sql.SQL("""
                            CREATE TABLE IF NOT EXISTS {}.metrics_snapshots (
                                timestamp DOUBLE PRECISION PRIMARY KEY,
                                payload BYTEA NOT NULL,
                                raw_size BIGINT NOT NULL,
                                stored_size BIGINT NOT NULL,
                                created_at DOUBLE PRECISION NOT NULL
                            )
                            """).format(sql.Identifier(self.schema)))
                    cursor.execute(
                        sql.SQL(
                            "CREATE INDEX IF NOT EXISTS idx_metrics_created_at "
                            "ON {}.metrics_snapshots(created_at)"
                        ).format(sql.Identifier(self.schema))
                    )
                connection.commit()
            finally:
                connection.close()
            self._ready = True

    def write(self, snapshot):
        self.write_many([snapshot])
        timestamp = snapshot.get("timestamp")
        return float(time.time() if timestamp is None else timestamp)

    def write_many(self, snapshots):
        if not snapshots:
            return 0
        self._ensure_schema()
        rows = []
        for snapshot in snapshots:
            timestamp_value = snapshot.get("timestamp")
            timestamp = float(
                time.time() if timestamp_value is None else timestamp_value
            )
            snapshot = dict(snapshot)
            snapshot["timestamp"] = timestamp
            payload, raw_size = _encode_snapshot(snapshot)
            rows.append(
                (
                    timestamp,
                    psycopg2.Binary(payload),
                    raw_size,
                    len(payload),
                    time.time(),
                )
            )
        connection = psycopg2.connect(**self._connection_kwargs())
        try:
            with connection.cursor() as cursor:
                cursor.executemany(
                    sql.SQL("""
                        INSERT INTO {}.metrics_snapshots
                            (timestamp, payload, raw_size, stored_size, created_at)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT(timestamp) DO UPDATE SET
                            payload = EXCLUDED.payload,
                            raw_size = EXCLUDED.raw_size,
                            stored_size = EXCLUDED.stored_size
                        """).format(sql.Identifier(self.schema)),
                    rows,
                )
            connection.commit()
        finally:
            connection.close()
        return len(rows)

    def read(self, since=None, limit=5000):
        self._ensure_schema()
        where = sql.SQL("")
        params = []
        if since is not None:
            where = sql.SQL("WHERE timestamp >= %s")
            params.append(float(since))
        limit_sql = sql.SQL("")
        if limit and limit > 0:
            limit_sql = sql.SQL("LIMIT %s")
            params.append(int(limit))
        query = sql.SQL(
            "SELECT payload FROM {}.metrics_snapshots {} " "ORDER BY timestamp DESC {}"
        ).format(sql.Identifier(self.schema), where, limit_sql)
        connection = psycopg2.connect(**self._connection_kwargs())
        try:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall()
        finally:
            connection.close()
        return [_decode_snapshot(row[0]) for row in reversed(rows)]

    def latest_timestamp(self):
        self._ensure_schema()
        connection = psycopg2.connect(**self._connection_kwargs())
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SELECT MAX(timestamp) FROM {}.metrics_snapshots").format(
                        sql.Identifier(self.schema)
                    )
                )
                row = cursor.fetchone()
        finally:
            connection.close()
        return row[0] if row and row[0] is not None else None

    def prune(self, retention_days=0, max_total_mb=0):
        del max_total_mb
        if not retention_days or retention_days <= 0:
            return 0
        self._ensure_schema()
        cutoff = time.time() - (float(retention_days) * 86400)
        connection = psycopg2.connect(**self._connection_kwargs())
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        "DELETE FROM {}.metrics_snapshots WHERE timestamp < %s"
                    ).format(sql.Identifier(self.schema)),
                    (cutoff,),
                )
                deleted = max(cursor.rowcount, 0)
            connection.commit()
        finally:
            connection.close()
        return deleted

    def status(self):
        self._ensure_schema()
        connection = psycopg2.connect(**self._connection_kwargs())
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("""
                        SELECT COUNT(*), COALESCE(SUM(raw_size), 0),
                               COALESCE(SUM(stored_size), 0),
                               MIN(timestamp), MAX(timestamp),
                               pg_total_relation_size(%s::regclass)
                        FROM {}.metrics_snapshots
                        """).format(sql.Identifier(self.schema)),
                    (f"{self.schema}.metrics_snapshots",),
                )
                row = cursor.fetchone()
        finally:
            connection.close()
        return {
            "database": self.database,
            "schema": self.schema,
            "samples": int(row[0] or 0),
            "raw_bytes": int(row[1] or 0),
            "compressed_bytes": int(row[2] or 0),
            "relation_bytes": int(row[5] or 0),
            "compression_ratio": (
                round(int(row[2]) / int(row[1]), 4) if row[1] else None
            ),
            "oldest_timestamp": row[3],
            "newest_timestamp": row[4],
        }


class MetricsHistoryManager:
    def __init__(self, config_manager, logger=None):
        self.config_manager = config_manager
        self.logger = logger
        self._lock = threading.RLock()
        self._config_key = None
        self._maintenance_config_key = None
        self._sqlite = None
        self._postgres = None
        self._configured_provider = "sqlite"
        self._active_provider = "sqlite"
        self._last_error = None
        self._last_postgres_attempt = 0.0
        self._last_postgres_success = None
        self._last_sqlite_prune = 0.0
        self._last_postgres_prune = 0.0
        self._last_migration = {
            "completed": False,
            "files": 0,
            "samples": 0,
            "skipped": 0,
        }

    def _root_config(self):
        config = (
            self.config_manager.config
            if hasattr(self.config_manager, "config")
            else self.config_manager
        )
        return config or {}

    def _metrics_config(self):
        return (self._root_config().get("dumb", {}) or {}).get("metrics", {}) or {}

    def _configure(self):
        metrics = self._metrics_config()
        storage = metrics.get("storage", {}) or {}
        provider = str(storage.get("provider", "sqlite")).lower()
        if provider not in {"sqlite", "postgresql"}:
            provider = "sqlite"
        history_dir = metrics.get("history_dir", "/config/metrics")
        sqlite_path = storage.get("sqlite_path") or os.path.join(
            history_dir, "metrics.sqlite"
        )
        pg_storage = storage.get("postgresql", {}) or {}
        postgres_config = self._root_config().get("postgres", {}) or {}
        maintenance_key = (
            metrics.get("history_retention_days", 7),
            metrics.get("history_max_total_mb", 100),
            pg_storage.get("local_retention_days", 7),
        )
        if maintenance_key != self._maintenance_config_key:
            # Apply changed retention limits to the next stored sample without
            # requiring the database connection itself to be rebuilt.
            self._last_sqlite_prune = 0.0
            self._last_postgres_prune = 0.0
            self._maintenance_config_key = maintenance_key
        key = (
            provider,
            sqlite_path,
            pg_storage.get("database", "dumb_metrics"),
            pg_storage.get("schema", "public"),
            postgres_config.get("host"),
            postgres_config.get("port"),
            postgres_config.get("user"),
            postgres_config.get("password"),
        )
        if key == self._config_key:
            return metrics, storage
        self._sqlite = SQLiteMetricsHistoryStore(sqlite_path, logger=self.logger)
        self._postgres = None
        if provider == "postgresql":
            self._postgres = PostgreSQLMetricsHistoryStore(
                postgres_config=postgres_config,
                database=pg_storage.get("database", "dumb_metrics"),
                schema=pg_storage.get("schema", "public"),
                logger=self.logger,
            )
        self._configured_provider = provider
        self._active_provider = "sqlite"
        self._config_key = key
        self._last_error = None
        # A newly opened store needs an initial maintenance pass, while later
        # samples use the bounded maintenance interval.
        self._last_sqlite_prune = 0.0
        self._last_postgres_prune = 0.0
        if storage.get("migrate_jsonl", True):
            self.migrate_legacy(force=False, _configured=True)
        return metrics, storage

    def migrate_legacy(self, force=False, _configured=False):
        with self._lock:
            if not _configured:
                metrics, _storage = self._configure()
            else:
                metrics = self._metrics_config()
            migration_key = "jsonl_migration_v1"
            if not force and self._sqlite.metadata(migration_key) == "complete":
                self._last_migration = {
                    "completed": True,
                    "files": int(self._sqlite.metadata("jsonl_migration_files", 0)),
                    "samples": int(self._sqlite.metadata("jsonl_migration_samples", 0)),
                    "skipped": 0,
                }
                return dict(self._last_migration)
            history_dir = metrics.get("history_dir", "/config/metrics")
            files = _list_history_files(history_dir)
            samples = 0
            skipped = 0
            batch = []
            for path in files:
                decode_errors = []
                for snapshot in _read_history_file(
                    path,
                    on_decode_error=lambda *_args: decode_errors.append(1),
                ):
                    try:
                        batch.append(snapshot)
                        if len(batch) >= 1000:
                            samples += self._sqlite.write_many(batch)
                            batch = []
                    except Exception:
                        skipped += len(batch) or 1
                        batch = []
                skipped += len(decode_errors)
            if batch:
                try:
                    samples += self._sqlite.write_many(batch)
                except Exception:
                    skipped += len(batch)
            completed = skipped == 0
            self._sqlite.set_metadata(
                migration_key, "complete" if completed else "incomplete"
            )
            self._sqlite.set_metadata("jsonl_migration_files", len(files))
            self._sqlite.set_metadata("jsonl_migration_samples", samples)
            self._sqlite.set_metadata("jsonl_migration_skipped", skipped)
            self._last_migration = {
                "completed": completed,
                "files": len(files),
                "samples": samples,
                "skipped": skipped,
            }
            if skipped and self.logger:
                self.logger.warning(
                    "Metrics JSONL migration left %s sample(s) incomplete; "
                    "the source files remain available and the migration remains "
                    "retryable on the next import or DUMB startup.",
                    skipped,
                )
            if self._configured_provider == "postgresql":
                # A forced rescan can insert samples older than PostgreSQL's
                # current maximum timestamp. Require a full reconciliation
                # before PostgreSQL is allowed to serve reads again.
                self._active_provider = "sqlite"
            return dict(self._last_migration)

    def _postgres_retry_interval(self, storage):
        postgres = storage.get("postgresql", {}) or {}
        try:
            return max(15, int(postgres.get("retry_interval_sec", 60)))
        except (TypeError, ValueError):
            return 60

    def _postgres_available(self, storage, force=False):
        if not self._postgres:
            return False
        if not force and self._last_error:
            if (
                time.time() - self._last_postgres_attempt
                < self._postgres_retry_interval(storage)
            ):
                return False
        return True

    def _sync_postgres(self, full=False):
        latest = None if full else self._postgres.latest_timestamp()
        since = (float(latest) + 0.000001) if latest is not None else None
        synced = 0
        while True:
            items = self._sqlite.read_forward(since=since, limit=5000)
            if not items:
                break
            synced += self._postgres.write_many(items)
            since = float(items[-1].get("timestamp")) + 0.000001
            if len(items) < 5000:
                break
        return synced

    def write(self, snapshot):
        with self._lock:
            metrics, storage = self._configure()
            self._sqlite.write(snapshot)
            now = time.time()
            if self._configured_provider == "postgresql" and self._postgres_available(
                storage
            ):
                self._last_postgres_attempt = time.time()
                try:
                    self._sync_postgres(full=self._active_provider != "postgresql")
                    if now - self._last_postgres_prune >= _MAINTENANCE_INTERVAL_SECONDS:
                        self._postgres.prune(
                            retention_days=metrics.get("history_retention_days", 7)
                        )
                        self._last_postgres_prune = now
                    self._active_provider = "postgresql"
                    self._last_error = None
                    self._last_postgres_success = time.time()
                except Exception as exc:
                    self._active_provider = "sqlite"
                    self._last_error = str(exc)
                    if self.logger:
                        self.logger.warning(
                            "PostgreSQL metrics history unavailable; using local "
                            "SQLite fallback: %s",
                            exc,
                        )
            local_retention = metrics.get("history_retention_days", 7)
            if self._configured_provider == "postgresql":
                local_retention = (storage.get("postgresql", {}) or {}).get(
                    "local_retention_days", 7
                )
            if now - self._last_sqlite_prune >= _MAINTENANCE_INTERVAL_SECONDS:
                self._sqlite.prune(
                    retention_days=local_retention,
                    max_total_mb=metrics.get("history_max_total_mb", 100),
                )
                self._last_sqlite_prune = now

    def activate_postgresql(self):
        """Replay the continuity buffer and promote PostgreSQL for reads."""
        with self._lock:
            metrics, storage = self._configure()
            if self._configured_provider != "postgresql" or not self._postgres:
                raise RuntimeError(
                    "Metrics history is not configured to use PostgreSQL."
                )

            self._last_postgres_attempt = time.time()
            try:
                synced = self._sync_postgres(full=True)
                self._postgres.prune(
                    retention_days=metrics.get("history_retention_days", 7)
                )
                self._active_provider = "postgresql"
                self._last_error = None
                self._last_postgres_success = time.time()
                postgres_status = self._postgres.status()
                return {
                    "synced_samples": synced,
                    "active_provider": self._active_provider,
                    "fallback_active": False,
                    "postgresql": postgres_status,
                }
            except Exception as exc:
                self._active_provider = "sqlite"
                self._last_error = str(exc)
                if self.logger:
                    self.logger.warning(
                        "PostgreSQL Metrics history activation failed; keeping "
                        "SQLite active: %s",
                        exc,
                    )
                raise

    def read(self, since=None, full=False, limit=5000, default_hours=6):
        with self._lock:
            _metrics, storage = self._configure()
            if since is None and not full:
                since = time.time() - (default_hours * 60 * 60)
            if self._configured_provider == "postgresql" and self._postgres_available(
                storage
            ):
                self._last_postgres_attempt = time.time()
                try:
                    if self._active_provider != "postgresql":
                        self._sync_postgres(full=True)
                    items = self._postgres.read(since=since, limit=limit)
                    self._active_provider = "postgresql"
                    self._last_error = None
                    self._last_postgres_success = time.time()
                    return items, bool(limit and len(items) >= limit)
                except Exception as exc:
                    self._active_provider = "sqlite"
                    self._last_error = str(exc)
            items = self._sqlite.read(since=since, limit=limit)
            return items, bool(limit and len(items) >= limit)

    def status(self, probe_postgresql=False):
        with self._lock:
            _metrics, storage = self._configure()
            sqlite_status = self._sqlite.status()
            postgres_status = None
            if self._configured_provider == "postgresql" and self._postgres_available(
                storage, force=probe_postgresql
            ):
                self._last_postgres_attempt = time.time()
                try:
                    if self._active_provider != "postgresql":
                        self._sync_postgres(full=True)
                    postgres_status = self._postgres.status()
                    self._active_provider = "postgresql"
                    self._last_error = None
                    self._last_postgres_success = time.time()
                except Exception as exc:
                    self._active_provider = "sqlite"
                    self._last_error = str(exc)
            legacy_files = _list_history_files(
                self._metrics_config().get("history_dir", "/config/metrics")
            )
            legacy_bytes = 0
            for path in legacy_files:
                try:
                    legacy_bytes += os.path.getsize(path)
                except OSError:
                    pass
            return {
                "configured_provider": self._configured_provider,
                "active_provider": self._active_provider,
                "fallback_active": self._configured_provider != self._active_provider,
                "last_error": self._last_error,
                "last_postgresql_success": self._last_postgres_success,
                "sqlite": sqlite_status,
                "postgresql": postgres_status,
                "legacy_jsonl": {
                    "files": len(legacy_files),
                    "bytes": legacy_bytes,
                    "migration": dict(self._last_migration),
                    "preserved": True,
                },
            }
