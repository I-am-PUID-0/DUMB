"""Guarded, persisted InfiniDysk 1.x to 2.x jobs.

Native conversion runs only through the trusted execution adapter. Preview is
metadata inspection; only an accepted contained native import is a rehearsal.
The original v1 runtime/data remain the rollback source, never a reverse copy.
"""

from __future__ import annotations

from contextlib import closing
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
from urllib.parse import quote

from utils.config_loader import CONFIG_MANAGER
from utils.infinidysk_migration_admission import INFINIDYSK_MIGRATION_ADMISSION_LOCK
from utils.infinidysk_v2_inventory import (
    MAX_TREE_MANIFEST_BYTES,
    InventoryError,
    tree_fingerprint,
    tree_summary,
)
from utils.infinidysk_v2_proxy import (
    compatibility_auth_environment,
    compatibility_auth_policy,
)
from utils.infinidysk_v2_subprocess import migration_process_cleanup_complete
from utils.infinidysk_v2_support import V2_RELEASE, generation, v1_data_root
from utils.infinidysk_v2_topology import associated_rclone, rclone_mount_path

PROTOCOL = 1
JOB_TYPE = "infinidysk_v1_to_v2"
PREVIEW_TTL = 30 * 60
MINIMUM_SCHEMA = "20260824160000"
MAX_STATE_BYTES = 4 * 1024 * 1024
ACTIVE = {
    "queued",
    "running",
    "quiescing",
    "snapshotting",
    "rehearsing",
    "importing",
    "validating",
    "cutover",
    "rolling_back",
}
RECOVERY = {"interrupted", "recovery_required"}
ACKNOWLEDGEMENTS = ("backup", "downtime", "beta", "oidc_policy", "downstream")
_ID = re.compile(r"[0-9a-f]{32}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
_MANAGED_SOURCE_ALIASES = (Path("/infinidysk"), Path("/nzbdav"))


class InfiniDyskV2MigrationError(RuntimeError):
    """A refused or interrupted guarded major-version operation."""


class InfiniDyskV2Cancelled(InfiniDyskV2MigrationError):
    """Cooperative cancellation at an owned migration checkpoint."""


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _path(value):
    path = Path(str(value))
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise InfiniDyskV2MigrationError(
            "Use dedicated absolute paths without parent traversal."
        )
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise InfiniDyskV2MigrationError(
                "A migration path crosses a symbolic link."
            )
    return path.resolve()


def _source_path(value, data_root):
    """Permit only DUMB's expected root alias into its configured data root."""
    path = Path(str(value))
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise InfiniDyskV2MigrationError("The retained source path is invalid.")
    aliases = []
    for parent in (path, *path.parents):
        if not parent.is_symlink():
            continue
        if parent not in _MANAGED_SOURCE_ALIASES:
            raise InfiniDyskV2MigrationError(
                "The retained source crosses an unmanaged symbolic link."
            )
        expected = _path(Path(data_root) / parent.name)
        if parent.resolve() != expected:
            raise InfiniDyskV2MigrationError(
                "The managed source alias points outside its expected data root."
            )
        aliases.append(
            {
                "path": str(parent),
                "link": os.readlink(parent),
                "link_identity": _identity(parent),
                "target": str(expected),
                "target_identity": _identity(expected),
            }
        )
    return _path(path.resolve()), aliases


def _identity(path):
    try:
        value = Path(path).lstat()
    except FileNotFoundError:
        return None
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "mode": stat.S_IFMT(value.st_mode),
    }


def _source_summary(path, *, validate_links=False, role="Source"):
    try:
        return tree_summary(path, validate_links=validate_links, role=role)
    except InventoryError as error:
        raise InfiniDyskV2MigrationError(str(error)) from error


def _source_fingerprint(path, *, scratch_root, validate_links=False, role="Source"):
    try:
        return tree_fingerprint(
            path, scratch_root=scratch_root, validate_links=validate_links, role=role
        )
    except InventoryError as error:
        raise InfiniDyskV2MigrationError(str(error)) from error


def _unavailable_capacity():
    return {
        "status": "unavailable",
        "complete": False,
        "estimated": True,
        "reserved": False,
        "observed_at": int(time.time()),
        "source": {"entries": None, "logical_bytes": None, "root_count": None},
        "filesystems": [],
    }


def _format_capacity_bytes(value):
    amount = value
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.2f} {unit}"
        amount /= 1024


class InfiniDyskV2CapacityError(InfiniDyskV2MigrationError):
    """A complete capacity estimate that refuses copying but remains reviewable."""

    def __init__(self, capacity):
        self.capacity = capacity
        detail = "; ".join(
            f"Filesystem {row['id'].rsplit('_', 1)[-1]}: "
            f"{_format_capacity_bytes(row['required_bytes'])} required, "
            f"{_format_capacity_bytes(row['free_bytes'])} free, "
            f"{_format_capacity_bytes(row['shortfall_bytes'])} short"
            for row in capacity["filesystems"]
            if not row["sufficient"]
        )
        super().__init__(
            "Insufficient estimated free space for retained backups, candidate "
            f"data and native database headroom. {detail}. Source copying is blocked."
        )


def _capacity_check(requirements, *, source=None):
    """Complete read-only per-filesystem estimates; no storage is reserved."""
    grouped = {}
    for requirement in requirements:
        destination, required = requirement[:2]
        role = requirement[2] if len(requirement) > 2 else "retained_backup"
        parent = Path(destination)
        while not parent.exists():
            parent = parent.parent
        device = parent.stat().st_dev
        item = grouped.setdefault(device, {"path": parent, "required": 0, "roles": []})
        item["required"] += int(required)
        if role not in item["roles"]:
            item["roles"].append(role)
    filesystems = []
    for index, item in enumerate(grouped.values(), 1):
        margin = max(64 * 1024 * 1024, item["required"] // 10)
        required = item["required"] + margin
        free = shutil.disk_usage(item["path"]).free
        shortfall = max(0, required - free)
        filesystems.append(
            {
                "id": f"filesystem_{index}",
                "roles": item["roles"],
                "base_required_bytes": item["required"],
                "safety_margin_bytes": margin,
                "required_bytes": required,
                "free_bytes": free,
                "shortfall_bytes": shortfall,
                "sufficient": shortfall == 0,
            }
        )
    sufficient = all(row["sufficient"] for row in filesystems)
    capacity = {
        "status": "sufficient" if sufficient else "insufficient",
        "complete": True,
        "estimated": True,
        "reserved": False,
        "observed_at": int(time.time()),
        "source": source or {"entries": 0, "logical_bytes": 0, "root_count": 0},
        "filesystems": filesystems,
    }
    if not sufficient:
        raise InfiniDyskV2CapacityError(capacity)
    return capacity


class InfiniDyskV2MigrationManager:
    def __init__(
        self,
        process_handler=None,
        updater=None,
        config_manager=CONFIG_MANAGER,
        state_root="/config/migrations/infinidysk-v2",
        executor=None,
    ):
        self.process_handler = process_handler
        self.updater = updater
        self.config_manager = config_manager
        self.state_root = Path(state_root)
        self.executor = executor
        self._lock = threading.RLock()
        self._worker_id = secrets.token_hex(16)
        self._thread = None
        self._active_id = None
        self._rollback_active = False
        self._cancel_requested = set()
        self._cleanup = None

    def configure(
        self, *, process_handler=None, updater=None, config_manager=None, executor=None
    ):
        with self._lock:
            if process_handler is not None:
                self.process_handler = process_handler
            if updater is not None:
                self.updater = updater
            if config_manager is not None:
                self.config_manager = config_manager
            if executor is not None:
                self.executor = executor
        return self

    def _directory(self, path):
        path = _path(path)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        value = path.lstat()
        if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.geteuid():
            raise InfiniDyskV2MigrationError(
                "Migration state must be controller-owned."
            )
        os.chmod(path, 0o700)
        return path

    def _write(self, path, payload):
        with self._lock:
            path = Path(path)
            if (
                path.parent == self.state_root / "jobs"
                and payload.get("job_id") in self._cancel_requested
            ):
                # A report/recovery checkpoint may hold an older in-memory job.
                # Never erase a cancellation already accepted by the API.
                payload["cancel_requested"] = True
            self._directory(path.parent)
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            if len(raw) > MAX_STATE_BYTES:
                raise InfiniDyskV2MigrationError(
                    "Migration state exceeds its supported bound."
                )
            temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
            try:
                descriptor = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)

    def _read(self, path):
        try:
            value = path.lstat()
        except FileNotFoundError:
            return None
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_nlink != 1
            or value.st_uid != os.geteuid()
            or stat.S_IMODE(value.st_mode) & 0o077
            or value.st_size > MAX_STATE_BYTES
        ):
            raise InfiniDyskV2MigrationError(
                "Migration state identity or permissions are unsafe."
            )
        _path(path.parent)
        flags = os.O_RDONLY | os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (value.st_dev, value.st_ino):
                raise InfiniDyskV2MigrationError(
                    "Migration state changed while it was read."
                )
            raw = handle.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise InfiniDyskV2MigrationError(
                "Migration state exceeds its supported bound."
            )
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise InfiniDyskV2MigrationError("Migration state is invalid.")
        return result

    def _job_path(self, job_id):
        if not _ID.fullmatch(str(job_id or "")):
            raise InfiniDyskV2MigrationError("Invalid migration job identity.")
        return self.state_root / "jobs" / f"{job_id}.json"

    def _jobs(self):
        directory = self.state_root / "jobs"
        if not directory.exists():
            return []
        if directory.is_symlink():
            raise InfiniDyskV2MigrationError("Migration state crosses a symbolic link.")
        paths = sorted(directory.glob("*.json"))
        if len(paths) > 1000:
            raise InfiniDyskV2MigrationError(
                "Migration job inventory exceeds its bound."
            )
        return [job for path in paths if (job := self._read(path)) is not None]

    def _effective_status(self, job):
        if self._rollback_active and self._active_id == job.get("job_id"):
            return job.get("status")
        if job.get("status") in ACTIVE and (
            job.get("worker_id") != self._worker_id
            or self._active_id != job.get("job_id")
            or self._thread is None
            or not self._thread.is_alive()
        ):
            return "interrupted"
        return job.get("status")

    @staticmethod
    def _owned_process_cleanup_complete(job):
        return "owned_process_cleanup" not in job or migration_process_cleanup_complete(
            job["owned_process_cleanup"]
        )

    @staticmethod
    def _retain_owned_process_cleanup(job, error):
        if getattr(error, "cleanup_complete", None) is not False:
            return False
        evidence = getattr(error, "cleanup_evidence", None)
        group = evidence.get("process_group") if isinstance(evidence, dict) else None
        # Keep bounded cleanup evidence private, including any retained scratch
        # identity. Never publish it or infer termination from missing metadata.
        retained = {
            "process_group": group if type(group) is int and group > 0 else None,
            "evidence_incomplete": True,
        }
        if isinstance(evidence, dict):
            try:
                encoded = json.dumps(evidence, ensure_ascii=True, allow_nan=False)
                if len(encoded) <= 64 * 1024:
                    retained = json.loads(encoded)
            except (TypeError, ValueError, OverflowError):
                pass
        job["owned_process_cleanup"] = retained
        return True

    @staticmethod
    def _possibly_exposed(job):
        if "activation_exposed" in job:
            return job.get("activation_exposed") is True
        # Older jobs did not journal provider exposure separately. An activated
        # but unfinished target may already contain writes; do not infer v1-only
        # recovery from the absence of a marker introduced by newer code.
        return bool(
            job.get("mode") == "cutover"
            and job.get("activation_fingerprint")
            and job.get("status") in ACTIVE | RECOVERY | {"completed"}
        )

    def _public_job(self, job):
        if not job:
            return None
        state = self._effective_status(job)
        cancel_requested = bool(
            job.get("cancel_requested") or job.get("job_id") in self._cancel_requested
        )
        can_cancel = bool(
            state in ACTIVE
            and state != "rolling_back"
            and not self._rollback_active
            and not cancel_requested
        )
        rollback = bool(
            state in RECOVERY
            or (
                job.get("snapshot")
                and job.get("mode") == "cutover"
                and state == "completed"
            )
        )
        exposed = self._possibly_exposed(job)
        legacy_exposed = exposed and "activation_exposed" not in job
        if exposed:
            can_cancel = False
            rollback = bool(
                state == "completed"
                and (job.get("activation_committed") is True or legacy_exposed)
                and job.get("snapshot")
                and job.get("mode") == "cutover"
            )
        cleanup = self._cleanup_controller().public_state(job)
        if exposed and state != "completed":
            cleanup["can_cleanup"] = False
        if (job.get("plan", {}).get("options") or {}).get("reclaim_source_space"):
            cleanup["can_cleanup"] = False
        rollback = (
            rollback
            and self._owned_process_cleanup_complete(job)
            and not cleanup["rollback_retired"]
            and not self._cleanup_controller().has_blocking_job(job)
        )
        public = {
            key: copy.deepcopy(job.get(key))
            for key in (
                "job_id",
                "job_type",
                "protocol",
                "mode",
                "import_report",
                "source_storage",
                "stage",
                "message",
                "created_at",
                "updated_at",
                "plan_fingerprint",
                "result",
                "error",
                "import_progress",
                "preview_expires_at",
            )
        } | {
            "status": state,
            "state": state,
            "can_rollback": rollback,
            "can_cancel": can_cancel,
            "cancel_requested": cancel_requested,
            "cancel_confirmation_text": f"CANCEL INFINIDYSK V2 {job['job_id']}",
            "actions": {
                "rollback": rollback,
                "cancel": can_cancel,
                "cleanup": cleanup["can_cleanup"],
            },
            "rollback_confirmation_text": f"ROLLBACK INFINIDYSK V2 {job['job_id']}",
            "activation_exposed": exposed,
            "activation_committed": job.get("activation_committed") is True,
            "rollback_requires_discard_ack": exposed and rollback,
            **(self._consumer_recovery().public(job) if exposed else {}),
            **cleanup,
        }
        if (
            state == "completed"
            and (public.get("result") or {}).get("restart_required") is True
            and job.get("worker_id") != self._worker_id
            and getattr(self.process_handler, "startup_phase", None)
            in {"ready", "degraded"}
        ):
            # Preserve the recovery receipt; its old controller's restart request
            # is satisfied only after this new controller finishes normal startup.
            public["result"]["restart_required"] = False
            public["message"] = (
                "Generation 2 consumer recovery finished and normal DUMB startup completed. Review any retained consumer warnings below."
            )
        if legacy_exposed and state in ACTIVE | RECOVERY:
            public.update(
                recovery_kind="v2_activation",
                can_retry_consumers=False,
                can_finish_consumer_recovery=False,
                message="This older activated job has no write-preserving activation receipt. Retain both v1 and v2 data and review the private job before recovery; automatic v1 restoration is unavailable.",
            )
        public["actions"].update(
            retry_consumers=public.get("can_retry_consumers", False),
            finish_consumer_recovery=public.get("can_finish_consumer_recovery", False),
        )
        if "owned_process_cleanup" in job and rollback:
            public["message"] = (
                "Helper termination is confirmed. Review and run rollback "
                "to restore the retained v1 service."
            )
        return public

    def _consumer_recovery(self):
        from utils.infinidysk_v2_consumer_recovery import InfiniDyskV2ConsumerRecovery

        return InfiniDyskV2ConsumerRecovery(self)

    def retry_consumers(self, request):
        return self._consumer_recovery().start(request)

    def finish_consumer_recovery(self, request):
        return self._consumer_recovery().start(request, finish=True)

    def _cleanup_controller(self):
        with self._lock:
            if self._cleanup is None:
                from utils.infinidysk_v2_cleanup import InfiniDyskV2Cleanup

                self._cleanup = InfiniDyskV2Cleanup(self)
            return self._cleanup

    def cleanup_preview(self, request):
        job = self._read(self._job_path(request.get("job_id")))
        if ((job or {}).get("plan", {}).get("options") or {}).get(
            "reclaim_source_space"
        ):
            raise InfiniDyskV2MigrationError(
                "Local v1 data was reused. Keep the retained archive for recovery; original-tree retirement does not apply."
            )
        return self._cleanup_controller().preview(request)

    def cleanup(self, request):
        return self._cleanup_controller().start(request)

    def get_job(self, job_id):
        with self._lock:
            return {"job": self._public_job(self._read(self._job_path(job_id)))}

    def has_blocking_job(self):
        with self._lock:
            if self._cleanup is not None and self._cleanup.has_blocking_job():
                return True
            return any(
                self._effective_status(job) in ACTIVE | RECOVERY for job in self._jobs()
            )

    def has_recovery_pending_job(self):
        with self._lock:
            return any(self._effective_status(job) in RECOVERY for job in self._jobs())

    def _config(self):
        value = getattr(self.config_manager, "config", None)
        if not isinstance(value, dict):
            raise InfiniDyskV2MigrationError(
                "The managed configuration is unavailable."
            )
        return copy.deepcopy(value)

    def _process_identity(self, name):
        handler = self.process_handler
        if handler is None:
            return None
        entries = getattr(handler, "processes", {})
        if isinstance(entries, dict):
            for info in entries.values():
                if not isinstance(info, dict) or info.get("name") != name:
                    continue
                process = info.get("process_obj")
                if process is not None and process.poll() is None:
                    return {"pid": process.pid}
        return None

    def _inventory(self, config, service):
        mounts, consumers = [], []
        instances = (config.get("rclone") or {}).get("instances") or {}
        for name, item in instances.items():
            if not isinstance(item, dict) or not item.get("enabled"):
                continue
            if not associated_rclone(item):
                continue
            path = rclone_mount_path(item)
            if not path or not os.path.isabs(path):
                raise InfiniDyskV2MigrationError("An associated mount path is invalid.")
            mounts.append(
                {
                    "instance": name,
                    "path": path,
                    "backend": "rclone",
                    "process_name": item.get("process_name"),
                    "identity": _identity(path),
                    "mounted": os.path.ismount(path),
                }
            )
        # All enabled managed Arr/media/request producers are part of quiescence,
        # not just consumers inferred from an optional core_service field.
        keys = {
            "radarr",
            "sonarr",
            "lidarr",
            "whisparr",
            "readarr",
            "chaptarr",
            "prowlarr",
            "plex",
            "jellyfin",
            "emby",
            "seerr",
            "pulsarr",
            "neutarr",
            "profilarr",
            "riven_backend",
        }
        for key in keys:
            section = config.get(key) or {}
            items = section.get("instances") if isinstance(section, dict) else None
            items = items.items() if isinstance(items, dict) else [(None, section)]
            for instance, item in items:
                if not isinstance(item, dict) or not item.get("enabled"):
                    continue
                name = item.get("process_name")
                if not name:
                    raise InfiniDyskV2MigrationError(
                        "An enabled consumer has no managed identity."
                    )
                consumers.append(
                    {
                        "service_key": key,
                        "instance": instance,
                        "process_name": name,
                        "process_identity": self._process_identity(name),
                        "config_fingerprint": _digest(item),
                    }
                )
        return {
            "mounts": mounts,
            "consumers": sorted(consumers, key=lambda item: item["process_name"]),
            "symlinks": list(service.get("symlink_backup_roots") or []),
            "strm": [],
        }

    def _copy_capacity(self, plan, *, postgres_size=None, postgres_snapshot=None):
        from utils.backup_storage import (
            BackupStorageError,
            get_backup_storage,
            require_local_path,
        )

        try:
            require_local_path(self.state_root)
            require_local_path(plan["target"]["data_dir"])
        except BackupStorageError as error:
            raise InfiniDyskV2MigrationError(str(error)) from error
        sizes = {}
        inventories = []
        for key in ("data_dir", "runtime_dir"):
            source = Path(plan["source"][key])
            if str(source) not in sizes:
                summary = _source_summary(
                    source,
                    validate_links=str(source) == plan["source"]["data_dir"],
                    role=(
                        "Retained v1 data"
                        if key == "data_dir"
                        else "Retained v1 runtime"
                    ),
                )
                sizes[str(source)] = summary["bytes"]
                inventories.append({"role": key, **summary})
        data = Path(plan["source"]["data_dir"])
        db = data / "db.sqlite"
        sqlite_files = [
            data / ("db.sqlite" + suffix) for suffix in ("", "-wal", "-shm", "-journal")
        ]
        database_bytes = 0
        for item in sqlite_files:
            try:
                value = item.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(value.st_mode):
                raise InfiniDyskV2MigrationError(
                    "A source SQLite file or sidecar is not a direct regular file."
                )
            database_bytes += value.st_size
        if postgres_snapshot:
            database_bytes = Path(postgres_snapshot["staged_sqlite"]).stat().st_size
        elif plan["source"]["provider"] in {"postgres", "postgresql"}:
            if not isinstance(postgres_size, int) or postgres_size <= 0:
                raise InfiniDyskV2MigrationError(
                    "The PostgreSQL source size must be estimated before retaining backups."
                )
            database_bytes = postgres_size * 4
        candidate = sizes.get(str(data), 0) + database_bytes
        if plan.get("options", {}).get("reclaim_source_space") is True:
            candidate = database_bytes
        # Native import writes a new metadata/blob database while retaining
        # its legacy inputs. Compression/schema changes prevent an exact bound;
        # budget at least another source tree as well as database growth.
        headroom = max(64 * 1024 * 1024, database_bytes * 3, sizes.get(str(data), 0))
        requirements = [
            (
                self.state_root,
                sum(sizes.values()) + database_bytes + MAX_TREE_MANIFEST_BYTES,
                "retained_backup",
            ),
            (
                Path(plan["target"]["data_dir"]).parent,
                candidate + headroom,
                "candidate",
            ),
        ]
        selected = plan.get("backup_storage") or {}
        if selected.get("mode") == "cold_archive":
            try:
                identity = get_backup_storage(self.config_manager).bound_identity(
                    selected.get("destination_id")
                )
            except (BackupStorageError, OSError) as error:
                raise InfiniDyskV2MigrationError(
                    "The selected registered backup destination could not be verified."
                ) from error
            if identity["binding_fingerprint"] != selected.get("binding_fingerprint"):
                raise InfiniDyskV2MigrationError(
                    "The selected backup destination identity changed; preview again."
                )
            # Closed archives replace the full local retained-tree copy. The
            # candidate, validation DB and canonical manifest stay local.
            requirements[0] = (
                self.state_root,
                database_bytes * (2 if plan["source"]["provider"] == "sqlite" else 1)
                + MAX_TREE_MANIFEST_BYTES
                + MAX_STATE_BYTES,
                "local_verification",
            )
            entries = sum(item["entries"] for item in inventories)
            # No compression savings: allow bounded portable metadata/header
            # overhead as well as the complete logical source bytes.
            archive_bytes = (
                sum(sizes.values()) + entries * 16 * 1024 + MAX_TREE_MANIFEST_BYTES
            )
            if postgres_snapshot:
                archive_bytes += sum(
                    Path(postgres_snapshot[key]).stat().st_size
                    for key in ("dump_path", "staged_sqlite")
                )
            elif postgres_size is not None:
                archive_bytes += postgres_size * 6
            else:
                archive_bytes += database_bytes
            requirements.append(
                (Path(identity["artifact_root"]), archive_bytes, "cold_archive")
            )
        if postgres_size is not None and postgres_snapshot is None:
            import tempfile

            requirements.extend(
                [
                    (self.state_root, postgres_size * 6, "retained_backup"),
                    (tempfile.gettempdir(), postgres_size * 3, "postgres_temporary"),
                ]
            )
        return _capacity_check(
            requirements,
            source={
                "entries": sum(item["entries"] for item in inventories),
                "logical_bytes": sum(sizes.values()),
                "root_count": len(sizes),
            },
        )

    def _plan(self, options):
        from utils.backup_storage import BackupStorageError, paths_overlap

        def overlaps(first, second):
            try:
                return paths_overlap(first, second)
            except (BackupStorageError, OSError) as error:
                raise InfiniDyskV2MigrationError(str(error)) from error

        config = self._config()
        service = config.get("infinidysk") or {}
        if not isinstance(service, dict):
            raise InfiniDyskV2MigrationError("InfiniDysk configuration is invalid.")
        old = generation(service)
        data_root = (
            config.get("data_root")
            or (config.get("dumb") or {}).get("data_root")
            or "/data"
        )
        source_data, data_aliases = _source_path(v1_data_root(service), data_root)
        source_runtime, runtime_aliases = _source_path(
            service.get("config_dir") or source_data, data_root
        )
        aliases = {item["path"]: item for item in data_aliases + runtime_aliases}
        target_data = _path(
            service.get("v2_data_dir") or Path(data_root) / "infinidysk-v2"
        )
        target_runtime = _path(service.get("v2_runtime_dir") or "/opt/infinidysk-v2")
        inventory = self._inventory(config, service)
        mount_backend = str(
            options.get("mount_backend") or service.get("mount_backend") or "rclone"
        )
        mount_path = str(
            inventory["mounts"][0]["path"]
            if options.get("mode", "migrate") == "migrate"
            and len(inventory["mounts"]) == 1
            else service.get("mount_path") or "/mnt/debrid/infinidysk"
        )
        env = service.get("env") or {}
        provider = str(
            env.get("DATABASE_PROVIDER")
            or ("postgres" if service.get("postgres_enabled") else "sqlite")
        ).lower()
        mode = str(options.get("mode") or "migrate")
        reclaim = options.get("reclaim_source_space", False)
        if type(reclaim) is not bool:
            raise InfiniDyskV2MigrationError(
                "Local storage reuse must be an explicit boolean choice."
            )
        blockers = []
        if old != "1":
            blockers.append(
                "The service already selects generation 2; use its existing runtime or retained rollback."
            )
        if mode not in {"migrate", "fresh"}:
            blockers.append("Choose a fresh installation or a v1-to-v2 migration.")
        if mount_backend not in {"rclone", "fuse"}:
            blockers.append("Choose rclone or native FUSE.")
        if options.get("import_strategy", "preserve") != "preserve":
            blockers.append(
                "This protocol preserves existing outputs; automatic output conversion is unsupported."
            )
        for target in (target_data, target_runtime):
            for source in (source_data, source_runtime):
                if overlaps(target, source):
                    blockers.append(
                        "Generation 2 runtime/data must be separate from retained generation 1 paths."
                    )
        if overlaps(target_data, target_runtime):
            blockers.append("Keep generation 2 program files separate from its data.")
        if target_data.exists() and any(target_data.iterdir()):
            blockers.append(
                "The generation 2 data destination is not empty; inspect retained state before retrying."
            )
        has_data = (source_data / "db.sqlite").exists() or provider in {
            "postgres",
            "postgresql",
        }
        if mode == "fresh" and has_data:
            blockers.append(
                "Existing v1 data must use the guided migration; a fresh installation cannot bypass it."
            )
        if mode == "migrate" and not has_data:
            blockers.append("No managed v1 application database was found.")
        if (
            mount_backend == "rclone"
            and mode == "migrate"
            and len(inventory["mounts"]) != 1
        ):
            blockers.append(
                "Preserving rclone requires exactly one associated enabled managed mount."
            )
        if inventory["mounts"] and any(
            item["path"] != mount_path for item in inventory["mounts"]
        ):
            blockers.append(
                "The selected mount path differs from the existing consumer-visible mount."
            )
        if self.executor is None:
            blockers.append("The contained native migration executor is unavailable.")
        supported = getattr(self.executor, "supported_source_providers", ("sqlite",))
        if provider in {"postgres", "postgresql"} and "postgres" not in supported:
            blockers.append(
                "This executor has no verified PostgreSQL snapshot/import path."
            )
        if self.process_handler is None or self.updater is None:
            blockers.append("Managed process and update dependencies are unavailable.")
        if getattr(self.process_handler, "shutting_down", False):
            blockers.append("DUMB is shutting down.")
        for source in (source_data, source_runtime):
            private_root = _path(self.state_root)
            if overlaps(source, private_root):
                blockers.append(
                    "Private migration storage must be outside the retained source trees."
                )
        target_service = copy.deepcopy(service)
        target_service.update(
            runtime_generation="2",
            mount_backend=mount_backend,
            mount_path=mount_path,
            v2_runtime_dir=str(target_runtime),
            v2_data_dir=str(target_data),
            v2_release_version=V2_RELEASE,
            postgres_enabled=False,
            enabled=True if mode == "fresh" else bool(service.get("enabled")),
        )
        timezone = env.get("TZ") or os.environ.get("TZ")
        target_service["env"] = {
            key: value
            for key, value in (service.get("env") or {}).items()
            if key
            not in {
                "DATABASE_PROVIDER",
                "DATABASE_CONNECTION_STRING",
                "INFINIDYSK_DATABASE_URL",
            }
        }
        if timezone:
            target_service["env"]["TZ"] = timezone
        if mount_backend == "fuse" and mode == "migrate":
            instances = (config.get("rclone") or {}).get("instances") or {}
            if any(
                str(argument).lower() in {"--no-modtime", "--no-modtime=true"}
                for mount in inventory["mounts"]
                for argument in (
                    instances.get(mount["instance"], {}).get("command") or []
                )
            ):
                # Native defaults expose real dates. Preserve historical epoch
                # dates before restarting consumers that cached --no-modtime.
                target_service["env"]["INFINIDYSK__MOUNT__EPOCH_TIMES"] = "true"
        runtime_marker = source_runtime / "version.txt"
        marker = (
            runtime_marker.read_text()[:256].strip() if runtime_marker.is_file() else ""
        )
        contract = {}
        contract_path = source_data / "db-contract.json"
        if contract_path.is_file() and contract_path.stat().st_size <= 64 * 1024:
            contract = json.loads(contract_path.read_text())
        terminal = str(
            contract.get("terminalMigration")
            or contract.get("terminal_migration")
            or ""
        )
        if (
            mode == "migrate"
            and provider not in {"postgres", "postgresql"}
            and terminal
            and terminal[:14] < MINIMUM_SCHEMA
        ):
            blockers.append(
                "Upgrade v1 to a native-import-compatible schema (v1.3.0 or newer) first."
            )
        plan = {
            "protocol": PROTOCOL,
            "job_type": JOB_TYPE,
            "mode": mode,
            "options": {
                "mode": mode,
                "mount_backend": mount_backend,
                "import_strategy": "preserve",
            },
            "source": {
                "service": service,
                "runtime_dir": str(source_runtime),
                "data_dir": str(source_data),
                "provider": provider,
                "timezone": timezone,
                "process_name": service.get("process_name") or "InfiniDysk",
                "process_identity": self._process_identity(
                    service.get("process_name") or "InfiniDysk"
                ),
                "runtime_identity": {
                    "directory": _identity(source_runtime),
                    "version_marker": marker,
                },
                "data_identity": _identity(source_data),
                "aliases": list(aliases.values()),
                "published_data_dir": str(v1_data_root(service)),
                "published_runtime_dir": str(service.get("config_dir") or source_data),
                "schema": terminal,
            },
            "target": {
                "service": target_service,
                "runtime_dir": str(target_runtime),
                "data_dir": str(target_data),
                "provider": "sqlite",
                "mount_backend": mount_backend,
                "mount_path": mount_path,
            },
            "config_snapshot": config,
            "inventory": inventory,
            "owned_config_paths": [["infinidysk"]],
            "blockers": blockers,
        }
        try:
            compatibility_auth_environment(target_service)
        except ValueError as error:
            plan["blockers"].append(str(error))
        if mount_backend == "fuse":
            plan["owned_config_paths"].extend(
                ["rclone", "instances", item["instance"]]
                for item in inventory["mounts"]
            )
        selected_id = options.get("backup_destination_id") or ""
        if selected_id:
            from utils.backup_storage import BackupStorageError, get_backup_storage

            if not isinstance(selected_id, str):
                plan["blockers"].append("Choose a registered backup destination.")
                selected_id = "invalid"
            plan["options"]["backup_destination_id"] = selected_id
            plan["backup_storage"] = {
                "mode": "cold_archive",
                "destination_id": selected_id,
                "name": "Selected cold archive storage",
                "path": None,
                "format": "pax_tar_v1",
                "control_state_local": True,
                "candidate_local": True,
                "backups_retained": True,
            }
            try:
                selected = get_backup_storage(self.config_manager).bound_identity(
                    selected_id
                )
                plan["backup_storage"].update(selected)
                protected = [
                    source_data,
                    source_runtime,
                    target_data,
                    target_runtime,
                    self.state_root,
                ]
                for destination in (
                    Path(selected["path"]),
                    Path(selected["artifact_root"]),
                ):
                    candidate_prefix = f".{target_data.name}.dumb-v2-"
                    relative = None
                    try:
                        relative = destination.relative_to(target_data.parent)
                    except ValueError:
                        pass
                    parent_identity = _identity(target_data.parent)
                    destination_parents = (destination, *destination.parents)
                    reserved_candidate = (
                        relative is not None
                        and bool(relative.parts)
                        and relative.parts[0].startswith(candidate_prefix)
                    ) or any(
                        part.name.startswith(candidate_prefix)
                        and parent_identity is not None
                        and _identity(part.parent) == parent_identity
                        for part in destination_parents
                    )
                    if reserved_candidate or any(
                        overlaps(destination, item) for item in protected
                    ):
                        plan["blockers"].append(
                            "Choose backup storage separate from retained v1, active v2, candidate and local control paths."
                        )
                        break
            except (BackupStorageError, OSError):
                plan["blockers"].append(
                    "The selected backup destination is unavailable or its mounted identity changed; no local fallback is allowed."
                )
        if reclaim:
            from utils.infinidysk_v2_space_control import preflight_reuse

            plan["options"]["reclaim_source_space"] = True
            try:
                preflight_reuse(plan)
            except (ValueError, OSError, BackupStorageError) as error:
                plan["blockers"].append(str(error))
        plan["source_storage"] = {
            "mode": "archive_recovery" if reclaim else "retain_local",
            "reclaim_source_space": reclaim,
            "rollback_requires_archive": reclaim,
        }
        binding = copy.deepcopy(
            {
                key: plan[key]
                for key in ("mode", "options", "source", "target", "inventory")
            }
        )
        if plan.get("backup_storage"):
            binding["backup_storage"] = copy.deepcopy(plan["backup_storage"])
        binding["source"].pop("process_identity", None)
        for item in binding["inventory"]["consumers"]:
            item.pop("process_identity", None)
        for item in binding["inventory"]["mounts"]:
            item.pop("identity", None)
            item.pop("mounted", None)
        plan["plan_fingerprint"] = _digest(binding)
        plan["execution_identity_fingerprint"] = _digest(
            {
                "source": plan["source"]["process_identity"],
                "consumers": plan["inventory"]["consumers"],
                "mounts": plan["inventory"]["mounts"],
            }
        )
        postgres_size = None
        check = getattr(self.executor, "preflight", None)
        if callable(check):
            extra = check(copy.deepcopy(plan))
            if not isinstance(extra, dict):
                raise InfiniDyskV2MigrationError(
                    "Executor preflight returned an invalid result."
                )
            plan["blockers"].extend(str(item) for item in extra.get("blockers") or [])
            if provider in {"postgres", "postgresql"} and "postgres" in supported:
                source_binding = extra.get("source_binding")
                postgres_size = (
                    (source_binding or {}).get("database_size_bytes")
                    if isinstance(source_binding, dict)
                    else None
                )
                required = (
                    "physical_identity",
                    "schema_fingerprint",
                    "migration_history_fingerprint",
                    "terminal_migration",
                    "session_timezone",
                )
                if not isinstance(source_binding, dict) or any(
                    not source_binding.get(key) for key in required
                ):
                    # A size without a verified provider identity is not a
                    # complete source estimate, even if it is positive.
                    postgres_size = None
                    plan["blockers"].append(
                        "The PostgreSQL source needs a verified physical/schema identity preflight."
                    )
                else:
                    # Active source row/sequence contents may change until quiescence.
                    # Bind stable source identity/schema here; capture later records the
                    # complete exported-snapshot contents and restore proof.
                    retained_binding = {
                        key: copy.deepcopy(source_binding[key]) for key in required
                    }
                    retained_binding["server"] = copy.deepcopy(
                        source_binding.get("server")
                    )
                    plan["source"]["postgres_binding"] = retained_binding
                    plan["source"]["schema"] = retained_binding["terminal_migration"]
                    binding["source"]["postgres_binding"] = copy.deepcopy(
                        retained_binding
                    )
                    binding["source"]["schema"] = retained_binding["terminal_migration"]
                    plan["plan_fingerprint"] = _digest(binding)
        plan["capacity"] = _unavailable_capacity()
        try:
            plan["capacity"] = self._copy_capacity(plan, postgres_size=postgres_size)
        except InfiniDyskV2CapacityError as error:
            plan["capacity"] = error.capacity
            plan["blockers"].append(str(error))
        except (InfiniDyskV2MigrationError, OSError) as error:
            plan["blockers"].append(str(error))
        return plan

    def status(self):
        with self._lock:
            config = self._config()
            service = config.get("infinidysk") or {}
            jobs = sorted(
                self._jobs(),
                key=lambda item: item.get("created_at_ns", item.get("created_at", 0)),
            )
            blocking = [
                job for job in jobs if self._effective_status(job) in ACTIVE | RECOVERY
            ]
            # A newer finished attempt must never hide older recovery evidence.
            selected = blocking[0] if blocking else (jobs[-1] if jobs else None)
            latest = self._public_job(selected)
            inventory = self._inventory(config, service)
            current_mount = (
                inventory["mounts"][0]["path"]
                if generation(service) == "1" and len(inventory["mounts"]) == 1
                else service.get("mount_path") or "/mnt/debrid/infinidysk"
            )
            blockers = []
            if generation(service) != "1":
                blockers.append("The service already selects generation 2.")
            if self.has_blocking_job():
                blockers.append("A major migration is active or needs recovery review.")
            if self.has_recovery_pending_job():
                blockers.append("A retained major migration needs recovery review.")
            if self.executor is None:
                blockers.append(
                    "The contained native migration executor is unavailable."
                )
            return {
                "protocol": PROTOCOL,
                "job_type": JOB_TYPE,
                "runtime_generation": generation(service),
                "mode": (
                    "migrate"
                    if (v1_data_root(service) / "db.sqlite").exists()
                    or service.get("postgres_enabled")
                    else "fresh"
                ),
                "current": {
                    "mount_backend": service.get("mount_backend") or "rclone",
                    "mount_path": current_mount,
                    "database_provider": (
                        str(
                            (service.get("env") or {}).get("DATABASE_PROVIDER")
                            or (
                                "postgres"
                                if service.get("postgres_enabled")
                                else "sqlite"
                            )
                        ).lower()
                    ),
                    "import_strategy": "preserve",
                    "compatibility_auth_policy": (
                        compatibility_auth_policy(service)
                        if generation(service) == "2"
                        else {"enforced": False, "mode": None, "reason": None}
                    ),
                },
                "supported_mount_backends": ["rclone", "fuse"],
                "job": latest,
                "blocking_job_count": len(blocking),
                "eligibility": {"ready": not blockers, "blockers": blockers},
            }

    def preview(self, options=None):
        plan = self._plan(dict(options or {}))
        with self._lock:
            if self.has_blocking_job():
                plan["blockers"].append(
                    "A major migration is active or needs recovery."
                )
            token = secrets.token_urlsafe(32)
            expires = int(time.time()) + PREVIEW_TTL
            saved = {"plan": plan, "expires_at": expires, "used_for_start": False}
            self._write(
                self.state_root
                / "previews"
                / f"{hashlib.sha256(token.encode()).hexdigest()}.json",
                saved,
            )
            current = {
                "runtime_generation": "1",
                "database_provider": plan["source"]["provider"],
                "mount_backend": "rclone",
                "mount_path": plan["target"]["mount_path"],
            }
            return {
                "protocol": PROTOCOL,
                "job_type": JOB_TYPE,
                "ready": not plan["blockers"],
                "preview_token": token,
                "expires_at": expires,
                "preview_fingerprint": plan["plan_fingerprint"],
                "plan_fingerprint": plan["plan_fingerprint"],
                "confirmation_text": (
                    "MIGRATE INFINIDYSK TO V2"
                    if plan["mode"] == "migrate"
                    else "INSTALL INFINIDYSK V2"
                ),
                "current": current,
                "target": {
                    "runtime_generation": "2",
                    "enabled": bool(plan["target"]["service"].get("enabled")),
                    "database_provider": "sqlite",
                    "mount_backend": plan["target"]["mount_backend"],
                    "mount_path": plan["target"]["mount_path"],
                    "runtime_dir": plan["target"]["runtime_dir"],
                    "data_dir": plan["target"]["data_dir"],
                    "compatibility_auth_policy": compatibility_auth_policy(
                        plan["target"]["service"]
                    ),
                },
                "backup_storage": (
                    {
                        key: copy.deepcopy(plan["backup_storage"].get(key))
                        for key in (
                            "mode",
                            "destination_id",
                            "name",
                            "path",
                            "storage_type",
                            "format",
                            "control_state_local",
                            "candidate_local",
                            "backups_retained",
                        )
                    }
                    if plan.get("backup_storage")
                    else {
                        "mode": "legacy_local_tree",
                        "destination_id": None,
                        "name": "Local retained backup",
                        "path": str(self.state_root / "backups"),
                        "storage_type": "local",
                        "format": "local_tree_v1",
                        "control_state_local": True,
                        "candidate_local": True,
                        "backups_retained": True,
                    }
                ),
                "source_storage": copy.deepcopy(plan["source_storage"]),
                "capacity": copy.deepcopy(plan["capacity"]),
                "checks": [
                    {
                        "name": "source_schema",
                        "status": "verified_at_snapshot",
                        "minimum": MINIMUM_SCHEMA,
                    },
                    {
                        "name": "source_queue",
                        "status": "must_be_empty_after_quiescence",
                        "message": "Finish or review queued download tasks in InfiniDysk 1 before rehearsal. Failed history entries alone do not block this check; DUMB does not delete Arr history or library files.",
                    },
                    {"name": "contained_native_import", "status": "required"},
                ],
                "blockers": list(plan["blockers"]),
                "warnings": [
                    *(
                        [
                            "The retained backend compatibility listener requires native admin authentication mode Required, locked by DUMB for each launch. Saved native authentication settings are unchanged; disable compatibility by using the same backend and frontend port to retain native Local/Disabled mode."
                        ]
                        if compatibility_auth_policy(plan["target"]["service"])[
                            "enforced"
                        ]
                        else []
                    ),
                    *(
                        [
                            "The retained rclone owner will use --links and --dir-cache-time=20s because generation 2 does not send the old directory-refresh notifications."
                        ]
                        if plan["target"]["mount_backend"] == "rclone"
                        else []
                    ),
                    "The native beta import can read NNTP and requires a real contained rehearsal.",
                    "Keep independent backups. Rollback returns to retained v1 data and never merges later v2 writes.",
                    "Timezone, existing library paths and outputs are preserved; OIDC must be explicitly verified.",
                    "Rehearsal can temporarily stop all affected managed consumers and the v1 source.",
                    "Verify external media/Arr mount paths, remote mappings and existing symlink/STRM origins, and stop external writers and scanners during the upgrade. Managed mount readiness does not qualify external playback.",
                ],
                "inventory": plan["inventory"],
                "required_acknowledgements": list(ACKNOWLEDGEMENTS)
                + (
                    ["archive_recovery"]
                    if plan["options"].get("reclaim_source_space")
                    else []
                ),
                "rehearsal_required": plan["mode"] == "migrate",
            }

    def _request_plan(self, request):
        token = str(request.get("preview_token") or "")
        if not _TOKEN.fullmatch(token):
            raise InfiniDyskV2MigrationError("Run a fresh preview before continuing.")
        path = (
            self.state_root
            / "previews"
            / f"{hashlib.sha256(token.encode()).hexdigest()}.json"
        )
        saved = self._read(path)
        if (
            not saved
            or saved["expires_at"] < int(time.time())
            or saved.get("used_for_start")
        ):
            raise InfiniDyskV2MigrationError("The preview expired or was already used.")
        plan = saved["plan"]
        confirmation = (
            "MIGRATE INFINIDYSK TO V2"
            if plan["mode"] == "migrate"
            else "INSTALL INFINIDYSK V2"
        )
        if request.get("confirmation") != confirmation:
            raise InfiniDyskV2MigrationError(f"Type {confirmation} to continue.")
        acknowledgements = request.get("acknowledgements") or {}
        if not isinstance(acknowledgements, dict) or any(
            acknowledgements.get(name) is not True
            for name in (
                list(ACKNOWLEDGEMENTS)
                + (
                    ["archive_recovery"]
                    if plan["options"].get("reclaim_source_space")
                    else []
                )
            )
        ):
            raise InfiniDyskV2MigrationError(
                "Acknowledge backup, downtime, beta, login policy and downstream paths/playback."
            )
        current = self._plan(plan["options"])
        if (
            current["plan_fingerprint"]
            != plan.get("restored_plan_fingerprint", plan["plan_fingerprint"])
            or current["execution_identity_fingerprint"]
            != plan["execution_identity_fingerprint"]
        ):
            raise InfiniDyskV2MigrationError(
                "Configuration, runtime, source, mount or consumer identity changed; preview again."
            )
        if current["blockers"]:
            raise InfiniDyskV2MigrationError("; ".join(current["blockers"]))
        return plan, path, saved

    def rehearse(self, request):
        return self._start(dict(request), "rehearsal")

    def start(self, request):
        return self._start(dict(request), "cutover")

    def _start(self, request, mode):
        plan, preview_path, saved = self._request_plan(request)
        with INFINIDYSK_MIGRATION_ADMISSION_LOCK, self._lock:
            if self.has_blocking_job():
                raise InfiniDyskV2MigrationError(
                    "A major migration is active or needs recovery."
                )
            from utils.infinidysk_migration_admission import (
                infinidysk_external_mutation_active,
                infinidysk_namespace_migration_active,
                infinidysk_postgres_migration_active,
            )

            if (
                infinidysk_namespace_migration_active()
                or infinidysk_postgres_migration_active()
                or infinidysk_external_mutation_active()
            ):
                raise InfiniDyskV2MigrationError(
                    "Another migration or managed mutation owns admission."
                )
            latest = self._read(preview_path)
            if (
                not latest
                or latest.get("used_for_start")
                or latest["expires_at"] < int(time.time())
                or latest["plan"]["plan_fingerprint"] != plan["plan_fingerprint"]
            ):
                raise InfiniDyskV2MigrationError(
                    "The preview expired or changed before job admission."
                )
            if _digest(self._owned(self._config(), plan)) != _digest(
                self._owned(plan["config_snapshot"], plan)
            ):
                raise InfiniDyskV2MigrationError(
                    "Owned configuration changed before job admission; preview again."
                )
            saved = latest
            reviewed_report = None
            reviewed_proof = None
            if mode == "cutover" and plan["mode"] == "migrate":
                rehearsal = self._read(self._job_path(request.get("rehearsal_job_id")))
                if (
                    not rehearsal
                    or rehearsal.get("mode") != "rehearsal"
                    or rehearsal.get("status") != "rehearsed"
                    or (rehearsal.get("result") or {}).get("validated") is not True
                    or (rehearsal.get("result") or {}).get(
                        "download_credentials_verified"
                    )
                    is not True
                    or rehearsal.get("plan_fingerprint") != plan["plan_fingerprint"]
                ):
                    raise InfiniDyskV2MigrationError(
                        "A successful contained rehearsal with download-client credential preservation for this exact plan is required. Run a fresh rehearsal."
                    )
                reviewed_proof = {
                    "job_id": rehearsal["job_id"],
                    "reference": rehearsal.get("omission_proof_ref"),
                }
                self._read_omission_proof(rehearsal)
                reviewed_report = rehearsal.get("import_report")
                if reviewed_report and reviewed_report.get("review_required"):
                    if (
                        reviewed_report.get("status") != "review_required"
                        or (request.get("acknowledgements") or {}).get("skipped_media")
                        is not True
                        or request.get("skip_review_digest")
                        != reviewed_report.get("review_digest")
                    ):
                        raise InfiniDyskV2MigrationError(
                            "Review and explicitly acknowledge the exact skipped items from this rehearsal before cutover."
                        )
            job_id = secrets.token_hex(16)
            job = {
                "protocol": PROTOCOL,
                "job_type": JOB_TYPE,
                "job_id": job_id,
                "mode": mode,
                "source_storage": copy.deepcopy(
                    plan.get("source_storage")
                    or {
                        "mode": "retain_local",
                        "reclaim_source_space": False,
                        "rollback_requires_archive": False,
                    }
                ),
                "reviewed_skip_digest": (reviewed_report or {}).get("review_digest"),
                "reviewed_omission_proof": reviewed_proof,
                "status": "queued",
                "stage": "queued",
                "message": "Waiting for the guarded native migration worker.",
                "created_at": int(time.time()),
                "created_at_ns": time.time_ns(),
                "updated_at": int(time.time()),
                "worker_id": self._worker_id,
                "plan_fingerprint": plan["plan_fingerprint"],
                "plan": plan,
                "preview_record": str(preview_path),
                "result": None,
                "error": None,
                "snapshot": None,
                "activation_exposed": False,
                "activation_committed": False,
            }
            self._active_id = job_id
            self._thread = threading.Thread(
                target=self._run,
                args=(job,),
                name=f"infinidysk-v2-{job_id[:8]}",
                daemon=True,
            )
            self._write(self._job_path(job_id), job)
            if mode == "cutover":
                saved["used_for_start"] = True
                self._write(preview_path, saved)
            try:
                self._thread.start()
            except Exception:
                job.update(
                    status="failed",
                    stage="failed",
                    message="The migration worker could not start.",
                )
                self._write(self._job_path(job_id), job)
                self._active_id = None
                raise
            return {"job": self._public_job(job)}

    def _progress(self, job, stage, message=None, **detail):
        with self._lock:
            self._check_cancel(job)
            job.setdefault("execution", {}).update(copy.deepcopy(detail))
            if "import_progress" in detail:
                job["import_progress"] = copy.deepcopy(detail["import_progress"])
            job.update(
                status="running",
                stage=str(stage),
                message=str(message or stage)[:1000],
                updated_at=int(time.time()),
            )
            self._write(self._job_path(job["job_id"]), job)

    def _check_cancel(self, job):
        with self._lock:
            if (
                job["job_id"] in self._cancel_requested
                and job.get("status") != "rolling_back"
                and not self._rollback_active
            ):
                job["cancel_requested"] = True
                raise InfiniDyskV2Cancelled(
                    "Cancellation requested; stop the owned importer and restore v1."
                )

    def _recovery_progress(self, job, stage, message=None, **detail):
        # Cancellation must not interrupt the recovery that it requested.
        with self._lock:
            job.setdefault("execution", {}).update(copy.deepcopy(detail))
            job.update(
                status="rolling_back",
                stage=str(stage),
                message=str(message or "Restoring the retained v1 service.")[:1000],
                updated_at=int(time.time()),
            )
            self._write(self._job_path(job["job_id"]), job)

    def cancel(self, request):
        with INFINIDYSK_MIGRATION_ADMISSION_LOCK, self._lock:
            job = self._read(self._job_path(request.get("job_id")))
            if (
                not job
                or request.get("confirmation")
                != f"CANCEL INFINIDYSK V2 {job['job_id']}"
            ):
                raise InfiniDyskV2MigrationError(
                    "Confirm the exact running major migration to cancel."
                )
            if self._possibly_exposed(job):
                raise InfiniDyskV2MigrationError(
                    "Generation 2 may contain new writes. Use its consumer recovery controls; cancellation cannot restore v1."
                )
            if job.get("cancel_requested"):
                return {"job": self._public_job(job)}
            if not self._public_job(job)["can_cancel"]:
                raise InfiniDyskV2MigrationError(
                    "This migration is not cancellable. Use its retained rollback when recovery is required."
                )
            job.update(
                cancel_requested=True,
                message="Cancellation requested. Waiting for the owned worker to stop and restore v1; recovery remains protected.",
                updated_at=int(time.time()),
            )
            self._write(self._job_path(job["job_id"]), job)
            self._cancel_requested.add(job["job_id"])
            return {"job": self._public_job(job)}

    def _assert_stopped(self, plan):
        if self.process_handler is None:
            raise InfiniDyskV2MigrationError(
                "Managed process ownership is unavailable."
            )
        names = [plan["source"]["process_name"]]
        names.extend(item["process_name"] for item in plan["inventory"]["consumers"])
        names.extend(
            item["process_name"]
            for item in plan["inventory"]["mounts"]
            if item.get("process_name")
        )
        for name in names:
            if self._process_identity(name) is not None:
                raise InfiniDyskV2MigrationError(
                    f"{name} is still running; no source snapshot was taken."
                )

    def _source_schema(self, database):
        value = database.lstat()
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise InfiniDyskV2MigrationError(
                "The captured source database must be a direct regular file."
            )
        with closing(
            sqlite3.connect(f"file:{quote(str(database))}?mode=ro", uri=True)
        ) as connection:
            rows = connection.execute(
                "SELECT MigrationId FROM __EFMigrationsHistory ORDER BY MigrationId"
            ).fetchall()
            terminal = str(rows[-1][0]) if rows else ""
            if terminal[:14] < MINIMUM_SCHEMA:
                raise InfiniDyskV2MigrationError(
                    "The captured source schema is older than the v1.3 import floor."
                )
            queued = connection.execute("SELECT COUNT(*) FROM QueueItems").fetchone()[0]
            if queued:
                raise InfiniDyskV2MigrationError(
                    f"The captured InfiniDysk 1 download queue contains {queued} item(s). "
                    "Finish or explicitly remove those download tasks using InfiniDysk or the Arr download-client controls, then rehearse again. "
                    "Failed history entries alone do not block this check; DUMB did not delete any queue entries."
                )
            oidc = False
            try:
                values = connection.execute(
                    "SELECT ConfigName, ConfigValue FROM ConfigItems"
                )
                oidc = any(
                    "oidc" in str(name).lower()
                    and str(value).lower() not in {"", "false", "0", "none", "null"}
                    for name, value in values
                )
            except sqlite3.OperationalError:
                raise InfiniDyskV2MigrationError(
                    "The captured source settings cannot be inspected safely."
                ) from None
            names = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            if len(names) > 128:
                raise InfiniDyskV2MigrationError(
                    "The captured source table inventory exceeds its bound."
                )
            counts = {}
            for name in names:
                quoted = str(name).replace('"', '""')
                counts[str(name)] = connection.execute(
                    f'SELECT COUNT(*) FROM "{quoted}"'
                ).fetchone()[0]
        return {
            "terminal_migration": terminal,
            "queue_count": 0,
            "oidc_configured": oidc,
            "tables": counts,
            "database_path": str(database),
        }

    def _capture(self, job, workspace, postgres_snapshot=None):
        with self._lock:
            if job.get("snapshot") or job.get("capture_lease"):
                raise InfiniDyskV2MigrationError(
                    "The source snapshot was already captured or is being captured."
                )
            nonce = secrets.token_hex(16)
            job["capture_lease"] = nonce
            self._write(self._job_path(job["job_id"]), job)
        plan = job["plan"]

        def owner_check():
            with self._lock:
                if (
                    self._active_id != job["job_id"]
                    or job.get("worker_id") != self._worker_id
                    or job.get("capture_lease") != nonce
                    or job.get("status") not in ACTIVE
                ):
                    raise InfiniDyskV2MigrationError(
                        "The source capture worker no longer owns publication."
                    )

        owner_check()
        self._assert_stopped(plan)
        for key, expected in (
            ("data_dir", plan["source"].get("data_identity")),
            (
                "runtime_dir",
                (plan["source"].get("runtime_identity") or {}).get("directory"),
            ),
        ):
            if _identity(Path(plan["source"][key])) != expected:
                raise InfiniDyskV2MigrationError(
                    "An original source root changed before capture."
                )
        self._copy_capacity(plan, postgres_snapshot=postgres_snapshot)
        self._progress(
            job, "snapshotting", "Retaining the complete original v1 data and runtime."
        )
        if (plan.get("backup_storage") or {}).get("mode") == "cold_archive":
            snapshot = self._capture_archive(
                job, workspace, postgres_snapshot, owner_check
            )
        else:
            snapshot = self._capture_local_trees(job, workspace, postgres_snapshot)
        with self._lock:
            owner_check()
            job["snapshot"] = snapshot
            job.pop("capture_lease", None)
            self._write(self._job_path(job["job_id"]), job)
        return copy.deepcopy(snapshot)

    def _capture_local_trees(self, job, workspace, postgres_snapshot=None):
        plan = job["plan"]
        root = self._directory(Path(workspace["root"]))
        self._write(Path(workspace["config_backup"]), plan["config_snapshot"])
        copied = {}
        originals = {}
        if plan["mode"] == "migrate" and plan["source"]["provider"] == "sqlite":
            original_db = Path(plan["source"]["data_dir"]) / "db.sqlite"
            info = original_db.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise InfiniDyskV2MigrationError(
                    "The original source database is linked or unsafe."
                )
        for label, key in (("data", "data_dir"), ("runtime", "runtime_dir")):
            source = Path(plan["source"][key])
            if str(source) in copied:
                continue
            destination = root / f"source-{label}"
            if source.exists():
                if destination.exists():
                    raise InfiniDyskV2MigrationError(
                        "A retained snapshot path is already occupied."
                    )
                originals[str(source)] = _source_fingerprint(
                    source,
                    scratch_root=root,
                    validate_links=label == "data",
                    role=f"Retained v1 {label}",
                )
                shutil.copytree(source, destination, symlinks=True)
                if (
                    _source_fingerprint(
                        destination,
                        scratch_root=root,
                        validate_links=label == "data",
                        role=f"Copied v1 {label}",
                    )
                    != originals[str(source)]
                ):
                    raise InfiniDyskV2MigrationError(
                        "The retained source copy did not verify."
                    )
            else:
                destination.mkdir(mode=0o700)
            copied[str(source)] = str(destination)
        data_snapshot = Path(copied[plan["source"]["data_dir"]])
        schema = None
        if plan["mode"] == "migrate":
            database = data_snapshot / "db.sqlite"
            if plan["source"]["provider"] in {"postgres", "postgresql"}:
                receipt = postgres_snapshot or {}
                if (
                    receipt.get("verified_restore") is not True
                    or receipt.get("source_read_only") is not True
                    or not receipt.get("physical_identity")
                ):
                    raise InfiniDyskV2MigrationError(
                        "A verified independent PostgreSQL dump and read-only native snapshot are required."
                    )
                staged = Path(receipt.get("staged_sqlite") or "")
                dump = Path(receipt.get("dump_path") or "")
                if (
                    root not in staged.resolve().parents
                    or root not in dump.resolve().parents
                    or not staged.is_file()
                    or not dump.is_file()
                ):
                    raise InfiniDyskV2MigrationError(
                        "PostgreSQL backup artifacts are outside this private job."
                    )
                database = staged
            schema = self._source_schema(database)
        candidate = Path(workspace["candidate_data"])
        _path(candidate.parent)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if candidate.exists():
            raise InfiniDyskV2MigrationError("The private candidate path is occupied.")
        candidate.mkdir(mode=0o700)
        shutil.copytree(data_snapshot, candidate, symlinks=True, dirs_exist_ok=True)
        os.chmod(candidate, 0o700)
        candidate_identity = _identity(candidate)
        if schema and plan["source"]["provider"] in {"postgres", "postgresql"}:
            self._stage_candidate_database(candidate, database, candidate_identity)
        snapshot = {
            "candidate_identity": candidate_identity,
            "source_snapshot": str(root),
            "data_snapshot": str(data_snapshot),
            "runtime_snapshot": copied[plan["source"]["runtime_dir"]],
            "config_backup": workspace["config_backup"],
            "source_schema": schema,
            "source_database": str(data_snapshot / "db.sqlite") if schema else None,
            "original_fingerprints": originals,
            "snapshot_fingerprint": _digest(originals),
            "postgres_snapshot": postgres_snapshot,
        }
        return snapshot

    def _stage_candidate_database(self, candidate, database, expected_identity):
        from utils.infinidysk_v2_archive import _source_leaf

        with _source_leaf(candidate, "db.sqlite", expected_identity) as (parent, leaf):
            # Remove only copied private sidecars. An old v1 SQLite WAL must
            # never be replayed against the authoritative PostgreSQL staging DB.
            for suffix in ("-wal", "-shm", "-journal"):
                name = leaf + suffix
                try:
                    value = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not (stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode)):
                    raise InfiniDyskV2MigrationError(
                        "A copied database sidecar is not safely replaceable."
                    )
                os.unlink(name, dir_fd=parent)
            try:
                value = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                value = None
            if value is not None and not (
                stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode)
            ):
                raise InfiniDyskV2MigrationError(
                    "The private candidate database is not safely replaceable."
                )
            temporary = ".dumb-postgres-snapshot-" + secrets.token_hex(16)
            source_fd = os.open(database, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                expected = os.fstat(source_fd)
                if not stat.S_ISREG(expected.st_mode) or expected.st_nlink != 1:
                    raise InfiniDyskV2MigrationError(
                        "The closed staging database is linked or unsafe."
                    )
                target_fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
                with (
                    os.fdopen(target_fd, "wb") as target,
                    os.fdopen(os.dup(source_fd), "rb") as source,
                ):
                    shutil.copyfileobj(source, target, 1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                after = os.fstat(source_fd)
                fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
                if any(getattr(expected, key) != getattr(after, key) for key in fields):
                    raise InfiniDyskV2MigrationError(
                        "The closed staging database changed during its private copy."
                    )
                os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(source_fd)
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def _capture_archive(self, job, workspace, postgres_snapshot, owner_check):
        from utils.backup_storage import get_backup_storage
        from utils.infinidysk_v2_archive import create_retained_archive, _source_leaf

        plan = job["plan"]
        root = self._directory(Path(workspace["root"]))
        self._write(Path(workspace["config_backup"]), plan["config_snapshot"])
        originals = {}
        for key in ("data_dir", "runtime_dir"):
            source = plan["source"][key]
            if source not in originals:
                originals[source] = _source_fingerprint(
                    source,
                    scratch_root=root,
                    validate_links=source == plan["source"]["data_dir"],
                    role="Retained v1 source",
                )
        validation = self._directory(root / "source-database")
        schema = None
        extras = {}
        if plan["mode"] == "migrate":
            if plan["source"]["provider"] in {"postgres", "postgresql"}:
                proof = postgres_snapshot or {}
                if (
                    proof.get("verified_restore") is not True
                    or proof.get("source_read_only") is not True
                    or not proof.get("physical_identity")
                ):
                    raise InfiniDyskV2MigrationError(
                        "A verified independent PostgreSQL dump and read-only native snapshot are required."
                    )
                database = Path(proof.get("staged_sqlite") or "")
                dump = Path(proof.get("dump_path") or "")
                if any(
                    root not in path.resolve().parents
                    or not path.is_file()
                    or path.is_symlink()
                    for path in (database, dump)
                ):
                    raise InfiniDyskV2MigrationError(
                        "PostgreSQL source artifacts must remain inside this local job."
                    )
                extras["v1-postgres.dump"] = dump
                metadata = root / "v1-postgres-snapshot.json"
                if metadata.is_file():
                    extras["v1-postgres-snapshot.json"] = metadata
            else:
                raw = self._directory(validation / "raw")
                source_root = Path(plan["source"]["data_dir"])
                for name in (
                    "db.sqlite",
                    "db.sqlite-wal",
                    "db.sqlite-shm",
                    "db.sqlite-journal",
                ):
                    source = source_root / name
                    if not source.exists() and not source.is_symlink():
                        continue
                    value = source.lstat()
                    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                        raise InfiniDyskV2MigrationError(
                            "A source SQLite file or sidecar is linked or unsafe."
                        )
                    with _source_leaf(
                        source_root, name, plan["source"].get("data_identity")
                    ) as (parent, leaf):
                        descriptor = os.open(
                            leaf,
                            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=parent,
                        )
                        with os.fdopen(descriptor, "rb") as handle:
                            opened = os.fstat(handle.fileno())
                            before = (
                                value.st_dev,
                                value.st_ino,
                                value.st_size,
                                value.st_mtime_ns,
                                value.st_ctime_ns,
                            )
                            if (
                                opened.st_dev,
                                opened.st_ino,
                                opened.st_size,
                                opened.st_mtime_ns,
                                opened.st_ctime_ns,
                            ) != before:
                                raise InfiniDyskV2MigrationError(
                                    "A source SQLite file changed before its local validation copy."
                                )
                            destination_fd = os.open(
                                raw / name,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600,
                            )
                            with os.fdopen(destination_fd, "wb") as destination:
                                shutil.copyfileobj(handle, destination, 1024 * 1024)
                                destination.flush()
                                os.fsync(destination.fileno())
                            after = os.fstat(handle.fileno())
                            if (
                                after.st_dev,
                                after.st_ino,
                                after.st_size,
                                after.st_mtime_ns,
                                after.st_ctime_ns,
                            ) != before:
                                raise InfiniDyskV2MigrationError(
                                    "A source SQLite file changed during its local validation copy."
                                )
                database = validation / "source-database.sqlite"
                descriptor = os.open(
                    database,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                os.close(descriptor)
                # SQLite sees only private copies here, including their WAL.
                # Its backup API produces one closed, independently usable DB.
                with closing(
                    sqlite3.connect(
                        f"file:{quote(str(raw / 'db.sqlite'))}?mode=ro", uri=True
                    )
                ) as original:
                    with closing(sqlite3.connect(database)) as closed:
                        original.backup(closed)
                        closed.execute("PRAGMA journal_mode=DELETE")
                        if (
                            closed.execute("PRAGMA integrity_check").fetchone()[0]
                            != "ok"
                        ):
                            raise InfiniDyskV2MigrationError(
                                "The standalone local source database did not validate."
                            )
                with database.open("rb") as handle:
                    os.fsync(handle.fileno())
                shutil.rmtree(raw)
            schema = self._source_schema(database)
            extras["source-database.sqlite"] = database
        storage = get_backup_storage(self.config_manager)
        archived = create_retained_archive(
            plan,
            workspace,
            storage,
            originals,
            progress=lambda stage, message=None: self._progress(job, stage, message),
            extra_files=extras,
            owner_check=owner_check,
        )
        owner_check()
        # Catch newly created/changed source entries after archive enumeration,
        # before they can enter an unbacked candidate or native conversion.
        for source, fingerprint in originals.items():
            if (
                _source_fingerprint(
                    source,
                    scratch_root=root,
                    validate_links=source == plan["source"]["data_dir"],
                )
                != fingerprint
            ):
                raise InfiniDyskV2MigrationError(
                    "An original source changed after its complete archive was verified."
                )
        snapshot = {
            "format_version": 2,
            "backup_format": "pax_tar_v1",
            "source_snapshot": str(root),
            "config_backup": workspace["config_backup"],
            "source_schema": schema,
            "source_database": str(database) if schema else None,
            "original_fingerprints": originals,
            "snapshot_fingerprint": _digest(originals),
            "postgres_snapshot": postgres_snapshot,
            "cold_archive": archived["receipt"],
            "archive_roots": archived["archive_roots"],
            "candidate_identity": None,
            "source_logical_bytes": _source_summary(
                plan["source"]["data_dir"], validate_links=True
            )["bytes"],
        }

        candidate = Path(workspace["candidate_data"])
        _path(candidate.parent)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if candidate.exists() or candidate.is_symlink():
            raise InfiniDyskV2MigrationError("The private candidate path is occupied.")
        source_data = Path(plan["source"]["data_dir"])
        if plan["options"].get("reclaim_source_space"):
            from utils.infinidysk_v2_space_control import SpaceReuse

            SpaceReuse(self).prepare(job, workspace, snapshot, owner_check)
        else:
            candidate.mkdir(mode=0o700)
            if source_data.exists():
                shutil.copytree(
                    source_data, candidate, symlinks=True, dirs_exist_ok=True
                )
        os.chmod(candidate, 0o700)
        candidate_identity = _identity(candidate)
        self._progress(
            job,
            "snapshotting",
            "Verifying the independent local candidate.",
            candidate_identity=candidate_identity,
        )
        if (
            _source_fingerprint(candidate, scratch_root=root, validate_links=True)
            != originals[plan["source"]["data_dir"]]
        ):
            raise InfiniDyskV2MigrationError(
                "The independent local candidate did not match its complete retained source."
            )
        if snapshot.get("space_recovery"):
            snapshot["space_recovery"]["native_mutations_authorized"] = True
            self._write(self._job_path(job["job_id"]), job)
        if schema and plan["source"]["provider"] in {"postgres", "postgresql"}:
            self._stage_candidate_database(candidate, database, candidate_identity)
        owner_check()
        snapshot["candidate_identity"] = candidate_identity
        return snapshot

    def _validate_receipt(self, job, receipt):
        required = (
            "execution_verified",
            "isolation_verified",
            "import_complete",
            "inventory_verified",
        )
        if not isinstance(receipt, dict) or any(
            receipt.get(key) is not True for key in required
        ):
            raise InfiniDyskV2MigrationError(
                "Native execution did not supply its complete verified acceptance receipt."
            )
        if (
            job["plan"].get("mode") == "migrate"
            and receipt.get("download_credentials_verified") is not True
        ):
            raise InfiniDyskV2MigrationError(
                "The native import did not verify download-client credential preservation. Run a fresh rehearsal before cutover."
            )
        protection = (
            "archive_recovery_verified"
            if job["plan"]["options"].get("reclaim_source_space")
            else "source_untouched"
        )
        if receipt.get(protection) is not True:
            raise InfiniDyskV2MigrationError(
                "The original source or its archive recovery was not verified."
            )
        if receipt.get("plan_fingerprint") != job["plan_fingerprint"]:
            raise InfiniDyskV2MigrationError(
                "Native execution was not bound to this exact plan."
            )
        report = receipt.get("report")
        if (
            not isinstance(report, dict)
            or report.get("failed_files") != 0
            or type(report.get("skipped_files")) is not int
            or report["skipped_files"] < 0
        ):
            raise InfiniDyskV2MigrationError(
                "The native import report contains skipped/failed files or is incomplete."
            )
        diagnostics = report.get("import_report")
        if report["skipped_files"]:
            if (
                not isinstance(diagnostics, dict)
                or diagnostics.get("status") != "review_required"
                or diagnostics.get("review_required") is not True
                or diagnostics.get("blocking_reasons")
                or not re.fullmatch(
                    r"[0-9a-f]{64}", str(diagnostics.get("review_digest") or "")
                )
            ):
                raise InfiniDyskV2MigrationError(
                    "The skipped media do not have a complete reconciled native report."
                )
        if job["plan"].get("mode") == "migrate":
            from utils.infinidysk_v2_omissions import (
                validate_omission_proof,
                verify_omission_subset,
            )

            proof = report.get("omission_proof")
            if proof is None:
                proof = self._read_omission_proof(
                    job, receipt.get("omission_proof_ref")
                )
            validate_omission_proof(proof)
            if len(proof["omitted"]) != report["skipped_files"] or (
                report["skipped_files"]
                and proof["review_digest"] != diagnostics.get("review_digest")
            ):
                raise InfiniDyskV2MigrationError(
                    "The complete omission proof does not match the reconciled native report."
                )
            if job["mode"] == "cutover":
                reviewed = job.get("reviewed_omission_proof") or {}
                reviewed_job = {
                    "job_id": reviewed.get("job_id"),
                    "plan_fingerprint": job["plan_fingerprint"],
                    "omission_proof_ref": reviewed.get("reference"),
                }
                verify_omission_subset(self._read_omission_proof(reviewed_job), proof)
        return receipt

    def _read_omission_proof(self, job, reference=None):
        from utils.infinidysk_v2_omission_store import read_proof

        job_id = job.get("job_id")
        self._job_path(job_id)
        return read_proof(
            self.state_root / "backups" / job_id,
            job_id,
            job["plan_fingerprint"],
            reference if reference is not None else job.get("omission_proof_ref"),
        )

    def _assert_job_owner(self, job):
        with self._lock:
            self._check_cancel(job)
            if (
                self._active_id != job["job_id"]
                or job.get("worker_id") != self._worker_id
                or job.get("status") not in ACTIVE
            ):
                raise InfiniDyskV2MigrationError(
                    "The native import worker no longer owns acceptance."
                )

    def _accept(self, job, receipt):
        self._assert_job_owner(job)
        self._validate_receipt(job, receipt)
        if (receipt.get("report") or {}).get("import_report") is not None:
            job["import_report"] = copy.deepcopy(receipt["report"]["import_report"])
        snapshot = job.get("snapshot")
        if not snapshot:
            raise InfiniDyskV2MigrationError("No verified retained v1 snapshot exists.")
        if (snapshot.get("source_schema") or {}).get("oidc_configured") and receipt.get(
            "oidc_policy_verified"
        ) is not True:
            raise InfiniDyskV2MigrationError(
                "The migrated OIDC policy has not been verified."
            )
        self._assert_stopped(job["plan"])
        if snapshot.get("space_recovery"):
            from utils.infinidysk_v2_space_control import SpaceReuse

            SpaceReuse(self).verify(job)
            if _identity(job["plan"]["source"]["data_dir"]) is not None:
                raise InfiniDyskV2MigrationError(
                    "The reused v1 source path was unexpectedly recreated."
                )
        for source, fingerprint in snapshot["original_fingerprints"].items():
            data = Path(job["plan"]["source"]["data_dir"])
            if snapshot.get("space_recovery") and (
                Path(source) == data or data in Path(source).parents
            ):
                continue
            if (
                _source_fingerprint(
                    source,
                    scratch_root=snapshot["source_snapshot"],
                    validate_links=source == job["plan"]["source"]["data_dir"],
                    role="Retained v1 source",
                )
                != fingerprint
            ):
                raise InfiniDyskV2MigrationError(
                    "The original v1 source changed during native execution."
                )
        with self._lock:
            self._assert_job_owner(job)
            if job["plan"].get("mode") == "migrate":
                from utils.infinidysk_v2_omission_store import write_proof

                proof = receipt["report"].pop("omission_proof")
                reference = write_proof(
                    self._directory(Path(self._workspace(job)["root"])),
                    job["job_id"],
                    job["plan_fingerprint"],
                    proof,
                )
                job["omission_proof_ref"] = reference
                receipt["omission_proof_ref"] = copy.deepcopy(reference)
            # The full report has its own bounded private file. Ordinary job
            # state/receipts must not duplicate it or the complete consent proof.
            receipt.pop("native_report_private", None)
            job["accepted_import"] = {
                "validated": True,
                "snapshot_fingerprint": snapshot["snapshot_fingerprint"],
                "plan_fingerprint": job["plan_fingerprint"],
            }
            self._write(self._job_path(job["job_id"]), job)
            return copy.deepcopy(job["accepted_import"])

    def _owned(self, config, plan):
        result = []
        for path in plan["owned_config_paths"]:
            current = config
            for key in path[:-1]:
                current = current.get(key) if isinstance(current, dict) else None
            exists = isinstance(current, dict) and path[-1] in current
            result.append(
                {
                    "path": path,
                    "exists": exists,
                    "value": copy.deepcopy(current.get(path[-1])) if exists else None,
                }
            )
        return result

    def _owned_for_job(self, config, job):
        records = self._owned(config, job["plan"])
        # This passive backup preference did not exist in old job receipts.
        # Its new empty default cannot invalidate an otherwise exact recovery
        # fingerprint; a selected destination or any other edit still does.
        original = (job["plan"].get("config_snapshot") or {}).get("infinidysk") or {}
        if "backup_destination_id" not in original:
            for record in records:
                value = record.get("value")
                if (
                    record["path"] == ["infinidysk"]
                    and isinstance(value, dict)
                    and value.get("backup_destination_id") == ""
                ):
                    value.pop("backup_destination_id")
        return records

    def _restore_config(self, job):
        plan = job["plan"]
        config = getattr(self.config_manager, "config", None)
        if not isinstance(config, dict):
            raise InfiniDyskV2MigrationError(
                "Managed configuration is unavailable for rollback."
            )
        expected = job.get("activation_fingerprint") or _digest(
            self._owned_for_job(plan["config_snapshot"], job)
        )
        if _digest(self._owned_for_job(config, job)) != expected:
            raise InfiniDyskV2MigrationError(
                "Task-owned configuration changed after cutover; inspect recovery before restoring it."
            )
        for record in self._owned(plan["config_snapshot"], plan):
            current = config
            for key in record["path"][:-1]:
                current = current.setdefault(key, {})
            if record["exists"]:
                current[record["path"][-1]] = copy.deepcopy(record["value"])
            else:
                current.pop(record["path"][-1], None)
        self.config_manager.save_config(plan["source"]["process_name"])

    def _activate_config(self, job, prepared_service=None):
        if not job.get("accepted_import"):
            raise InfiniDyskV2MigrationError(
                "A verified native import must precede configuration activation."
            )
        plan = job["plan"]
        config = getattr(self.config_manager, "config", None)
        if _digest(self._owned_for_job(config, job)) != _digest(
            self._owned_for_job(plan["config_snapshot"], job)
        ):
            raise InfiniDyskV2MigrationError(
                "Task-owned configuration changed during migration."
            )
        prepared = copy.deepcopy(prepared_service or plan["target"]["service"])
        for key in (
            "runtime_generation",
            "mount_backend",
            "mount_path",
            "v2_data_dir",
            "v2_runtime_dir",
            "v2_release_version",
        ):
            if prepared.get(key) != plan["target"]["service"].get(key):
                raise InfiniDyskV2MigrationError(
                    "Prepared runtime configuration differs from the accepted target."
                )
        expected_tz = plan["source"].get("timezone")
        if expected_tz and (prepared.get("env") or {}).get("TZ") != expected_tz:
            raise InfiniDyskV2MigrationError(
                "Prepared runtime changed the source timezone."
            )
        if any(
            (prepared.get("env") or {}).get(key)
            for key in ("DATABASE_PROVIDER", "DATABASE_CONNECTION_STRING")
        ):
            raise InfiniDyskV2MigrationError(
                "Legacy provider variables cannot remain on the activated v2 runtime."
            )
        config["infinidysk"] = prepared
        if plan["target"]["mount_backend"] == "fuse":
            for mount in plan["inventory"]["mounts"]:
                config["rclone"]["instances"][mount["instance"]]["enabled"] = False
        self.config_manager.save_config(plan["source"]["process_name"])
        job["activation_fingerprint"] = _digest(self._owned_for_job(config, job))
        self._write(self._job_path(job["job_id"]), job)

    def _workspace(self, job):
        root = self.state_root / "backups" / job["job_id"]
        target = Path(job["plan"]["target"]["data_dir"])
        workspace = {
            "root": str(root),
            "source_snapshot": str(root),
            "candidate_data": str(
                target.parent / f".{target.name}.dumb-v2-{job['job_id']}"
            ),
            "config_backup": str(root / "dumb-config.json"),
            "receipt_path": str(root / "native-receipt.json"),
        }
        workspace["capture_source"] = lambda postgres_snapshot=None: self._capture(
            job, workspace, postgres_snapshot
        )
        workspace["accept_import"] = lambda receipt: self._accept(job, receipt)
        workspace["record_import_report"] = (
            lambda raw, diagnostics=None: self._record_import_report(
                job, raw, diagnostics
            )
        )
        workspace["restore_source"] = (
            lambda assert_unmounted: self._restore_reused_source(job, assert_unmounted)
        )
        workspace["activate_config"] = (
            lambda prepared_service=None: self._activate_config(job, prepared_service)
        )
        workspace["restore_config"] = lambda: self._restore_config(job)
        workspace["resume_consumers"] = (
            lambda receipt: self._consumer_recovery().resume(job)
        )
        workspace[
            "expose_activation"
        ] = lambda service, receipt: self._consumer_recovery().expose(
            job, service, receipt
        )
        workspace["commit_activation"] = (
            lambda receipt: self._consumer_recovery().commit(job, receipt)
        )
        workspace["check_cancel"] = lambda: self._check_cancel(job)
        return workspace

    def _restore_reused_source(self, job, assert_unmounted):
        from utils.infinidysk_v2_space_control import SpaceReuse

        SpaceReuse(self).restore(job, assert_unmounted)

    def _record_import_report(self, job, raw, diagnostics=None):
        from utils.private_files import atomic_write_private_text

        if raw is not None:
            if not isinstance(raw, str) or len(raw.encode()) > 8_000_000:
                raise InfiniDyskV2MigrationError(
                    "Native report exceeds its supported size."
                )
            path = (
                self._directory(Path(self._workspace(job)["root"]))
                / "native-import-report.txt"
            )
            atomic_write_private_text(path, raw)
            job["native_report_path"] = str(path)
        if diagnostics is not None:
            job["import_report"] = copy.deepcopy(diagnostics)
        self._write(self._job_path(job["job_id"]), job)

    def get_report(self, job_id):
        job = self._read(self._job_path(job_id))
        if not job:
            raise InfiniDyskV2MigrationError("No migration report exists for this job.")
        path = Path(self._workspace(job)["root"]) / "native-import-report.txt"
        if job.get("native_report_path") != str(path):
            raise InfiniDyskV2MigrationError(
                "The native report is unavailable for this job."
            )
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            value = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(value.st_mode)
                or value.st_nlink != 1
                or value.st_uid != os.geteuid()
                or value.st_mode & 0o077
                or value.st_size > 8_000_000
            ):
                raise InfiniDyskV2MigrationError("Native report ownership is invalid.")
            raw = handle.read(8_000_001)
        text = raw.decode("utf-8")

        def redact(value):
            nonlocal text
            if isinstance(value, dict):
                for key, item in value.items():
                    if (
                        any(
                            word in key.lower()
                            for word in (
                                "password",
                                "secret",
                                "token",
                                "api_key",
                                "connection_string",
                                "database_url",
                            )
                        )
                        and isinstance(item, str)
                        and len(item) >= 4
                    ):
                        text = text.replace(item, "[REDACTED]")
                    else:
                        redact(item)
            elif isinstance(value, list):
                for item in value:
                    redact(item)

        redact(job["plan"]["config_snapshot"])
        return re.sub(
            r"(?i)(?:https?|postgres(?:ql)?|nntps?)://[^\s<>\"']+",
            "[URL omitted]",
            text,
        )

    def _error(self, error, plan):
        message = str(error)[:2000]

        def scrub(value):
            nonlocal message
            if isinstance(value, dict):
                for key, item in value.items():
                    if (
                        any(
                            word in key.lower()
                            for word in (
                                "password",
                                "secret",
                                "token",
                                "api_key",
                                "connection_string",
                                "database_url",
                            )
                        )
                        and isinstance(item, str)
                        and len(item) >= 4
                    ):
                        message = message.replace(item, "[REDACTED]")
                    else:
                        scrub(item)
            elif isinstance(value, list):
                for item in value:
                    scrub(item)

        scrub(plan["config_snapshot"])
        return re.sub(
            r"(?i)(postgres(?:ql)?://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@", message
        )

    def _run(self, job):
        update_lock = getattr(self.updater, "updating", None)
        acquired = update_lock is not None and update_lock.acquire(blocking=False)
        try:
            workspace = self._workspace(job)
            if not acquired:
                raise InfiniDyskV2MigrationError(
                    "A service update already owns lifecycle admission."
                )
            self._progress(job, "running", "Starting the contained native migration.")
            operation = (
                self.executor.rehearse
                if job["mode"] == "rehearsal"
                else self.executor.cutover
            )
            from utils.infinidysk_migration_admission import v2_migration_owner

            job["execution_started"] = True
            self._write(self._job_path(job["job_id"]), job)
            with v2_migration_owner("major"):
                receipt = operation(
                    copy.deepcopy(job["plan"]),
                    workspace,
                    lambda stage, message=None, **detail: self._progress(
                        job, stage, message, **detail
                    ),
                )
            self._check_cancel(job)
            self._validate_receipt(job, receipt)
            if not job.get("accepted_import"):
                raise InfiniDyskV2MigrationError(
                    "The executor did not pass native import acceptance."
                )
            if job["mode"] == "rehearsal":
                if receipt.get("source_restored") is not True:
                    raise InfiniDyskV2MigrationError(
                        "Rehearsal did not restore the original v1 service."
                    )
                current = self._plan(job["plan"]["options"])
                if current["plan_fingerprint"] != job["plan_fingerprint"]:
                    from utils.infinidysk_v2_space_control import restored_plan_matches

                    if (
                        not job["plan"]["options"].get("reclaim_source_space")
                        or not restored_plan_matches(job["plan"], current)
                        or (job.get("snapshot", {}).get("space_recovery") or {}).get(
                            "state"
                        )
                        != "restored"
                    ):
                        raise InfiniDyskV2MigrationError(
                            "The source plan changed during rehearsal."
                        )
                    current["restored_plan_fingerprint"] = current["plan_fingerprint"]
                    current["plan_fingerprint"] = job["plan_fingerprint"]
                saved = self._read(Path(job["preview_record"]))
                if saved:
                    saved["plan"] = current
                    # A long, verified rehearsal must leave time to review cutover.
                    saved["expires_at"] = int(time.time()) + PREVIEW_TTL
                    job["preview_expires_at"] = saved["expires_at"]
                    self._write(Path(job["preview_record"]), saved)
                state = "rehearsed"
            else:
                if receipt.get("generation_2_started") is not True or not job.get(
                    "activation_fingerprint"
                ):
                    raise InfiniDyskV2MigrationError(
                        "Generation 2 activation was not verified."
                    )
                state = "completed"
            self._write(Path(workspace["receipt_path"]), receipt)
            with self._lock:
                self._check_cancel(job)
                job.update(
                    status=state,
                    stage=state,
                    message=(
                        "Contained rehearsal verified; v1 is retained."
                        if state == "rehearsed"
                        else "Generation 2 cutover verified; retained v1 rollback is available."
                    ),
                    result={
                        "validated": True,
                        "execution_verified": True,
                        "plan_fingerprint": job["plan_fingerprint"],
                        "import_strategy": "preserve",
                        **(
                            {"download_credentials_verified": True}
                            if receipt.get("download_credentials_verified") is True
                            else {}
                        ),
                    },
                )
                self._write(self._job_path(job["job_id"]), job)
                self._active_id = None
                self._cancel_requested.discard(job["job_id"])
                # Publish completion and release admission in the same lane.
                update_lock.release()
                acquired = False
        except Exception as error:
            restored = False
            recovery_error = None
            cancelled = isinstance(error, InfiniDyskV2Cancelled)
            cleanup_pending = self._retain_owned_process_cleanup(job, error)
            if cancelled:
                job["cancel_requested"] = True
            if cleanup_pending:
                recovery_error = (
                    "Owned helper termination is unconfirmed; private recovery "
                    "files and migration admission remain retained."
                )
            elif self._possibly_exposed(job):
                self._consumer_recovery().hold(job, error)
                return
            elif acquired and job.get("execution_started"):
                try:
                    self._recovery_progress(
                        job,
                        "rolling_back",
                        "Stopping native writers and restoring retained v1 state.",
                    )
                    recovery_job = copy.deepcopy(job)
                    recovery_job["restore_config"] = lambda: self._restore_config(job)
                    recovery_job["restore_source"] = (
                        lambda assert_unmounted: self._restore_reused_source(
                            job, assert_unmounted
                        )
                    )
                    from utils.infinidysk_migration_admission import v2_migration_owner

                    with v2_migration_owner("major"):
                        recovery = self.executor.rollback(
                            recovery_job,
                            lambda stage, message=None, **detail: self._recovery_progress(
                                job, stage, message, **detail
                            ),
                        )
                    restored = (
                        isinstance(recovery, dict)
                        and recovery.get("source_restored") is True
                        and recovery.get("generation_2_stopped") is True
                    )
                except Exception as recovery_failure:
                    cleanup_pending = (
                        self._retain_owned_process_cleanup(job, recovery_failure)
                        or cleanup_pending
                    )
                    recovery_error = (
                        "Owned helper termination remains unconfirmed."
                        if cleanup_pending
                        else self._error(recovery_failure, job["plan"])
                    )
                    restored = False
            elif cancelled and not job.get("execution_started"):
                restored = True
            job.update(
                status=(
                    "recovery_required"
                    if cleanup_pending
                    else (
                        ("cancelled" if cancelled else "failed_restored")
                        if restored
                        else (
                            "recovery_required"
                            if job.get("execution_started")
                            else "failed"
                        )
                    )
                ),
                stage="cancelled" if cancelled and restored else "failed",
                message=(
                    "Migration cancelled. "
                    if cancelled
                    else "Migration did not pass acceptance. "
                )
                + (
                    "Owned helper termination is unconfirmed; recovery remains held."
                    if cleanup_pending
                    else (
                        "Original v1 service was restored."
                        if restored
                        else "Inspect retained recovery state before continuing."
                    )
                ),
                error=(
                    "The owned migration helper could not be confirmed stopped."
                    if cleanup_pending
                    else self._error(error, job["plan"])
                )
                + (
                    f" Recovery remains pending: {recovery_error}"
                    if recovery_error
                    else ""
                ),
                result={
                    "validated": False,
                    "execution_verified": False,
                    "source_restored": restored,
                },
            )
        finally:
            with self._lock:
                try:
                    # A completed worker must not overwrite a later rollback or
                    # clear a newer worker's ownership after releasing its lane.
                    if self._active_id == job["job_id"]:
                        job["updated_at"] = int(time.time())
                        self._write(self._job_path(job["job_id"]), job)
                        self._active_id = None
                        self._cancel_requested.discard(job["job_id"])
                finally:
                    if acquired:
                        update_lock.release()

    def rollback(self, request):
        with INFINIDYSK_MIGRATION_ADMISSION_LOCK, self._lock:
            job = self._read(self._job_path(request.get("job_id")))
            if job and not self._owned_process_cleanup_complete(job):
                raise InfiniDyskV2MigrationError(
                    "Owned helper termination is unconfirmed; retain recovery state before rollback."
                )
            if not job or not self._public_job(job)["can_rollback"]:
                raise InfiniDyskV2MigrationError(
                    "This job has no retained major-migration rollback."
                )
            if request.get("confirmation") != f"ROLLBACK INFINIDYSK V2 {job['job_id']}":
                raise InfiniDyskV2MigrationError(
                    "Confirm the exact retained major-migration rollback."
                )
            if (
                self._possibly_exposed(job)
                and request.get("acknowledge_discard_v2_writes") is not True
            ):
                raise InfiniDyskV2MigrationError(
                    "Explicitly acknowledge that retained v1 rollback discards later generation 2 writes."
                )
            if self.has_blocking_job() and self._effective_status(job) not in RECOVERY:
                raise InfiniDyskV2MigrationError(
                    "Another migration must finish before rollback."
                )
            lock = getattr(self.updater, "updating", None)
            if lock is None or not lock.acquire(blocking=False):
                raise InfiniDyskV2MigrationError("A service update is active.")
            self._active_id = job["job_id"]
            self._rollback_active = True
            job.update(
                status="rolling_back", stage="rolling_back", worker_id=self._worker_id
            )
            self._write(self._job_path(job["job_id"]), job)
        try:
            private = copy.deepcopy(job)
            private["restore_config"] = lambda: self._restore_config(job)
            private["restore_source"] = (
                lambda assert_unmounted: self._restore_reused_source(
                    job, assert_unmounted
                )
            )
            from utils.infinidysk_migration_admission import v2_migration_owner

            with v2_migration_owner("major"):
                receipt = self.executor.rollback(
                    private,
                    lambda stage, message=None, **detail: self._recovery_progress(
                        job, stage, message, **detail
                    ),
                )
            if (
                not isinstance(receipt, dict)
                or receipt.get("generation_2_stopped") is not True
                or receipt.get("source_restored") is not True
            ):
                raise InfiniDyskV2MigrationError(
                    "The executor did not verify rollback ownership and v1 restoration."
                )
            job.pop("owned_process_cleanup", None)
            job.update(
                status="rolled_back",
                stage="rolled_back",
                message="Retained v1 runtime/configuration restored. Later v2 writes were not copied back.",
                result={"validated": True, "reverse_copy_performed": False},
            )
        except Exception as error:
            self._retain_owned_process_cleanup(job, error)
            job.update(
                status="recovery_required",
                stage="recovery_required",
                error=self._error(error, job["plan"]),
            )
            raise
        finally:
            try:
                with self._lock:
                    self._write(self._job_path(job["job_id"]), job)
                    self._active_id = None
                    self._rollback_active = False
            finally:
                lock.release()
        return {"job": self._public_job(job)}


INFINIDYSK_V2_MIGRATION_MANAGER = InfiniDyskV2MigrationManager()
