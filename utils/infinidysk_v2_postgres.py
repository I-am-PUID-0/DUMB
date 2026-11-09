"""DUMB-owned admission and recovery around InfiniDysk's native DB engine."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import threading
import time
import tomllib
from urllib.parse import quote
import uuid

import psycopg2

from utils.private_files import atomic_write_private_text


class NativePostgresError(ValueError):
    pass


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
_HOST = re.compile(r"[A-Za-z0-9_.:-]+")
_ACTIVE = {
    "starting",
    "copying",
    "syncing",
    "cutover",
    "rolling_back",
    "recovery_required",
}
_PUBLIC_JOB = {
    "job_id",
    "status",
    "stage",
    "message",
    "created_at",
    "updated_at",
    "completed_at",
    "recovery_required",
    "can_rollback",
    "progress",
    "native",
    "snapshot_at",
    "target_summary",
    "protocol",
    "operation",
    "cleanup_required",
}


def _request(value):
    if not isinstance(value, dict):
        raise NativePostgresError("Supply a JSON object for this operation.")
    return value


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def _cleanup_warnings(value):
    if not value:
        return []
    warnings = value if isinstance(value, list) else [value]
    result = []
    for warning in warnings[:8]:
        message = (
            warning
            if isinstance(warning, str)
            else json.dumps(warning, sort_keys=True, default=str)
        )
        message = re.sub(
            r"(?:https?|postgres(?:ql)?):\/\/[^\s<>\"']+",
            "[private URL omitted]",
            message,
            flags=re.IGNORECASE,
        )
        message = re.sub(
            r"\b(password|token|secret|authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+",
            r"\1=[redacted]",
            message,
            flags=re.IGNORECASE,
        )
        result.append(message[:1024])
    return result


def _require_collation(identity):
    if identity.get("collation") != "C":
        raise NativePostgresError(
            "Use a dedicated PostgreSQL database with C collation to preserve SQLite ordering."
        )


def _private_directory(path):
    path = Path(path)
    if not path.is_absolute():
        raise NativePostgresError(
            "Migration state needs an absolute private directory."
        )
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise NativePostgresError("Migration state cannot use directory symlinks.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if info.st_uid != os.geteuid():
        raise NativePostgresError("Migration state is not owned by the controller.")
    path.chmod(0o700)
    return path


def _read_private(path):
    path = Path(path)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise NativePostgresError("Migration evidence is not a private regular file.")
    return json.loads(path.read_text())


def _target(options, managed):
    options = _request(options)
    values = _request(options["target"]) if "target" in options else options
    for container in (options, values):
        if "use_managed_postgres" in container and not isinstance(
            container["use_managed_postgres"], bool
        ):
            raise NativePostgresError("Choose a boolean managed PostgreSQL option.")
    if (
        options.get("use_managed_postgres") is True
        or values.get("use_managed_postgres") is True
    ):
        values = {**managed, "database": values.get("database") or "infinidysk_v2"}
    for field in (
        "host",
        "database",
        "user",
        "password",
        "sslmode",
        "sslrootcert",
        "sslcert",
        "sslkey",
    ):
        if field in values and not isinstance(values[field], str):
            raise NativePostgresError("PostgreSQL connection fields must be strings.")
    if isinstance(values.get("port"), bool):
        raise NativePostgresError("Invalid PostgreSQL port.")
    host = str(values.get("host") or "127.0.0.1")
    database = str(values.get("database") or "")
    user = str(values.get("user") or "")
    try:
        port = int(values.get("port") or 5432)
    except (TypeError, ValueError):
        raise NativePostgresError("Invalid PostgreSQL port.") from None
    if (
        not _HOST.fullmatch(host)
        or not _NAME.fullmatch(database)
        or not user
        or not 1 <= port <= 65535
    ):
        raise NativePostgresError(
            "Supply a TCP PostgreSQL host, port, database and user."
        )
    sslmode = str(
        values.get("sslmode")
        or ("prefer" if host in {"127.0.0.1", "localhost", "::1"} else "verify-full")
    )
    if sslmode not in {"disable", "prefer", "require", "verify-ca", "verify-full"}:
        raise NativePostgresError("Unsupported PostgreSQL TLS mode.")
    if host not in {"127.0.0.1", "localhost", "::1"} and sslmode in {
        "disable",
        "prefer",
    }:
        raise NativePostgresError(
            "Remote PostgreSQL targets require verified TLS or explicit require mode."
        )
    result = {
        "host": host,
        "port": port,
        "dbname": database,
        "user": user,
        "password": str(values.get("password") or ""),
        "sslmode": sslmode,
    }
    for key in ("sslrootcert", "sslcert", "sslkey"):
        if values.get(key):
            result[key] = str(values[key])
    return result


def _target_url(target):
    host = target["host"]
    if ":" in host:
        host = f"[{host}]"
    query = "&".join(
        f"{quote(key)}={quote(str(target[key]), safe='')}"
        for key in ("sslmode", "sslrootcert", "sslcert", "sslkey")
        if key in target
    )
    return (
        f"postgres://{quote(target['user'], safe='')}:{quote(target['password'], safe='')}"
        f"@{host}:{target['port']}/{quote(target['dbname'], safe='')}?{query}"
    )


def _inspect_target(target, *, require_empty=False):
    """Bind destructive native operations to the actual cluster and database."""
    connection = psycopg2.connect(
        **target, connect_timeout=5, application_name="dumb-infinidysk-v2-migration"
    )
    try:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT system_identifier::text FROM pg_control_system()")
            system_id = cursor.fetchone()[0]
            cursor.execute(
                "SELECT oid::text, datcollate FROM pg_database WHERE datname=current_database()"
            )
            database_id, collation = cursor.fetchone()
            cursor.execute("SELECT current_schema(), current_user")
            schema, owner = cursor.fetchone()
            cursor.execute(
                "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.relkind IN ('r','p','v','m','f') AND n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND n.nspname NOT LIKE 'pg_toast%' ORDER BY 1,2"
            )
            tables = cursor.fetchall()
    finally:
        connection.close()
    if schema != "public":
        raise NativePostgresError(
            "Use the dedicated public schema; custom search paths require separate review."
        )
    if require_empty and tables:
        raise NativePostgresError("The destination must be a new empty database.")
    return {
        "system_identifier": str(system_id),
        "database_oid": str(database_id),
        "schema": schema,
        "owner": owner,
        "collation": collation,
        "relations": [list(row) for row in tables],
    }


def inspect_target(target, *, require_empty=False):
    try:
        return _inspect_target(target, require_empty=require_empty)
    except psycopg2.Error:
        raise NativePostgresError(
            "The dedicated PostgreSQL destination could not be verified. Check that it exists and the configured user can inspect its identity."
        ) from None


class InfiniDyskV2PostgresManager:
    def __init__(
        self,
        *,
        config_manager=None,
        process_handler=None,
        state_root="/config/migrations/infinidysk-v2-postgres",
        native_request=None,
        target_inspector=inspect_target,
        delivery_checker=None,
        clock=time.time,
    ):
        self.config_manager = config_manager
        self.process_handler = process_handler
        self.state_root = Path(state_root)
        self.native_request = native_request
        self.target_inspector = target_inspector
        self.delivery_checker = delivery_checker
        self.clock = clock
        self._lock = threading.RLock()
        self._previews = {}

    def configure(self, process_handler=None, config_manager=None):
        if process_handler is not None:
            self.process_handler = process_handler
        if config_manager is not None:
            self.config_manager = config_manager
        return self

    def _config(self):
        if self.config_manager is None:
            from utils.config_loader import CONFIG_MANAGER

            self.config_manager = CONFIG_MANAGER
        return self.config_manager

    def _service(self):
        return copy.deepcopy(self._config().get("infinidysk") or {})

    def _data(self):
        from utils.infinidysk_v2_runtime import resolve_v2_data_dir

        return Path(
            resolve_v2_data_dir(
                self._service(), self._config().get("data_root") or "/data"
            )
        )

    def _native(self, method="GET", path="/api/v1/database", payload=None):
        callback = self.native_request
        if callback is None:
            from utils.infinidysk_v2_runtime import native_api_request

            callback = native_api_request
        response = callback(
            self._service(),
            method,
            path,
            payload,
            data_root=self._config().get("data_root") or "/data",
        )
        if isinstance(response, dict) and "status" in response:
            if not 200 <= response["status"] < 300:
                raise NativePostgresError(
                    "The native database API rejected the operation."
                )
            return response.get("body") or {}
        return response or {}

    def _job(self):
        path = self.state_root / "job.json"
        if self.state_root.exists() or self.state_root.is_symlink():
            _private_directory(self.state_root)
        return _read_private(path) if path.exists() or path.is_symlink() else None

    def _save(self, job):
        _private_directory(self.state_root)
        job["updated_at"] = int(self.clock())
        atomic_write_private_text(
            self.state_root / "job.json", json.dumps(job, sort_keys=True)
        )
        _sync_directory(self.state_root)

    @staticmethod
    def _public(job):
        return (
            {
                key: copy.deepcopy(value)
                for key, value in job.items()
                if key in _PUBLIC_JOB
            }
            if job
            else None
        )

    def has_blocking_job(self):
        # Admission holds the shared lock. Do not wait for this manager's I/O
        # lock: a cutover may be consulting media protection in the other order.
        try:
            job = self._job()
            if job and job.get("status") in _ACTIVE:
                return True
            from utils.infinidysk_v2_support import generation

            return generation(self._service()) == "2" and bool(
                self._boot().get("migration")
            )
        except (OSError, ValueError):
            return True

    def has_recovery_pending_job(self):
        job = self._job()
        return bool(job and job.get("recovery_required"))

    def resume_runtime_allowed(self, service_key, service=None):
        """Permit configure-only cold boot of exactly a retained native job.

        This does not authorize upgrades, provider edits or recovery with a
        changed target. The native application resumes its own durable journal.
        """
        try:
            job = self._job()
            if not job or job.get("status") not in {
                "starting",
                "copying",
                "syncing",
                "cutover",
            }:
                return False
            if job.get("service_fingerprint") != _fingerprint(self._service()):
                return False
            boot = self._boot()
            transfer = boot.get("migration") or {}
            if not (
                boot.get("url") == job.get("target_url_private")
                or (
                    transfer.get("target") == job.get("target_url_private")
                    and transfer.get("state") in {"copying", "syncing", "cutover"}
                )
            ):
                return False
            if service_key == "infinidysk":
                self._binding(job)
                return (
                    service is None
                    or _fingerprint(service) == job["service_fingerprint"]
                )
            if service_key == "postgres":
                return (
                    job.get("managed_postgres_fingerprint") is not None
                    and _fingerprint(service or self._config().get("postgres"))
                    == job["managed_postgres_fingerprint"]
                )
            if service_key == "rclone":
                fingerprints = job.get("rclone_fingerprints") or {}
                return bool(service and _fingerprint(service) in fingerprints.values())
        except (OSError, ValueError):
            return False
        return False

    def _check_service(self):
        from utils.infinidysk_v2_support import generation

        if generation(self._service()) != "2":
            raise NativePostgresError(
                "The native database tool requires an installed InfiniDysk 2.x runtime."
            )

    def _binding(self, job, *, empty=False):
        if _fingerprint(self._service()) != job["service_fingerprint"]:
            raise NativePostgresError(
                "InfiniDysk configuration changed; retained migration identity needs review."
            )
        actual = self.target_inspector(job["target_private"], require_empty=empty)
        _require_collation(actual)
        for key in ("system_identifier", "database_oid", "schema", "owner"):
            if actual.get(key) != job["target_identity"].get(key):
                raise NativePostgresError(
                    "The PostgreSQL target identity changed. No cleanup was attempted."
                )
        return actual

    def _boot(self):
        path = self._data() / "database.toml"
        if path.is_symlink():
            raise NativePostgresError(
                "Native provider configuration cannot be a redirected file."
            )
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
                raise NativePostgresError(
                    "Native provider configuration is not a bounded regular file."
                )
            with os.fdopen(descriptor, "rb") as source:
                descriptor = -1
                content = source.read(64 * 1024 + 1)
            if len(content) > 64 * 1024:
                raise NativePostgresError(
                    "Native provider configuration exceeds its bounded size."
                )
            return tomllib.loads(content.decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError):
            raise NativePostgresError(
                "Native provider configuration is invalid; inspect it privately before continuing."
            ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _delivery_ready(self):
        instances = (self._config().get("rclone") or {}).get("instances") or {}
        checker = self.delivery_checker
        if checker is None:
            from utils.infinidysk_v2_topology import delivery_mount_ready

            checker = delivery_mount_ready
        return checker(self._service(), instances)

    def _complete_protection(self, job, *, success):
        token = job.get("protection_token_private")
        manager = getattr(self.process_handler, "media_protection_manager", None)
        if token and manager:
            manager.complete_planned(token, success=success)
            job.pop("protection_token_private", None)

    def _reconcile(self, job, native):
        if not job:
            return None
        migration = native.get("migration") or {}
        backend = str(native.get("backend") or "").lower()
        if job["status"] in {"starting", "copying", "syncing"}:
            state = migration.get("state")
            if state in {"copying", "syncing"}:
                job.update(
                    status=state,
                    stage=migration.get("stage") or state,
                    progress={
                        key: migration.get(key)
                        for key in ("done", "total", "table", "pending_changes")
                    },
                    message=(
                        "Native copy is synchronizing."
                        if state == "syncing"
                        else "Native copy is running."
                    ),
                )
            elif not migration:
                job.update(
                    status="recovery_required",
                    recovery_required=True,
                    message="Native transfer disappeared; inspect retained evidence before continuing.",
                )
            if migration.get("error"):
                job["message"] = (
                    "The native transfer reported an error; it retains its source and retry state."
                )
        elif (
            job["status"] == "cutover"
            and backend == "sqlite"
            and migration.get("state") in {"copying", "syncing"}
        ):
            job.update(
                status=migration["state"],
                stage=migration["state"],
                message="Native cutover retained SQLite and resumed synchronization; inspect the native error before retrying.",
            )
            self._complete_protection(job, success=False)
        elif (
            job["status"] == "cutover"
            and backend in {"postgres", "postgresql"}
            and not migration
        ):
            self._binding(job)
            if self._boot().get("url") != job["target_url_private"]:
                raise NativePostgresError(
                    "Native PostgreSQL selection does not match the authorized destination."
                )
            generation = self._native(path="/api/v1/database/cutover")
            if generation.get("started_ms") == job.get("native_started_ms"):
                return job
            health = self._native(path="/healthz")
            if health.get("status") not in {"ok", "healthy", "ready"}:
                return job
            if not self._delivery_ready():
                return job
            self._complete_protection(job, success=True)
            job.update(
                status="completed",
                stage="completed",
                completed_at=int(self.clock()),
                recovery_required=False,
                can_rollback=True,
                message="Native PostgreSQL cutover verified. SQLite rollback retains the switch-time snapshot.",
            )
        job["native"] = {key: native.get(key) for key in ("backend", "env_managed")}
        self._save(job)
        return job

    def status(self):
        with self._lock:
            self._check_service()
            job = self._job()
            blockers = []
            try:
                native = self._native()
                job = self._reconcile(job, native)
            except (OSError, ValueError):
                native = {}
                blockers.append(
                    "The native database state is temporarily unavailable; no mutation is admitted."
                )
            external = bool(
                native.get("migration") and not (job and job.get("status") in _ACTIVE)
            )
            if external:
                blockers.append(
                    "A native migration was started outside DUMB; inspect it in InfiniDysk before admitting another job."
                )
            if native.get("env_managed"):
                blockers.append(
                    "The database is environment-managed; native provider moves are disabled."
                )
            ready = bool(native and not blockers)
            active = bool(job and job.get("status") in _ACTIVE)
            actions = {
                "start": ready
                and not active
                and native.get("backend") == "sqlite"
                and not native.get("migration"),
                "cutover": ready
                and bool(
                    job
                    and job.get("status") == "syncing"
                    and not (native.get("migration") or {}).get("error")
                ),
                "cancel": ready
                and bool(
                    job
                    and job.get("status")
                    in {"starting", "copying", "syncing", "recovery_required"}
                    and native.get("backend") == "sqlite"
                    and (native.get("migration") or {}).get("state") != "cutover"
                ),
                "rollback": bool(
                    job
                    and (
                        (job.get("status") == "completed" and job.get("can_rollback"))
                        or (
                            job.get("status") == "rolling_back"
                            and job.get("rollback_requested")
                        )
                    )
                ),
            }
            return {
                "protocol": 1,
                "provider": native.get("backend"),
                "job": self._public(job),
                "ready": ready,
                "blockers": blockers,
                "actions": actions,
                "confirmation_texts": {
                    "cutover": "SWITCH TO POSTGRESQL",
                    "cancel": "EMPTY MIGRATION TARGET",
                    "rollback": "RESTORE SQLITE SNAPSHOT",
                },
            }

    def preview(self, options):
        with self._lock:
            _request(options)
            self._check_service()
            status = self.status()
            if not status["actions"]["start"]:
                raise NativePostgresError(
                    "A native provider move is not currently available."
                )
            target = _target(options, self._config().get("postgres") or {})
            identity = self.target_inspector(target, require_empty=True)
            _require_collation(identity)
            url = _target_url(target)
            check = self._native("POST", "/api/v1/database/check", {"url": url})
            if check.get("error"):
                raise NativePostgresError(
                    "The native target check rejected this destination."
                )
            token = secrets.token_urlsafe(32)
            self._previews = {
                key: value
                for key, value in self._previews.items()
                if value["expires_at"] > self.clock()
            }
            record = {
                "target_private": target,
                "target_identity": identity,
                "target_url_private": url,
                "service_fingerprint": _fingerprint(self._service()),
                "expires_at": self.clock() + 300,
            }
            self._previews[token] = record
            return {
                "protocol": 1,
                "ready": True,
                "preview_token": token,
                "expires_at": int(record["expires_at"]),
                "confirmation_text": "COPY TO NEW POSTGRESQL DATABASE",
                "current": {"provider": "sqlite"},
                "target": {
                    "provider": "postgres",
                    "host": target["host"],
                    "port": target["port"],
                    "database": target["dbname"],
                },
                "target_identity": {
                    key: identity[key]
                    for key in ("system_identifier", "database_oid", "schema")
                },
                "checks": [{"label": "Dedicated empty destination", "ok": True}],
                "blockers": [],
                "warnings": check.get("warnings") or [],
                "required_acknowledgements": ["backup", "destructive_target"],
            }

    def _ack(self, request, text):
        request = _request(request)
        if (
            request.get("confirmation") != text
            or request.get("acknowledge_backup") is not True
        ):
            raise NativePostgresError(
                "Confirm the operation and independent backup before continuing."
            )
        if request.get("acknowledge_destructive_target") is not True:
            raise NativePostgresError(
                "Acknowledge that native cancellation can empty the dedicated target."
            )

    def _snapshot(self, job):
        directory = _private_directory(self.state_root / job["job_id"])
        source = self._data() / "infinidysk.sqlite"
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise NativePostgresError(
                "The active SQLite source is not a direct regular file."
            )
        snapshot = directory / "before-native-copy.sqlite"
        with sqlite3.connect(
            f"file:{quote(str(source))}?mode=ro", uri=True
        ) as original:
            with sqlite3.connect(snapshot) as backup:
                original.backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise NativePostgresError("The SQLite backup did not validate.")
        snapshot.chmod(0o600)
        job["snapshot_private"] = str(snapshot)
        job["snapshot_sha256"] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        job["snapshot_at"] = int(self.clock())

    def start(self, request):
        with self._lock:
            self._ack(request, "COPY TO NEW POSTGRESQL DATABASE")
            record = self._previews.pop(str(request.get("preview_token") or ""), None)
            if not record or record["expires_at"] < self.clock():
                raise NativePostgresError("Preview expired; inspect the target again.")
            if not self.status()["actions"]["start"]:
                raise NativePostgresError(
                    "The native provider state changed after preview."
                )
            job = {
                **record,
                "job_id": uuid.uuid4().hex,
                "protocol": 1,
                "operation": "infinidysk_v2_postgres",
                "status": "starting",
                "stage": "backup",
                "created_at": int(self.clock()),
                "recovery_required": False,
                "can_rollback": False,
                "target_summary": {
                    key: record["target_private"][key]
                    for key in ("host", "port", "dbname")
                },
            }
            managed = self._config().get("postgres") or {}
            target = record["target_private"]
            instances = (self._config().get("rclone") or {}).get("instances") or {}
            from utils.infinidysk_v2_topology import associated_rclone

            job["rclone_fingerprints"] = {
                key: _fingerprint(value)
                for key, value in instances.items()
                if associated_rclone(value)
            }
            if (
                str(managed.get("host") or "127.0.0.1") == target["host"]
                and int(managed.get("port") or 5432) == target["port"]
            ):
                job["managed_postgres_fingerprint"] = _fingerprint(managed)
            self._binding(job, empty=True)
            self._snapshot(job)
            self._save(job)  # Recovery exists before native target mutation.
            try:
                self._native(
                    "POST",
                    "/api/v1/database/migration",
                    {"url": job["target_url_private"]},
                )
            except (OSError, ValueError):
                job.update(
                    status="recovery_required",
                    recovery_required=True,
                    message="Native admission could not be confirmed; inspect it before retrying.",
                )
                self._save(job)
                raise NativePostgresError(job["message"]) from None
            job.update(
                status="copying",
                stage="copying",
                message="Native online copy admitted; SQLite remains active.",
            )
            self._save(job)
            return {"job": self._public(job)}

    def _action_job(self, request, action):
        request = _request(request)
        status = self.status()
        job = self._job()
        if (
            not job
            or request.get("job_id") != job["job_id"]
            or not status["actions"].get(action)
        ):
            raise NativePostgresError(
                "This operation does not match the current native migration job."
            )
        self._ack(request, status["confirmation_texts"][action])
        if action != "rollback":
            self._binding(job)
        elif _fingerprint(self._service()) != job["service_fingerprint"]:
            raise NativePostgresError(
                "InfiniDysk configuration changed; retained rollback identity needs review."
            )
        return job

    def cutover(self, request):
        with self._lock:
            job = self._action_job(request, "cutover")
            manager = getattr(self.process_handler, "media_protection_manager", None)
            protection = (
                manager.begin_planned(
                    self._service().get("process_name", "InfiniDysk"),
                    "native_postgres",
                    "safe",
                )
                if manager
                else None
            )
            if protection and protection.get("status") == "deferred":
                raise NativePostgresError(
                    "Library protection deferred PostgreSQL cutover."
                )
            if protection and protection.get("token"):
                job["protection_token_private"] = protection["token"]
            try:
                job.update(
                    status="cutover",
                    stage="cutover",
                    message="Native cutover is draining and restarting the application.",
                )
                self._save(job)
                result = self._native("POST", "/api/v1/database/cutover")
                job["native_started_ms"] = result.get("started_ms")
                self._save(job)
            except (OSError, ValueError):
                # A lost response may follow an accepted cutover. Keep both the
                # job and library protection until observed native recovery.
                raise NativePostgresError(
                    "Native cutover acknowledgement is unavailable; retained protection and status polling will observe recovery."
                ) from None
            return {"job": self._public(job)}

    def cancel(self, request):
        with self._lock:
            job = self._action_job(request, "cancel")
            native = self._native()
            result = (
                self._native("DELETE", "/api/v1/database/migration")
                if native.get("migration")
                else {"warnings": ["Retained target cleanup requires review."]}
            )
            after = self._native()
            if after.get("backend") != "sqlite" or after.get("migration"):
                raise NativePostgresError(
                    "Cancellation is not yet confirmed; retained recovery remains active."
                )
            warnings = _cleanup_warnings(result.get("warnings"))
            if warnings:
                directory = _private_directory(self.state_root / job["job_id"])
                evidence = directory / "cleanup-warnings.json"
                if evidence.exists() or evidence.is_symlink():
                    prior = _read_private(evidence)
                    if (
                        not isinstance(prior, dict)
                        or prior.get("job_id") != job["job_id"]
                    ):
                        raise NativePostgresError(
                            "Retained native cleanup evidence needs private review."
                        )
                else:
                    atomic_write_private_text(
                        evidence,
                        json.dumps(
                            {
                                "job_id": job["job_id"],
                                "recorded_at": int(self.clock()),
                                "target_summary": job["target_summary"],
                                "target_identity": {
                                    key: job["target_identity"].get(key)
                                    for key in (
                                        "system_identifier",
                                        "database_oid",
                                        "schema",
                                        "owner",
                                        "collation",
                                    )
                                },
                                "warnings": warnings,
                            },
                            sort_keys=True,
                        ),
                    )
                    _sync_directory(directory)
                job["cleanup_evidence_private"] = str(evidence)
            self._complete_protection(job, success=False)
            job.update(
                status="cancelled",
                stage="cancelled",
                recovery_required=False,
                cleanup_required=bool(warnings),
                cleanup_warnings_private=warnings,
                message=(
                    "Native copy cancelled."
                    if not warnings
                    else "Native cleanup needs review; SQLite remains active."
                ),
            )
            self._save(job)
            return {"job": self._public(job)}

    def rollback(self, request):
        with self._lock:
            job = self._action_job(request, "rollback")
            if not self.process_handler:
                raise NativePostgresError(
                    "The managed process controller is unavailable."
                )
            data = self._data()
            source = data / "infinidysk.sqlite.pre-postgres"
            destination = data / "infinidysk.sqlite"
            retained = Path(
                job.get("rollback_snapshot_private")
                or self.state_root / job["job_id"] / "switch-time.sqlite"
            )
            resumed = bool(job.get("rollback_requested"))
            boot_url = self._boot().get("url")
            if boot_url != job["target_url_private"] and not (resumed and not boot_url):
                raise NativePostgresError(
                    "The native switch-time snapshot or provider identity is missing."
                )
            if job.get("rollback_phase") == "sqlite_restored":
                return self._finish_rollback(job)
            snapshot_path = retained if resumed and retained.is_file() else source
            if (
                snapshot_path.is_symlink()
                or not snapshot_path.is_file()
                or snapshot_path.stat().st_nlink != 1
            ):
                raise NativePostgresError(
                    "The retained switch-time snapshot is not a direct regular file."
                )
            with sqlite3.connect(
                f"file:{quote(str(snapshot_path))}?mode=ro", uri=True
            ) as snapshot:
                if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise NativePostgresError(
                        "The native rollback snapshot failed integrity validation."
                    )
            private = _private_directory(self.state_root / job["job_id"])
            if not resumed:
                shutil.copyfile(source, retained)
                retained.chmod(0o600)
                job["rollback_snapshot_private"] = str(retained)
                job["rollback_snapshot_sha256"] = hashlib.sha256(
                    retained.read_bytes()
                ).hexdigest()
            elif hashlib.sha256(retained.read_bytes()).hexdigest() != job.get(
                "rollback_snapshot_sha256"
            ):
                raise NativePostgresError(
                    "The retained switch-time snapshot changed; no files were replaced."
                )
            manager = getattr(self.process_handler, "media_protection_manager", None)
            if not job.get("protection_token_private") and manager:
                protection = manager.begin_planned(
                    self._service().get("process_name", "InfiniDysk"),
                    "native_postgres_rollback",
                    "safe",
                )
                if protection.get("status") == "deferred":
                    raise NativePostgresError(
                        "Library protection deferred SQLite rollback."
                    )
                job["protection_token_private"] = protection.get("token")
            job.update(
                status="rolling_back",
                stage="rollback",
                recovery_required=True,
                rollback_requested=True,
                message="Restoring switch-time SQLite; later PostgreSQL writes stay on PostgreSQL.",
            )
            self._save(job)
            name = self._service().get("process_name", "InfiniDysk")
            self.process_handler.stop_process(name)
            if (
                self.process_handler._prefixed_name(name)
                in self.process_handler.process_names
            ):
                raise NativePostgresError(
                    "InfiniDysk did not stop; no database files were changed."
                )
            if destination.exists() or destination.is_symlink():
                if (
                    destination.is_symlink()
                    or hashlib.sha256(destination.read_bytes()).hexdigest()
                    != job["rollback_snapshot_sha256"]
                ):
                    raise NativePostgresError(
                        "An unexpected SQLite destination exists; recovery evidence was retained."
                    )
            boot = data / "database.toml"
            if boot.exists():
                backup_boot = private / "database.toml.before-rollback"
                if not backup_boot.exists():
                    shutil.copy2(boot, backup_boot)
                    backup_boot.chmod(0o600)
            if not destination.exists():
                if (
                    source.is_symlink()
                    or not source.is_file()
                    or hashlib.sha256(source.read_bytes()).hexdigest()
                    != job["rollback_snapshot_sha256"]
                ):
                    raise NativePostgresError(
                        "The switch-time source changed; no SQLite file was replaced."
                    )
                os.replace(source, destination)
            if boot.exists():
                boot.unlink()
            info = destination.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise NativePostgresError(
                    "Restored SQLite is not a direct regular file."
                )
            _sync_directory(data)
            # Record the restored identity before native SQLite accepts writes.
            # Recovery retries must not compare its changing bytes to a backup
            # or restore over writes made after the first successful restart.
            job.update(
                rollback_phase="sqlite_restored",
                rollback_sqlite_identity_private={
                    "device": info.st_dev,
                    "inode": info.st_ino,
                },
                stage="sqlite_recovery",
                message="SQLite was restored; verifying application and selected mount recovery.",
            )
            self._save(job)
            return self._finish_rollback(job)

    def _finish_rollback(self, job):
        destination = self._data() / "infinidysk.sqlite"
        info = destination.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or job.get("rollback_sqlite_identity_private")
            != {"device": info.st_dev, "inode": info.st_ino}
        ):
            raise NativePostgresError(
                "The restored SQLite identity changed; no files were replaced."
            )
        boot = self._boot()
        if boot.get("url") or boot.get("migration"):
            raise NativePostgresError(
                "The restored SQLite provider selection changed; inspect recovery evidence."
            )
        name = self._service().get("process_name", "InfiniDysk")
        if (
            self.process_handler._prefixed_name(name)
            not in self.process_handler.process_names
        ):
            success, _ = self.process_handler.start_process(name)
            if not success:
                raise NativePostgresError(
                    "SQLite was restored but application recovery needs review."
                )
        native = self._native()
        health = self._native(path="/healthz")
        if (
            native.get("backend") != "sqlite"
            or native.get("migration")
            or health.get("status") not in {"ok", "healthy", "ready"}
        ):
            raise NativePostgresError(
                "SQLite was restored but application recovery needs review."
            )
        # Startup may write to SQLite. Recheck identity, not snapshot contents.
        after = destination.lstat()
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise NativePostgresError(
                "SQLite changed identity during recovery; no files were replaced."
            )
        if not self._delivery_ready():
            job.update(
                stage="waiting_for_delivery",
                message="SQLite is active; waiting for selected mount recovery.",
            )
            self._save(job)
            raise NativePostgresError(
                "SQLite is active; waiting for selected mount recovery before completing rollback."
            )
        self._complete_protection(job, success=True)
        job.update(
            status="rolled_back",
            stage="rolled_back",
            recovery_required=False,
            can_rollback=False,
            message="Restored the retained SQLite snapshot; PostgreSQL was left untouched.",
        )
        self._save(job)
        return {"job": self._public(job)}


INFINIDYSK_V2_POSTGRES_MANAGER = InfiniDyskV2PostgresManager()
