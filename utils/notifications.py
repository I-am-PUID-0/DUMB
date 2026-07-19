import json
import os
import queue
import re
import sqlite3
import threading
import time
import uuid
from copy import deepcopy

import requests
from jsonschema import ValidationError, validate

from utils.config_loader import CONFIG_MANAGER
from utils.logger import redact_sensitive_log_data
from utils.url_security import validate_url_scheme

SEVERITY_RANK = {"info": 0, "success": 1, "warning": 2, "critical": 3}
SUPPORTED_EVENT_TYPES = (
    "manual",
    "dumb.startup.degraded",
    "service.preinstall.failed",
    "service.start.failed",
    "service.unhealthy",
    "service.stopped.unexpectedly",
    "service.auto_restart.attempt",
    "service.auto_restart.succeeded",
    "service.auto_restart.failed",
    "service.auto_restart.suppressed",
    "update.available",
    "update.succeeded",
    "update.failed",
    "symlink.job.succeeded",
    "symlink.job.failed",
    "resource.cpu.high",
    "resource.memory.high",
    "resource.disk.high",
    "resource.inode.high",
    "database.pressure",
    "database.collection.failed",
    "recovery",
)

DEFAULT_NOTIFICATION_CONFIG = {
    "enabled": False,
    "monitor_interval_sec": 30,
    "history_retention_days": 30,
    "max_attempts": 3,
    "retry_base_sec": 30,
    "destinations": [],
    "thresholds": {
        "cpu_percent": 85,
        "memory_percent": 85,
        "disk_percent": 90,
        "inode_percent": 90,
        "database_pressure": "high",
        "duration_sec": 60,
    },
}

_STORAGE_INITIALIZATION_ATTEMPTS = 4
_STORAGE_INITIALIZATION_TIMEOUT_SECONDS = 1
_STORAGE_RETRY_BASE_SECONDS = 0.25
_STORAGE_RETRY_INTERVAL_SECONDS = 5


class NotificationStorageUnavailableError(RuntimeError):
    pass


def _enabled_process_names(config=None):
    source = (
        config
        if isinstance(config, dict)
        else getattr(CONFIG_MANAGER, "config", {}) or {}
    )
    names = set()

    def collect(value):
        if not isinstance(value, dict):
            return
        enabled = (
            value.get("enabled") is True
            or str(value.get("enabled", "")).lower() == "true"
        )
        process_name = str(value.get("process_name") or "").strip()
        if enabled and process_name:
            names.add(process_name)
        for nested in value.values():
            if isinstance(nested, dict):
                collect(nested)

    collect(source)
    return names


def _notification_config(config=None):
    source = (
        config
        if isinstance(config, dict)
        else getattr(CONFIG_MANAGER, "config", {}) or {}
    )
    configured = (
        source.get("dumb", {}).get("notifications", {})
        if isinstance(source, dict)
        else {}
    )
    merged = deepcopy(DEFAULT_NOTIFICATION_CONFIG)
    if isinstance(configured, dict):
        for key, value in configured.items():
            if key == "thresholds" and isinstance(value, dict):
                merged["thresholds"].update(value)
            else:
                merged[key] = value
    return merged


def _safe_error(error):
    value = redact_sensitive_log_data(str(error or "Notification delivery failed"))
    value = re.sub(r"([a-z][a-z0-9+.-]*://)[^\s]+", r"\1[redacted]", value, flags=re.I)
    return value[:1000]


def _safe_metadata(value):
    if isinstance(value, dict):
        return {str(key): _safe_metadata(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_metadata(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_log_data(value)[:2000]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return redact_sensitive_log_data(str(value))[:2000]


class NotificationManager:
    def __init__(self, process_handler, metrics_collector, logger, base_dir=None):
        self.process_handler = process_handler
        self.metrics_collector = metrics_collector
        self.logger = logger
        self.base_dir = base_dir or "/config/notifications"
        self.db_path = os.path.join(self.base_dir, "notifications.sqlite")
        self.apprise_storage_path = os.path.join(self.base_dir, "apprise")
        self._db_lock = threading.RLock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._worker_thread = None
        self._monitor_thread = None
        self._conditions = {}
        self._last_prune = 0
        self._storage_ready = False
        self._storage_error = None
        self._storage_retry_at = 0.0
        self._storage_unavailable_logged = False
        initial_attempts = (
            _STORAGE_INITIALIZATION_ATTEMPTS
            if _notification_config().get("enabled")
            else 1
        )
        self._initialize_storage(attempts=initial_attempts)

    def _connect(self, timeout=15):
        connection = sqlite3.connect(self.db_path, timeout=timeout)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {max(0, int(timeout * 1000))}")
        return connection

    @staticmethod
    def _is_lock_error(error):
        message = str(error or "").lower()
        return "locked" in message or "busy" in message

    def _initialize_storage_once(self):
        with self._connect(
            timeout=_STORAGE_INITIALIZATION_TIMEOUT_SECONDS
        ) as connection:
            current_mode = connection.execute("PRAGMA journal_mode").fetchone()
            if not current_mode or str(current_mode[0]).lower() != "wal":
                connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    destination_id TEXT NOT NULL,
                    destination_name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    service_name TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    bypass_enabled INTEGER NOT NULL DEFAULT 0,
                    bypass_destination_enabled INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    error TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    sent_at REAL
                )
                """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_deliveries_due
                ON deliveries(status, next_attempt_at)
                """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_deliveries_created
                ON deliveries(created_at DESC)
                """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS delivery_state (
                    state_key TEXT PRIMARY KEY,
                    active INTEGER NOT NULL DEFAULT 0,
                    last_sent_at REAL,
                    last_value TEXT,
                    updated_at REAL NOT NULL
                )
                """)
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(deliveries)"
                ).fetchall()
            }
            if "bypass_enabled" not in columns:
                connection.execute(
                    "ALTER TABLE deliveries ADD COLUMN bypass_enabled INTEGER NOT NULL DEFAULT 0"
                )
            if "bypass_destination_enabled" not in columns:
                connection.execute(
                    "ALTER TABLE deliveries ADD COLUMN bypass_destination_enabled "
                    "INTEGER NOT NULL DEFAULT 0"
                )

    def _initialize_storage(self, attempts=_STORAGE_INITIALIZATION_ATTEMPTS):
        os.makedirs(self.base_dir, exist_ok=True)
        os.makedirs(self.apprise_storage_path, exist_ok=True)
        try:
            os.chmod(self.base_dir, 0o700)
        except OSError:
            pass
        last_error = None
        attempts = max(1, int(attempts or 1))
        for attempt in range(1, attempts + 1):
            try:
                with self._db_lock:
                    if self._storage_ready:
                        return True
                    self._initialize_storage_once()
                    recovered = self._storage_error is not None
                    self._storage_ready = True
                    self._storage_error = None
                    self._storage_retry_at = 0.0
                    self._storage_unavailable_logged = False
                if recovered:
                    self._log(
                        "info",
                        "Notification SQLite storage recovered; queued delivery "
                        "and history are available again.",
                    )
                break
            except sqlite3.OperationalError as error:
                if not self._is_lock_error(error):
                    raise
                last_error = error
                if attempt < attempts:
                    time.sleep(_STORAGE_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
        else:
            with self._db_lock:
                if self._storage_ready:
                    return True
                self._storage_ready = False
                self._storage_error = _safe_error(last_error)
                self._storage_retry_at = (
                    time.monotonic() + _STORAGE_RETRY_INTERVAL_SECONDS
                )
                should_log = not self._storage_unavailable_logged
                self._storage_unavailable_logged = True
            if should_log:
                self._log(
                    "warning",
                    "Notification SQLite storage is locked after %s attempt(s). "
                    "DUMB startup will continue and notification storage will retry "
                    "in the background: %s",
                    attempts,
                    self._storage_error,
                )
            return False
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass
        return True

    def _ensure_storage_ready(self, force=False):
        if self._storage_ready:
            return True
        if not force and time.monotonic() < self._storage_retry_at:
            return False
        return self._initialize_storage(attempts=1)

    def _handle_runtime_storage_error(self, error):
        if not isinstance(error, sqlite3.OperationalError) or not self._is_lock_error(
            error
        ):
            return False
        with self._db_lock:
            self._storage_ready = False
            self._storage_error = _safe_error(error)
            self._storage_retry_at = time.monotonic() + _STORAGE_RETRY_INTERVAL_SECONDS
            should_log = not self._storage_unavailable_logged
            self._storage_unavailable_logged = True
        if should_log:
            self._log(
                "warning",
                "Notification SQLite storage became locked. Delivery and history "
                "are paused while background recovery continues: %s",
                self._storage_error,
            )
        return True

    @staticmethod
    def _storage_unavailable_exception():
        return NotificationStorageUnavailableError(
            "Notification storage is temporarily unavailable because its SQLite "
            "database is locked. DUMB will retry automatically."
        )

    def _require_storage(self):
        if self._ensure_storage_ready(force=True):
            return
        raise self._storage_unavailable_exception()

    def start(self):
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="notification-delivery"
        )
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="notification-monitor"
        )
        self._worker_thread.start()
        self._monitor_thread.start()
        if self._storage_ready:
            self._log("info", "Notification utility initialized.")
        else:
            self._log(
                "warning",
                "Notification utility started in degraded mode while SQLite "
                "storage recovery is pending.",
            )

    def _log(self, level, message, *args):
        handler = getattr(self.logger, level, None)
        if callable(handler):
            handler(message, *args)

    def shutdown(self):
        self._stop_event.set()
        self._wake_event.set()
        for thread in (self._worker_thread, self._monitor_thread):
            if thread and thread.is_alive():
                thread.join(timeout=3)

    def get_config(self, redact=True):
        config = _notification_config()
        safe = deepcopy(config)
        enabled_processes = _enabled_process_names()
        for destination in safe.get("destinations", []):
            if not isinstance(destination, dict):
                continue
            destination["service_names"] = sorted(
                {
                    str(name).strip()
                    for name in destination.get("service_names", [])
                    if str(name).strip() in enabled_processes
                }
            )
            if not redact:
                continue
            destination["url_configured"] = bool(destination.get("url"))
            destination["headers_configured"] = bool(destination.get("headers"))
            destination["url"] = ""
            destination["headers"] = {}
        return safe

    def update_config(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Notification configuration must be an object.")
        current = _notification_config()
        next_config = deepcopy(DEFAULT_NOTIFICATION_CONFIG)
        for key in next_config:
            if key in payload:
                next_config[key] = deepcopy(payload[key])
            elif key in current:
                next_config[key] = deepcopy(current[key])

        existing = {
            item.get("id"): item
            for item in current.get("destinations", [])
            if isinstance(item, dict) and item.get("id")
        }
        normalized_destinations = []
        enabled_processes = _enabled_process_names()
        seen = set()
        for raw in next_config.get("destinations", []):
            if not isinstance(raw, dict):
                continue
            destination = deepcopy(raw)
            destination_id = str(destination.get("id") or uuid.uuid4().hex).strip()
            if destination_id in seen:
                raise ValueError(f"Duplicate destination id: {destination_id}")
            seen.add(destination_id)
            previous = existing.get(destination_id, {})
            if not str(destination.get("url") or "").strip():
                destination["url"] = previous.get("url", "")
            if not destination.get("headers"):
                destination["headers"] = previous.get("headers", {})
            destination.pop("url_configured", None)
            destination.pop("headers_configured", None)
            destination["id"] = destination_id
            destination.setdefault("name", "Notification destination")
            destination.setdefault("enabled", True)
            destination.setdefault("provider", "apprise")
            destination.setdefault("verify_tls", True)
            destination.setdefault("headers", {})
            destination.setdefault("minimum_severity", "warning")
            destination.setdefault("event_types", [])
            destination.setdefault("service_names", [])
            destination.setdefault("cooldown_sec", 300)
            destination.setdefault("send_recovery", True)
            requested_services = {
                str(name).strip()
                for name in destination.get("service_names", [])
                if str(name).strip()
            }
            unavailable_services = sorted(requested_services - enabled_processes)
            if unavailable_services:
                raise ValueError(
                    "Notification service filters may only include currently enabled "
                    f"services: {', '.join(unavailable_services)}"
                )
            destination["service_names"] = sorted(requested_services)
            if destination.get("provider") == "webhook" and destination.get("url"):
                try:
                    validate_url_scheme(destination["url"])
                except ValueError as error:
                    raise ValueError(
                        f"Invalid webhook URL for {destination['name']}: {error}"
                    ) from None
            normalized_destinations.append(destination)
        next_config["destinations"] = normalized_destinations

        notification_schema = (
            (getattr(CONFIG_MANAGER, "schema", {}) or {})
            .get("properties", {})
            .get("dumb", {})
            .get("properties", {})
            .get("notifications", {})
        )
        try:
            validate(instance=next_config, schema=notification_schema)
        except ValidationError as error:
            location = " -> ".join(str(item) for item in error.absolute_path) or "root"
            raise ValueError(
                f"Invalid notification configuration at {location}: {error.message}"
            ) from None

        CONFIG_MANAGER.config.setdefault("dumb", {})["notifications"] = next_config
        CONFIG_MANAGER.save_config()
        self._wake_event.set()
        return self.get_config(redact=True)

    def emit(
        self,
        event_type,
        severity,
        title,
        body,
        service_name=None,
        metadata=None,
        destination_ids=None,
        force=False,
        include_disabled_destinations=False,
    ):
        config = _notification_config()
        if not force and not config.get("enabled"):
            return []
        if not self._ensure_storage_ready(force=force):
            if force:
                raise self._storage_unavailable_exception()
            return []
        severity = severity if severity in SEVERITY_RANK else "info"
        title = redact_sensitive_log_data(str(title or "DUMB notification"))
        body = redact_sensitive_log_data(str(body or ""))
        event_id = uuid.uuid4().hex
        now = time.time()
        queued = []
        for destination in config.get("destinations", []):
            if not self._destination_matches(
                destination,
                event_type,
                severity,
                service_name,
                destination_ids,
                force,
                include_disabled_destinations,
                metadata,
            ):
                continue
            cooldown = max(0, int(destination.get("cooldown_sec", 0) or 0))
            cooldown_key = self._cooldown_key(destination, event_type, service_name)
            if not force and self._is_suppressed(cooldown_key, cooldown, now):
                self._record_suppressed(
                    event_id,
                    destination,
                    event_type,
                    severity,
                    title,
                    body,
                    service_name,
                    metadata,
                    now,
                )
                continue
            delivery_id = uuid.uuid4().hex
            with self._db_lock, self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO deliveries (
                        id, event_id, destination_id, destination_name, event_type,
                        severity, title, body, service_name, status, attempts,
                        bypass_enabled, bypass_destination_enabled,
                        next_attempt_at, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        delivery_id,
                        event_id,
                        destination.get("id", ""),
                        destination.get("name", "Notification destination"),
                        event_type,
                        severity,
                        str(title)[:300],
                        str(body)[:10000],
                        service_name,
                        1 if force else 0,
                        1 if include_disabled_destinations else 0,
                        now,
                        json.dumps(_safe_metadata(metadata or {}), default=str),
                        now,
                        now,
                    ),
                )
            self._set_last_sent(cooldown_key, now)
            queued.append(delivery_id)
        if queued:
            self._wake_event.set()
        return queued

    def send_test(self, destination_id, title=None, body=None):
        try:
            queued = self.emit(
                "manual",
                "info",
                title or "DUMB notification test",
                body or "Your DUMB notification destination is configured correctly.",
                destination_ids=[destination_id],
                force=True,
                include_disabled_destinations=True,
            )
            if not queued:
                raise ValueError("Destination was not found or has no configured URL.")
            deadline = time.time() + 15
            while time.time() < deadline:
                result = self.get_delivery(queued[0])
                if result and result.get("status") in (
                    "sent",
                    "failed",
                    "retrying",
                ):
                    return result
                time.sleep(0.1)
            return self.get_delivery(queued[0])
        except sqlite3.OperationalError as error:
            if self._handle_runtime_storage_error(error):
                raise self._storage_unavailable_exception() from None
            raise

    def send_manual(self, title, body, severity="info", destination_ids=None):
        try:
            return self.emit(
                "manual",
                severity,
                title,
                body,
                destination_ids=destination_ids,
                force=True,
                include_disabled_destinations=False,
            )
        except sqlite3.OperationalError as error:
            if self._handle_runtime_storage_error(error):
                raise self._storage_unavailable_exception() from None
            raise

    def _destination_matches(
        self,
        destination,
        event_type,
        severity,
        service_name,
        destination_ids,
        force,
        include_disabled_destinations,
        metadata,
    ):
        if not isinstance(destination, dict):
            return False
        if destination_ids is not None and destination.get("id") not in destination_ids:
            return False
        if not destination.get("enabled", True) and not include_disabled_destinations:
            return False
        if not str(destination.get("url") or "").strip():
            return False
        if force:
            return True
        if event_type != "recovery":
            minimum = destination.get("minimum_severity", "warning")
            if SEVERITY_RANK.get(severity, 0) < SEVERITY_RANK.get(minimum, 2):
                return False
        event_types = destination.get("event_types") or []
        if event_types:
            recovered_event = (
                metadata.get("recovered_event_type")
                if event_type == "recovery" and isinstance(metadata, dict)
                else None
            )
            if event_type not in event_types and recovered_event not in event_types:
                return False
        services = [
            str(item).strip().lower() for item in destination.get("service_names", [])
        ]
        if services and str(service_name or "").strip().lower() not in services:
            return False
        if event_type == "recovery" and not destination.get("send_recovery", True):
            return False
        return True

    def _cooldown_key(self, destination, event_type, service_name):
        return "|".join(
            (
                "cooldown",
                str(destination.get("id") or ""),
                str(event_type or ""),
                str(service_name or ""),
            )
        )

    def _is_suppressed(self, state_key, cooldown, now):
        if cooldown <= 0:
            return False
        with self._db_lock, self._connect() as connection:
            row = connection.execute(
                "SELECT last_sent_at FROM delivery_state WHERE state_key = ?",
                (state_key,),
            ).fetchone()
        return bool(
            row and row["last_sent_at"] and now - row["last_sent_at"] < cooldown
        )

    def _set_last_sent(self, state_key, now):
        with self._db_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO delivery_state (state_key, active, last_sent_at, updated_at)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    last_sent_at = excluded.last_sent_at,
                    updated_at = excluded.updated_at
                """,
                (state_key, now, now),
            )

    def _record_suppressed(
        self,
        event_id,
        destination,
        event_type,
        severity,
        title,
        body,
        service_name,
        metadata,
        now,
    ):
        with self._db_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO deliveries (
                    id, event_id, destination_id, destination_name, event_type,
                    severity, title, body, service_name, status, attempts,
                    next_attempt_at, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'suppressed', 0, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    event_id,
                    destination.get("id", ""),
                    destination.get("name", "Notification destination"),
                    event_type,
                    severity,
                    str(title)[:300],
                    str(body)[:10000],
                    service_name,
                    now,
                    json.dumps(_safe_metadata(metadata or {}), default=str),
                    now,
                    now,
                ),
            )

    def get_delivery(self, delivery_id):
        self._require_storage()
        try:
            with self._db_lock, self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)
                ).fetchone()
        except sqlite3.OperationalError as error:
            if self._handle_runtime_storage_error(error):
                raise self._storage_unavailable_exception() from None
            raise
        return self._serialize_row(row) if row else None

    def history(self, limit=100, status=None, event_type=None):
        self._require_storage()
        clauses = []
        params = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        try:
            with self._db_lock, self._connect() as connection:
                rows = connection.execute(
                    f"SELECT * FROM deliveries {where} "
                    "ORDER BY created_at DESC LIMIT ?",
                    params,
                ).fetchall()
        except sqlite3.OperationalError as error:
            if self._handle_runtime_storage_error(error):
                raise self._storage_unavailable_exception() from None
            raise
        return [self._serialize_row(row) for row in rows]

    def clear_history(self):
        self._require_storage()
        try:
            with self._db_lock, self._connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM deliveries WHERE status NOT IN ('queued', 'retrying')"
                )
        except sqlite3.OperationalError as error:
            if self._handle_runtime_storage_error(error):
                raise self._storage_unavailable_exception() from None
            raise
        return cursor.rowcount

    def _serialize_row(self, row):
        value = dict(row)
        try:
            value["metadata"] = json.loads(value.pop("metadata_json") or "{}")
        except (TypeError, ValueError):
            value["metadata"] = {}
            value.pop("metadata_json", None)
        return value

    def _worker_loop(self):
        while not self._stop_event.is_set():
            try:
                self._deliver_due()
            except Exception as error:
                if not self._handle_runtime_storage_error(error):
                    self._log(
                        "error",
                        "Notification delivery loop failed: %s",
                        _safe_error(error),
                    )
            self._wake_event.wait(timeout=1)
            self._wake_event.clear()

    def _deliver_due(self):
        if not self._ensure_storage_ready():
            return
        now = time.time()
        enabled = 1 if _notification_config().get("enabled") else 0
        with self._db_lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM deliveries
                WHERE status IN ('queued', 'retrying') AND next_attempt_at <= ?
                    AND (? = 1 OR bypass_enabled = 1)
                ORDER BY created_at ASC LIMIT 20
                """,
                (now, enabled),
            ).fetchall()
        for row in rows:
            self._deliver(row)

    def _deliver(self, row):
        config = _notification_config()
        destination = next(
            (
                item
                for item in config.get("destinations", [])
                if isinstance(item, dict) and item.get("id") == row["destination_id"]
            ),
            None,
        )
        if not destination:
            self._finish_delivery(row, False, "Destination no longer exists.")
            return
        if (
            not destination.get("enabled", True)
            and not row["bypass_destination_enabled"]
        ):
            self._defer_delivery(row)
            return
        try:
            provider = destination.get("provider", "apprise")
            if provider == "webhook":
                self._send_webhook(destination, row)
            elif provider == "apprise":
                self._send_apprise(destination, row)
            else:
                raise ValueError(f"Unsupported notification provider: {provider}")
            self._finish_delivery(row, True, None)
        except Exception as error:
            self._finish_delivery(row, False, _safe_error(error))

    def _defer_delivery(self, row, delay=30):
        """Leave a queued delivery pending while its destination is disabled."""
        now = time.time()
        with self._db_lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE deliveries SET next_attempt_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now + max(1, int(delay)), now, row["id"]),
            )

    def _send_webhook(self, destination, row):
        validate_url_scheme(destination["url"])
        response = requests.post(
            destination["url"],
            json={
                "source": "DUMB",
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "severity": row["severity"],
                "title": row["title"],
                "body": row["body"],
                "service_name": row["service_name"],
                "timestamp": row["created_at"],
            },
            headers=destination.get("headers") or {},
            timeout=15,
            verify=destination.get("verify_tls", True),
        )
        response.raise_for_status()

    def _send_apprise(self, destination, row):
        try:
            import apprise
        except ImportError as error:
            raise RuntimeError(
                "Apprise is not installed in this DUMB image."
            ) from error
        asset = apprise.AppriseAsset(storage_path=self.apprise_storage_path)
        client = apprise.Apprise(asset=asset)
        if not client.add(destination["url"]):
            raise ValueError("The Apprise URL is invalid or unsupported.")
        notify_type = {
            "info": apprise.NotifyType.INFO,
            "success": apprise.NotifyType.SUCCESS,
            "warning": apprise.NotifyType.WARNING,
            "critical": apprise.NotifyType.FAILURE,
        }.get(row["severity"], apprise.NotifyType.INFO)
        if not client.notify(
            title=row["title"], body=row["body"], notify_type=notify_type
        ):
            raise RuntimeError("Apprise did not confirm successful delivery.")

    def _finish_delivery(self, row, success, error):
        now = time.time()
        attempts = int(row["attempts"] or 0) + 1
        config = _notification_config()
        max_attempts = max(1, int(config.get("max_attempts", 3) or 3))
        if success:
            status = "sent"
            next_attempt = now
            sent_at = now
        elif attempts >= max_attempts:
            status = "failed"
            next_attempt = now
            sent_at = None
        else:
            status = "retrying"
            base = max(1, int(config.get("retry_base_sec", 30) or 30))
            next_attempt = now + (base * (2 ** (attempts - 1)))
            sent_at = None
        with self._db_lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE deliveries SET status = ?, attempts = ?, next_attempt_at = ?,
                    error = ?, updated_at = ?, sent_at = ? WHERE id = ?
                """,
                (status, attempts, next_attempt, error, now, sent_at, row["id"]),
            )
        if success:
            self._log(
                "info",
                "Notification delivered to %s for %s.",
                row["destination_name"],
                row["event_type"],
            )
        elif status == "failed":
            self._log(
                "error",
                "Notification delivery to %s failed after %s attempt(s): %s",
                row["destination_name"],
                attempts,
                error,
            )

    def _monitor_loop(self):
        while not self._stop_event.is_set():
            config = _notification_config()
            interval = max(15, int(config.get("monitor_interval_sec", 30) or 30))
            if config.get("enabled") and self._ensure_storage_ready():
                try:
                    self._collect_monitored_conditions(config)
                except Exception as error:
                    if not self._handle_runtime_storage_error(error):
                        self._log(
                            "error",
                            "Notification monitor failed: %s",
                            _safe_error(error),
                        )
                if time.time() - self._last_prune > 3600:
                    self._prune_history(config)
            self._stop_event.wait(interval)

    def _collect_monitored_conditions(self, config):
        snapshot = self.metrics_collector.snapshot(
            external_limit=0, database_details=True, database_refresh=True
        )
        system = snapshot.get("system", {})
        thresholds = config.get("thresholds", {})
        duration = max(0, int(thresholds.get("duration_sec", 60) or 0))
        resources = (
            ("cpu", system.get("cpu_percent"), thresholds.get("cpu_percent"), "%"),
            (
                "memory",
                system.get("mem", {}).get("percent"),
                thresholds.get("memory_percent"),
                "%",
            ),
            (
                "disk",
                system.get("disk", {}).get("percent"),
                thresholds.get("disk_percent"),
                "%",
            ),
            (
                "inode",
                system.get("inode", {}).get("percent"),
                thresholds.get("inode_percent"),
                "%",
            ),
        )
        for name, value, threshold, suffix in resources:
            if value is None or threshold is None:
                continue
            self._condition(
                f"resource:{name}",
                float(value) >= float(threshold),
                duration,
                f"resource.{name}.high",
                "warning",
                f"DUMB {name.title()} pressure is high",
                f"{name.title()} usage is {float(value):.1f}{suffix}; configured threshold is {float(threshold):.1f}{suffix}.",
                value=float(value),
            )

        database_rank = {
            "observing": 0,
            "healthy": 0,
            "moderate": 1,
            "high": 2,
            "critical": 3,
        }
        minimum = database_rank.get(str(thresholds.get("database_pressure", "high")), 2)
        services = snapshot.get("database_health", {}).get("services", [])
        current_keys = set()
        for service in services:
            if not isinstance(service, dict) or not service.get("monitoring_enabled"):
                continue
            name = (
                service.get("process_name") or service.get("service_key") or "Database"
            )
            key = f"database:{name}"
            current_keys.update((key, f"{key}:collection"))
            pressure = str(service.get("pressure") or "observing")
            collection_error = next(
                (
                    database.get("probe_error")
                    for database in service.get("databases", [])
                    if isinstance(database, dict) and database.get("probe_error")
                ),
                None,
            )
            if collection_error:
                self._condition(
                    f"{key}:collection",
                    True,
                    duration,
                    "database.collection.failed",
                    "warning",
                    f"Database health collection failed for {name}",
                    "DUMB could not collect database health telemetry. Review the service and DUMB logs for the redacted failure details.",
                    service_name=name,
                )
            else:
                self._condition(
                    f"{key}:collection",
                    False,
                    duration,
                    "database.collection.failed",
                    "warning",
                    f"Database health collection failed for {name}",
                    "Database health collection recovered.",
                    service_name=name,
                )
            self._condition(
                key,
                database_rank.get(pressure, 0) >= minimum,
                duration,
                "database.pressure",
                "critical" if pressure == "critical" else "warning",
                f"Database pressure is {pressure} for {name}",
                str(
                    service.get("recommendation")
                    or "Review Database Health details in DUMB Metrics."
                ),
                service_name=name,
                value=pressure,
            )
        for key in list(self._conditions):
            if key.startswith("database:") and key not in current_keys:
                self._conditions.pop(key, None)

    def _condition(
        self,
        key,
        active,
        duration,
        event_type,
        severity,
        title,
        body,
        service_name=None,
        value=None,
    ):
        now = time.time()
        state = self._conditions.setdefault(key, {"first_seen": None, "active": False})
        if active:
            state["first_seen"] = state.get("first_seen") or now
            if not state.get("active") and now - state["first_seen"] >= duration:
                state["active"] = True
                self.emit(
                    event_type,
                    severity,
                    title,
                    body,
                    service_name=service_name,
                    metadata={"condition_key": key, "value": value},
                )
            return
        was_active = state.get("active")
        state["first_seen"] = None
        state["active"] = False
        if was_active:
            self.emit(
                "recovery",
                "success",
                f"Recovered: {title}",
                f"The condition has returned below its configured threshold. Last observed value: {value}.",
                service_name=service_name,
                metadata={"condition_key": key, "recovered_event_type": event_type},
            )

    def _prune_history(self, config):
        retention_days = max(1, int(config.get("history_retention_days", 30) or 30))
        cutoff = time.time() - (retention_days * 86400)
        with self._db_lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM deliveries WHERE created_at < ? AND status NOT IN ('queued', 'retrying')",
                (cutoff,),
            )
        self._last_prune = time.time()


def get_notification_manager():
    try:
        from utils.dependencies import get_notification_manager as dependency_getter

        return dependency_getter()
    except (ImportError, KeyError, RuntimeError):
        return None


def notify_event(
    event_type,
    severity,
    title,
    body,
    service_name=None,
    metadata=None,
):
    manager = get_notification_manager()
    if not manager:
        return []
    try:
        return manager.emit(
            event_type,
            severity,
            title,
            body,
            service_name=service_name,
            metadata=metadata,
        )
    except Exception as error:
        if not manager._handle_runtime_storage_error(error):
            manager._log(
                "error",
                "Notification event %s could not be queued without affecting the "
                "originating DUMB operation: %s",
                event_type,
                _safe_error(error),
            )
        return []
