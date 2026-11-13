"""Guarded complete installation backup/restore, with persistent recovery evidence."""

from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import threading
import tempfile
import time

from utils.backup_storage import (
    BackupStorageError,
    _directory,
    _private_file,
    get_backup_storage,
    mount_records,
    paths_overlap,
    require_local_path,
    _Reader,
)
from utils.installation_backup_archive import (
    FORMAT,
    SCHEMA,
    InstallationArchiveError,
    canonical,
    identity,
    inventory,
    inspect_archive,
    open_leaf,
    write_archive,
)
from utils.installation_backup_inventory import (
    CONTROL_KEYS,
    EXCLUSIONS,
    FORBIDDEN_ROOTS,
    capacity,
    collect_roots,
    compatibility,
    ensure_local_restore_roots,
    service_entries,
    verify_cold_postgres,
)

from utils.installation_backup_limits import paired_workspace_bytes
from utils.installation_backup_codec import compression_kind

from utils.installation_backup_lifecycle import BackupLifecycleError, order_resume_rows
from utils.installation_backup_preview import (
    PreviewCancelled,
    PreviewStatuses,
    PreviewStatusError,
)

PROTOCOL = 1
NAMESPACE = "installation"
MAX_STATE = 4 * 1024 * 1024
PREVIEW_TTL = 900
ACTIVE = {"queued", "running", "recovery_required"}
_MANAGER = None
_MANAGER_LOCK = threading.RLock()
_OWNER = threading.local()
_BOOT_HOLD = None
_SCOPE = {
    "includes": [
        "DUMB configuration, authentication and private keys",
        "Managed application configuration, databases and persistent runtime state",
        "Configured link/STRM namespace state",
    ],
    "excludes": [x["reason"] for x in EXCLUSIONS],
}
_SCHEDULE = {
    "enabled": False,
    "destination_id": "",
    "interval_hours": 24,
    "keep_last": 7,
    "compression": "none",
}
_BLOCKER = "An installation backup or restore owns managed state, or its retained recovery requires review."


class InstallationBackupError(RuntimeError):
    """A safe complete operation cannot be admitted."""


@contextmanager
def installation_owner():
    previous = getattr(_OWNER, "active", False)
    _OWNER.active = True
    try:
        yield
    finally:
        _OWNER.active = previous


@contextmanager
def installation_persistence_work(label):
    """Short atomic admission for control-plane writes; never hold the lock over I/O."""
    from utils.infinidysk_migration_admission import (
        InfiniDyskMigrationAdmissionError,
        reserve_installation_control_write,
        release_infinidysk_external_mutation,
    )

    try:
        token = reserve_installation_control_write(label)
    except InfiniDyskMigrationAdmissionError:
        yield False
        return
    try:
        yield True
    finally:
        release_infinidysk_external_mutation(token)


def set_installation_backup_boot_hold(message):
    global _BOOT_HOLD
    _BOOT_HOLD = str(message)[:500]
    if _MANAGER is not None:
        _MANAGER.boot_error = _BOOT_HOLD


def installation_backup_blocker():
    if getattr(_OWNER, "active", False):
        return None
    if _BOOT_HOLD:
        return _BOOT_HOLD
    if _MANAGER is not None:
        if getattr(_MANAGER, "boot_error", None):
            return _MANAGER.boot_error
        try:
            return _BLOCKER if _MANAGER.has_blocking_job() else None
        except (OSError, ValueError, BackupStorageError, InstallationBackupError):
            return _BLOCKER
    # Boot admission must fail closed even before dependencies are composed.
    descriptors = []
    try:
        config_fd = os.open("/config", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(config_fd)
        directory = os.open(
            "installation-backups",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=config_fd,
        )
        descriptors.append(directory)
        root = os.fstat(directory)
        if root.st_uid != os.geteuid() or root.st_mode & 0o077:
            return _BLOCKER
        descriptor = os.open(
            "active.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            _private_file(info)
            if info.st_uid != os.geteuid():
                return _BLOCKER
            raw = handle.read(MAX_STATE + 1)
        if len(raw) > MAX_STATE:
            return _BLOCKER
        json.loads(raw)
        # Any active record, valid or malformed, is a hold until owned recovery.
        return _BLOCKER
    except FileNotFoundError:
        return None
    except (OSError, ValueError, BackupStorageError):
        return _BLOCKER
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _boot_identity():
    return [
        os.getpid(),
        Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19],
        Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    ]


def _digest(value):
    # Content identity only, never an authentication primitive.
    return hashlib.sha256(canonical(value)).hexdigest()


def _mount_binding(path):
    target = Path(path)
    anchor = target
    missing = []
    while not os.path.lexists(anchor):
        missing.append(anchor.name)
        anchor = anchor.parent
    with _directory(
        anchor if anchor.is_dir() and not anchor.is_symlink() else anchor.parent
    ) as descriptor:
        value = os.fstat(descriptor)
        records = [
            x
            for x in mount_records()
            if Path(x["path"]) == anchor or Path(x["path"]) in anchor.parents
        ]
        mount = (
            max(records, key=lambda x: len(Path(x["path"]).parts)) if records else None
        )
        return {
            "anchor": str(anchor),
            "identity": identity(value),
            "missing": missing,
            "mount": mount,
        }


def _forbidden(path):
    target = Path(path)
    blocked = {
        "/usr",
        "/etc",
        "/var",
        "/proc",
        "/sys",
        "/dev",
        "/root",
        "/home",
        "/run",
        "/boot",
        "/workspace",
        "/dumb",
        "/utils",
    }
    return str(target) in FORBIDDEN_ROOTS or any(
        target == Path(x) or Path(x) in target.parents for x in blocked
    )


@contextmanager
def _owned_child(parent, name):
    """Create or validate one anchored private directory, including raced creation."""
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
        created = True
    except FileExistsError:
        # Another first-use caller may have won; an existing link/file/foreign
        # directory is not accepted without the exact descriptor checks below.
        pass
    descriptor = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
    )
    try:
        value = os.fstat(descriptor)
        if value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) & 0o5077:
            raise InstallationBackupError(
                "Installation control/workspace directories must be private and controller-owned."
            )
        if created:
            # mkdir(0700) still inherits SGID and its group from a setgid
            # config parent. New control roots must satisfy the same private
            # ownership/mode contract used on the next controller boot.
            if value.st_gid != os.getegid():
                os.fchown(descriptor, os.geteuid(), os.getegid())
            os.fchmod(descriptor, 0o700)
            os.fsync(descriptor)
            os.fsync(parent)
        yield descriptor
    finally:
        os.close(descriptor)


class InstallationBackupManager:
    def __init__(
        self,
        config_manager,
        process_handler=None,
        *,
        state_root=None,
        storage=None,
        clock=time.time,
    ):
        self.config_manager = config_manager
        self.handler = process_handler
        self.state_root = Path(
            state_root or Path(config_manager.file_path).parent / "installation-backups"
        )
        self.storage = storage or get_backup_storage(config_manager)
        self.clock = clock
        self._lock = threading.RLock()
        self._worker = None
        self._rollback_running = False
        self._preview_reader = threading.Lock()
        self._preview_statuses = PreviewStatuses(clock=clock, ttl=PREVIEW_TTL)
        self._backup_preparation = None
        self._previews = {}
        self._stop = threading.Event()
        self._scheduler = None
        self._session = secrets.token_hex(16)
        self.workspace_root = Path(tempfile.gettempdir()) / "dumb-installation-work"
        self._root_identity = None
        self._boot = _boot_identity()
        self.boot_error = None

    def initialize_controller(self):
        """Only a new controller process may release a successful restore restart hold."""
        active = self._active()
        if not active:
            return
        job = self._job(active["job_id"])
        if (
            job.get("restart_required")
            and job.get("boot_identity") != self._boot
            and job["status"] in {"completed", "rolled_back"}
        ):
            expected = job.get("restart_config_sha256")
            path = Path(self.config_manager.file_path)
            if (
                expected
                and path.is_file()
                and hashlib.sha256(path.read_bytes()).hexdigest() == expected
            ):
                self._finish(
                    job["job_id"],
                    restart_required=False,
                    can_rollback=False,
                    result=(job.get("result") or {}) | {"restart_required": False},
                    message="The restored configuration loaded in a new controller. Automatic rollback is retired; retained previous trees remain available for manual recovery.",
                )
        self.status()

    @property
    def config(self):
        return deepcopy(getattr(self.config_manager, "config", {}) or {})

    def _state(self, *parts):
        if any(
            not isinstance(part, str)
            or part in {"", ".", ".."}
            or Path(part).name != part
            or "\x00" in part
            for part in parts
        ):
            raise InstallationBackupError(
                "Invalid installation recovery path component."
            )
        with self._lock:
            require_local_path(self.state_root)
            with _directory(self.state_root.parent) as parent:
                with _owned_child(parent, self.state_root.name) as descriptor:
                    current = identity(os.fstat(descriptor))
                    if (
                        self._root_identity is not None
                        and self._root_identity != current
                    ):
                        raise InstallationBackupError(
                            "The local installation recovery filesystem was replaced."
                        )
                    self._root_identity = current
                    with ExitStack() as children:
                        for name in parts[:-1]:
                            descriptor = children.enter_context(
                                _owned_child(descriptor, name)
                            )
        return self.state_root.joinpath(*parts)

    def _workspace(self, identifier):
        if identifier not in {"preview", "restore-preview"} and (
            len(identifier) != 32
            or any(x not in "0123456789abcdef" for x in identifier)
        ):
            raise InstallationBackupError("Invalid private workspace identity.")
        require_local_path(self.workspace_root)
        with _directory(self.workspace_root.parent) as parent:
            with _owned_child(parent, self.workspace_root.name) as root:
                with _owned_child(root, identifier):
                    return self.workspace_root / identifier

    def _write(self, path, value):
        data = canonical(value)
        if len(data) > MAX_STATE:
            raise InstallationBackupError(
                "Installation recovery state exceeds its bound."
            )
        temporary = ".write-" + secrets.token_hex(16)
        with _directory(path.parent, private=True) as parent:
            if os.fstat(parent).st_uid != os.geteuid():
                raise InstallationBackupError(
                    "A recovery write parent has a foreign owner."
                )
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def _read(self, path):
        try:
            with open_leaf(path) as (parent, leaf):
                descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                with os.fdopen(descriptor, "rb") as handle:
                    info = os.fstat(handle.fileno())
                    _private_file(info)
                    if info.st_uid != os.geteuid():
                        raise InstallationBackupError(
                            "A recovery record has a foreign owner."
                        )
                    raw = handle.read(MAX_STATE + 1)
            if len(raw) > MAX_STATE:
                raise InstallationBackupError(
                    "Installation recovery state exceeds its bound."
                )
            return json.loads(raw)
        except FileNotFoundError:
            return None

    def _active(self):
        record = self._read(self._state("active.json"))
        if record is not None:
            identifier = record.get("job_id") if isinstance(record, dict) else None
            if (
                not isinstance(identifier, str)
                or len(identifier) != 32
                or any(x not in "0123456789abcdef" for x in identifier)
            ):
                raise InstallationBackupError(
                    "The active installation recovery lease is malformed; managed state remains frozen."
                )
        return record

    def _job(self, identifier):
        if (
            not isinstance(identifier, str)
            or len(identifier) != 32
            or any(x not in "0123456789abcdef" for x in identifier)
        ):
            raise InstallationBackupError("The installation job identity is invalid.")
        job = self._read(self._state("jobs", identifier + ".json"))
        if not job:
            raise InstallationBackupError("The installation job was not found.")
        if (
            not isinstance(job, dict)
            or job.get("job_id") != identifier
            or job.get("job_type")
            not in {"installation_backup", "installation_restore"}
        ):
            raise InstallationBackupError(
                "The owned installation job identity is malformed."
            )
        return job

    def _save(self, job):
        job["updated_at"] = self.clock()
        self._write(self._state("jobs", job["job_id"] + ".json"), job)

    def _progress(self, identifier, stage, message, **fields):
        with self._lock:
            job = self._job(identifier)
            job.update(stage=stage, message=message, **fields)
            self._save(job)
            return job

    def _public(self, job):
        if job is None:
            return None
        keys = {
            "job_id",
            "job_type",
            "status",
            "stage",
            "message",
            "created_at",
            "updated_at",
            "restart_required",
            "errors",
            "result",
            "can_rollback",
            "rollback_confirmation_text",
            "progress",
        }
        return {key: deepcopy(value) for key, value in job.items() if key in keys}

    def has_blocking_job(self):
        with self._lock:
            return (
                bool(self._active())
                or self._rollback_running
                or bool(self.boot_error)
                or bool(_BOOT_HOLD)
            )

    def status(self):
        with self._lock:
            active = self._active()
            identifier = (active or {}).get("job_id")
            if not identifier:
                latest = self._read(self._state("latest.json")) or {}
                identifier = latest.get("job_id")
            job = self._job(identifier) if identifier else None
            if (
                active
                and job
                and job["status"] in {"queued", "running"}
                and (
                    job.get("session") != self._session
                    or (
                        not self._rollback_running
                        and (self._worker is None or not self._worker.is_alive())
                    )
                )
            ):
                job.update(
                    status="recovery_required",
                    stage="interrupted",
                    message="The installation worker is no longer live. Review retained recovery before starting services.",
                    can_rollback=True,
                    rollback_confirmation_text=f"ROLL BACK {identifier}",
                )
                self._save(job)
            schedule = _SCHEDULE | (self.config.get("installation_backup") or {})
            return {
                "protocol": PROTOCOL,
                "job": self._public(job),
                "preparation": deepcopy(self._backup_preparation),
                "recovery_required": bool(
                    active and (job or {}).get("status") != "running"
                ),
                "schedule": schedule,
                "schedule_runtime": self._schedule_runtime(),
                "scope": deepcopy(_SCOPE),
                "warnings": [
                    "Archives contain credentials, keys and application code. Keep them private and restore only archives you trust."
                ],
            }

    def get_job(self, identifier):
        self.status()
        return {"protocol": PROTOCOL, "job": self._public(self._job(identifier))}

    def catalog(self, destination):
        receipts = self.storage.list_artifacts(destination, NAMESPACE)
        return {
            "protocol": PROTOCOL,
            "artifacts": [
                x for x in receipts if x.get("artifact_kind") == "installation_backup"
            ],
        }

    def _critical_files(self):
        return {
            "config": Path(self.config_manager.file_path),
            "auth": Path(
                os.environ.get("DUMB_AUTH_CONFIG_PATH") or "/config/users.json"
            ),
        }

    @staticmethod
    def _controller_json(path):
        with open_leaf(path) as (parent, leaf):
            descriptor = os.open(
                leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            with os.fdopen(descriptor, "rb") as handle:
                value = os.fstat(handle.fileno())
                if not stat.S_ISREG(value.st_mode) or value.st_size > 8 * 1024 * 1024:
                    raise InstallationBackupError(
                        "A controller configuration/authentication file is not a bounded regular file."
                    )
                raw = handle.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise InstallationBackupError(
                "A controller configuration/authentication file exceeds its bound."
            )
        try:
            if not isinstance(json.loads(raw.decode("utf-8")), dict):
                raise ValueError("object required")
        except (ValueError, UnicodeError):
            raise InstallationBackupError(
                "A controller configuration/authentication file is not valid object JSON."
            ) from None
        return hashlib.sha256(raw).hexdigest()

    def _verify_staged_controller(self, plan, stage):
        for name, current in self._critical_files().items():
            metadata = plan["metadata"]
            if metadata.get(
                "config_path" if name == "config" else "auth_config_path"
            ) != str(current):
                raise InstallationBackupError(
                    "Restore requires the same actual controller config/auth paths, including environment overrides."
                )
            candidates = []
            for root in plan["roots"]:
                base = Path(root["path"])
                if current == base:
                    candidates.append(stage / root["id"])
                elif root["kind"] == "directory" and base in current.parents:
                    candidates.append(stage / root["id"] / current.relative_to(base))
            if len(candidates) != 1 or self._controller_json(
                candidates[0]
            ) != metadata.get(name + "_sha256"):
                raise InstallationBackupError(
                    "The complete staged controller configuration/authentication did not match its retained source hash."
                )

    @staticmethod
    def _verify_controller_proof(metadata, proof):
        for name in ("config", "auth"):
            expected = metadata.get(name + "_sha256")
            if (
                not isinstance(expected, str)
                or len(expected) != 64
                or proof.get("controller_sha256", {}).get(name) != expected
            ):
                raise InstallationBackupError(
                    "The complete archive does not contain both hash-bound controller config/auth files."
                )

    def _backup_verifier(self, stream, scratch, metadata, check, *, progress=None):
        proof = (
            inspect_archive(stream, scratch, owner_check=check)
            if progress is None
            else inspect_archive(stream, scratch, owner_check=check, progress=progress)
        )
        self._verify_controller_proof(metadata, proof)
        return proof

    def _critical_binding(self):
        result = {}
        for name, path in self._critical_files().items():
            try:
                result[name] = self._controller_json(path)
            except (OSError, InstallationBackupError):
                result[name] = None
        return result

    def _binding(self):
        return _digest(
            {
                "config": self.config,
                "controller_files": self._critical_binding(),
                "auth_config_path": str(self._critical_files()["auth"]),
                "compatibility": compatibility(self.config),
                "control": _mount_binding(self.state_root.parent),
            }
        )

    def _restore_contract(self, roots):
        critical = [
            Path(self.config_manager.file_path),
            Path(os.environ.get("DUMB_AUTH_CONFIG_PATH") or "/config/users.json"),
        ]
        budget = 0
        for file in critical:
            covered = False
            if (
                not file.is_file()
                or file.is_symlink()
                or file.stat().st_size > 8 * 1024 * 1024
            ):
                raise InstallationBackupError(
                    "Controller config/auth files must exist as real regular files before complete backup."
                )
            self._controller_json(file)
            for root in roots:
                path = Path(root["path"])
                if path == file:
                    covered = True
                    root["atomic_restore"] = True
                elif root["kind"] == "directory" and path in file.parents:
                    covered = True
                    relative = file.relative_to(path)
                    if len(relative.parts) != 1:
                        raise InstallationBackupError(
                            "A nested controller config/auth path has no crash-safe atomic restore contract."
                        )
                    if relative.name not in root.setdefault("atomic_names", []):
                        root["atomic_names"].append(relative.name)
            if not covered:
                raise InstallationBackupError(
                    "Both actual controller config/auth files must be covered by the complete archive roots."
                )
        for root in roots:
            if root["kind"] != "directory":
                continue
            cost = 0
            with os.scandir(root["path"]) as entries:
                for count, entry in enumerate(entries, 1):
                    if count > 8192:
                        raise InstallationBackupError(
                            "A persistent root exceeds the restorable top-level transaction budget; no partial archive was made."
                        )
                    cost += len(canonical(entry.name)) + 128
            budget += cost * 2 + len(canonical(root)) + 1024
        if budget > MAX_STATE // 2:
            raise InstallationBackupError(
                "The exact restore journal would exceed its bounded recovery state; no partial archive was made."
            )

    @staticmethod
    def _acknowledged(kind, request, confirmation):
        required = (
            {"downtime", "secrets", "external_state"}
            if kind == "backup"
            else {"downtime", "replace_state", "external_state"}
        )
        value = request.get("acknowledgements")
        if (
            request.get("confirmation") != confirmation
            or not isinstance(value, dict)
            or any(value.get(key) is not True for key in required)
        ):
            raise InstallationBackupError(
                "Review the typed confirmation and every required acknowledgement."
            )

    def _replay(self, kind, request):
        token = request.get("preview_token")
        if not isinstance(token, str) or not token:
            return None
        spent = self._read(self._state("tokens", _digest(token) + ".json"))
        if not spent:
            return None
        if spent["kind"] != kind:
            raise InstallationBackupError("This preview belongs to another operation.")
        self._acknowledged(kind, request, spent["confirmation"])
        return {"protocol": PROTOCOL, "job": self._public(self._job(spent["job_id"]))}

    def _preview_record(self, kind, plan, public):
        if len(canonical(plan)) > MAX_STATE // 2:
            public["ready"] = False
            public["blockers"].append(
                "The exact recovery plan exceeds its bounded private state budget."
            )
            plan = {}
        token = secrets.token_urlsafe(32)
        expiry = self.clock() + PREVIEW_TTL
        with self._lock:
            self._previews = {
                k: v
                for k, v in self._previews.items()
                if v["expires_at"] > self.clock()
            }
            if len(self._previews) >= 32:
                self._previews.pop(next(iter(self._previews)))
            self._previews[token] = {
                "kind": kind,
                "plan": plan,
                "expires_at": expiry,
                "binding": self._binding(),
                "confirmation": public["confirmation_text"],
                "ready": public["ready"],
                "token": token,
            }
        return public | {"preview_token": token, "expires_at": expiry}

    def _retained_holders(self):
        parent = self._state("jobs", "placeholder.json").parent
        rows = []
        with os.scandir(parent) as entries:
            for index, entry in enumerate(entries):
                if index >= 1000:
                    raise InstallationBackupError(
                        "The retained operation ledger needs archival review before another complete inventory."
                    )
                if not entry.name.endswith(".json"):
                    continue
                job = self._read(Path(entry.path))
                for row in (job or {}).get("rollback_journal") or []:
                    holder = Path(row["holder"])
                    target = Path(row["root"]["path"])
                    identifier = job["job_id"]
                    expected = (
                        target / f".dumb-installation-restore-{identifier}"
                        if row["root"]["kind"] == "directory"
                        else target.parent
                        / f".dumb-installation-file-{identifier}-{row['root']['id']}"
                    )
                    if holder != expected:
                        raise InstallationBackupError(
                            "A retained rollback holder has an invalid owned path."
                        )
                    if not holder.exists():
                        continue
                    with _directory(holder) as descriptor:
                        value = os.fstat(descriptor)
                        if (
                            identity(value) != row.get("holder_identity")
                            or value.st_mode & 0o077
                        ):
                            raise InstallationBackupError(
                                "A retained rollback holder changed identity."
                            )
                    rows.append(
                        {
                            "job_id": identifier,
                            "path": str(holder),
                            "can_rollback": bool(job.get("can_rollback")),
                            "cleanup": "manual_recovery_only",
                        }
                    )
        return rows

    @contextmanager
    def _preview_scan(self, *, blocking=False):
        if not self._preview_reader.acquire(blocking=blocking):
            raise InstallationBackupError(
                "An installation preview or admission is already inspecting managed state; wait for it to finish."
            )
        try:
            yield
        finally:
            self._preview_reader.release()

    def preview_status(self, preview_id=None):
        try:
            operation = self._preview_statuses.snapshot(preview_id)
        except PreviewStatusError as error:
            raise InstallationBackupError(str(error)) from None
        result = (operation or {}).get("result")
        if result is not None:
            with self._lock:
                record = self._previews.get(result.get("preview_token"))
                if not record or record["expires_at"] <= self.clock():
                    self._preview_statuses.invalidate_result(operation["preview_id"])
                    operation = self._preview_statuses.snapshot(operation["preview_id"])
        return {
            "protocol": PROTOCOL,
            "controller_session": self._session,
            "preview": operation,
        }

    def cancel_preview(self, preview_id):
        try:
            self._preview_statuses.cancel(preview_id)
        except PreviewStatusError as error:
            raise InstallationBackupError(str(error)) from None
        return self.preview_status(preview_id)

    def preview(self, options):
        progress = result = None
        try:
            with self._preview_scan():
                requested_compression = options.get("compression", "none")
                progress = self._preview_statuses.begin(
                    options.get("destination_id"),
                    (
                        requested_compression
                        if requested_compression in ("none", "zstd")
                        else "none"
                    ),
                )
                try:
                    result = self._backup_preview(options, progress=progress.checkpoint)
                    self._preview_statuses.seal(progress.identifier)
                finally:
                    self._preview_statuses.close_cancel_window(progress.identifier)
            # Terminal status is published only after the census workspace is
            # cleaned and this operation has released its reader lane.
            self._preview_statuses.finish(
                progress.identifier, "completed", result=result
            )
            return result
        except PreviewCancelled:
            if result is not None:
                with self._lock:
                    self._previews.pop(result.get("preview_token"), None)
            self._preview_statuses.finish(progress.identifier, "cancelled")
            raise InstallationBackupError(
                "Backup preview cancelled. The preview did not start a backup or stop services."
            ) from None
        except Exception:
            if progress is not None:
                if result is not None:
                    with self._lock:
                        self._previews.pop(result.get("preview_token"), None)
                self._preview_statuses.finish(progress.identifier, "failed")
            raise

    def _backup_preview(self, options, *, progress=None):
        compression = options.get("compression", "none")
        blockers, roots, details = [], [], []
        report = {
            "status": "unavailable",
            "complete": False,
            "estimated": True,
            "reserved": False,
            "observed_at": self.clock(),
            "source": None,
            "filesystems": [],
        }
        plan = {}
        try:
            compression_kind(compression)
            if self.has_blocking_job():
                raise InstallationBackupError(_BLOCKER)
            if progress:
                progress(
                    force=True,
                    stage="checking_destination",
                    message="Checking the selected backup destination.",
                )
            destination = self.storage.bound_identity(options.get("destination_id"))
            scratch = self._workspace("preview")
            if progress:
                progress(
                    force=True,
                    stage="classifying",
                    message="Identifying managed persistence and library link roots.",
                )
            roots, exclusions = collect_roots(
                self.config,
                self.config_manager.file_path,
                self.state_root,
                self.storage,
            )
            if progress:
                progress(
                    force=True,
                    stage="checking_roots",
                    message="Checking managed roots and archive containment.",
                    root_count=len(roots),
                )
            self._restore_contract(roots)
            retained = self._retained_holders()
            exclusions.extend(row["path"] for row in retained)
            if any(
                paths_overlap(
                    destination["artifact_root"],
                    root["path"],
                    second_leaf_object=root["kind"] in {"file", "symlink"},
                )
                for root in roots
                if root["path"]
                not in {
                    str(Path(self.config_manager.file_path).parent),
                    str(self.config.get("data_root") or "/data"),
                }
            ):
                raise InstallationBackupError(
                    "The selected archive store overlaps a managed application root."
                )
            if progress is None:
                report, details = capacity(
                    roots,
                    scratch,
                    destination["path"],
                    exclusions,
                    compression=compression,
                )
            else:
                report, details = capacity(
                    roots,
                    scratch,
                    destination["path"],
                    exclusions,
                    progress=progress,
                    compression=compression,
                )
            report["observed_at"] = self.clock()
            if report["status"] != "sufficient":
                blockers.append(
                    "The complete archive and local verification workspace require more free space; no service was stopped."
                )
            if self.handler is None:
                blockers.append("The managed process lifecycle is unavailable.")
            plan = {
                "roots": roots,
                "exclusions": exclusions,
                "destination": destination,
                "compatibility": compatibility(self.config),
                "compression": compression,
            }
        except (
            OSError,
            ValueError,
            BackupStorageError,
            InstallationArchiveError,
            InstallationBackupError,
        ) as error:
            blockers.append(str(error))
        public = {
            "protocol": PROTOCOL,
            "ready": not blockers,
            "confirmation_text": "BACK UP DUMB INSTALLATION",
            "compression": compression,
            "required_acknowledgements": ["downtime", "secrets", "external_state"],
            "inventory": {"roots": details, "excluded": deepcopy(EXCLUSIONS)},
            "capacity": report,
            "blockers": blockers,
            "warnings": [
                "All managed writers stop while the complete cold archive is made. Large blob stores are included.",
                "Traefik and tunnels may stop too. Use direct frontend/API access before starting.",
                "Existing archives are not recursively embedded. External media/services need their own backups.",
                "This private archive is checksummed, not encrypted or signed.",
            ],
        }
        if progress:
            progress(
                force=True,
                stage="finalizing",
                message="Finalizing the live-state estimate and review token.",
            )
        return self._preview_record("backup", plan, public)

    def _validated_request(self, kind, request):
        with self._lock:
            record = self._previews.get(request.get("preview_token"))
            if (
                not record
                or record["kind"] != kind
                or record["expires_at"] <= self.clock()
            ):
                raise InstallationBackupError(
                    "The preview expired; create and review a new preview."
                )
            if request.get("confirmation") != record["confirmation"]:
                raise InstallationBackupError(
                    "The typed confirmation does not match the preview."
                )
            required = (
                {"downtime", "secrets", "external_state"}
                if kind == "backup"
                else {"downtime", "replace_state", "external_state"}
            )
            acknowledgements = request.get("acknowledgements")
            if not isinstance(acknowledgements, dict) or any(
                acknowledgements.get(x) is not True for x in required
            ):
                raise InstallationBackupError("Confirm every required acknowledgement.")
            if record["binding"] != self._binding():
                raise InstallationBackupError(
                    "Managed configuration or filesystem identity changed; review a new preview."
                )
            if not record.get("ready") or not record["plan"]:
                raise InstallationBackupError("The preview was blocked.")
            return deepcopy(record)

    def _admit(self, kind, record, *, scheduled=False):
        from utils.infinidysk_migration_admission import (
            INFINIDYSK_MIGRATION_ADMISSION_LOCK,
            infinidysk_external_mutation_active,
            infinidysk_namespace_recovery_pending,
            infinidysk_postgres_migration_active,
            infinidysk_postgres_recovery_pending,
            infinidysk_v2_recovery_pending,
        )

        with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
            spent = self._read(
                self._state("tokens", _digest(record["token"]) + ".json")
            )
            if spent:
                return {
                    "protocol": PROTOCOL,
                    "job": self._public(self._job(spent["job_id"])),
                }
            if (
                self.has_blocking_job()
                or infinidysk_external_mutation_active()
                or infinidysk_postgres_migration_active()
                or infinidysk_namespace_recovery_pending()
                or infinidysk_postgres_recovery_pending()
                or infinidysk_v2_recovery_pending()
            ):
                raise InstallationBackupError(
                    "Another managed mutation or migration recovery is active."
                )
            if self.handler is None or getattr(self.handler, "shutting_down", False):
                raise InstallationBackupError(
                    "The managed process lifecycle is unavailable or shutting down."
                )
            if (
                hasattr(self.handler, "is_startup_complete")
                and not self.handler.is_startup_complete()
            ):
                raise InstallationBackupError(
                    "Wait for DUMB startup to reach a terminal state."
                )
            from utils.auto_update import Update

            with Update._scheduled_work_lock:
                if Update._active_scheduled_work:
                    raise InstallationBackupError(
                        "A scheduled update/check/backup worker is still live."
                    )
            from utils.dependencies import _shared_instances

            optimizer = _shared_instances.get("rclone_optimizer_manager")
            if optimizer is not None and any(
                thread.is_alive()
                for thread in getattr(optimizer, "_threads", {}).values()
            ):
                raise InstallationBackupError(
                    "A managed mount optimizer worker is still live."
                )
            from utils.arr_postgres_migration import POSTGRES_MIGRATION_MANAGER

            if POSTGRES_MIGRATION_MANAGER.has_namespace_conflicting_job():
                raise InstallationBackupError(
                    "A managed database migration is active or requires recovery."
                )
            media = _shared_instances.get("media_protection_manager")
            if media is not None:
                with media.lock:
                    if any(
                        row.get("status")
                        in {"active", "waiting_for_recovery", "recovery_failed"}
                        for row in media.incidents.values()
                    ):
                        raise InstallationBackupError(
                            "A media protection incident needs recovery before a complete installation snapshot."
                        )
            identifier = secrets.token_hex(16)
            job = {
                "job_id": identifier,
                "job_type": (
                    "installation_backup"
                    if kind == "backup"
                    else "installation_restore"
                ),
                "status": "queued",
                "stage": "queued",
                "message": "Waiting for the admitted worker.",
                "created_at": self.clock(),
                "updated_at": self.clock(),
                "session": self._session,
                "plan": record["plan"],
                "config_binding": record["binding"],
                "boot_identity": self._boot,
                "scheduled": scheduled,
                "can_rollback": False,
                "errors": [],
            }
            with self._lock:
                if record.get("token") not in self._previews:
                    raise InstallationBackupError(
                        "The reviewed preview was already consumed."
                    )
                self._previews.pop(record["token"])
                self._save(job)
                self._write(self._state("latest.json"), {"job_id": identifier})
                self._write(
                    self._state("active.json"),
                    {"job_id": identifier, "session": self._session},
                )
                self._write(
                    self._state("tokens", _digest(record["token"]) + ".json"),
                    {
                        "kind": kind,
                        "confirmation": record["confirmation"],
                        "job_id": identifier,
                    },
                )
                if scheduled:
                    runtime = self._schedule_runtime()
                    runtime.update(
                        status="running",
                        job_id=identifier,
                        blocker=None,
                        error=None,
                        next_due_at=self.clock() + 300,
                    )
                    self._write(self._state("schedule.json"), runtime)
                self._worker = threading.Thread(
                    target=self._run,
                    args=(identifier,),
                    name="installation-backup",
                    daemon=True,
                )
                try:
                    self._worker.start()
                except BaseException:
                    self._progress(
                        identifier,
                        "dispatch_failed",
                        "The worker could not start.",
                        status="failed",
                    )
                    self._state("active.json").unlink()
                    raise
            return {"protocol": PROTOCOL, "job": self._public(job)}

    def start(self, request):
        return self._start_backup(request)

    def _start_backup(self, request, *, scheduled=False):
        replay = self._replay("backup", request)
        if replay is not None:
            return replay
        with self._preview_scan(blocking=True):
            # A duplicate may have been admitted while this caller waited. It
            # must return that job without allocating another census ledger.
            replay = self._replay("backup", request)
            if replay is not None:
                return replay
            if self.has_blocking_job():
                raise InstallationBackupError(_BLOCKER)
            record = self._validated_request("backup", request)
            plan = record["plan"]
            if (
                self.storage.bound_identity(plan["destination"]["destination_id"])
                != plan["destination"]
            ):
                raise InstallationBackupError(
                    "The destination changed; review a new preview."
                )
            with self._lock:
                self._backup_preparation = {
                    "stage": "checking_capacity",
                    "message": "Rechecking backup coverage and space before stopping services.",
                    "started_at": self.clock(),
                }
            try:
                report, _ = capacity(
                    plan["roots"],
                    self._workspace("preview"),
                    plan["destination"]["path"],
                    plan["exclusions"],
                    compression=plan.get("compression", "none"),
                )
                if report["status"] != "sufficient":
                    raise InstallationBackupError(
                        "Available space changed; the complete backup is blocked before quiescence."
                    )
                return self._admit("backup", record, scheduled=scheduled)
            finally:
                with self._lock:
                    self._backup_preparation = None

    @staticmethod
    def _pid_alive(pid):
        try:
            state = (
                Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[0]
            )
            return state != "Z"
        except FileNotFoundError:
            return False

    def _ordered_lifecycle_rows(self, rows):
        from utils.dependency_map import build_conditional_dependency_map

        configuration = self.config
        dependencies = build_conditional_dependency_map(
            lambda key: configuration.get(key) or {}
        )
        try:
            return order_resume_rows(rows, configuration, dependencies)
        except BackupLifecycleError as error:
            raise InstallationBackupError(str(error)) from None

    def _running(self):
        configured = {}
        for key, service in service_entries(self.config):
            name = service.get("process_name")
            if name:
                configured[self.handler._prefixed_name(name)] = (key, name)
                configured[name] = (key, name)
        found = {}
        for internal, process in list(self.handler.process_names.items()):
            if process is not None and process.poll() is None:
                found[internal] = getattr(process, "pid", None)
        for internal, pid in list(
            getattr(self.handler, "external_processes", {}).items()
        ):
            if self._pid_alive(pid):
                found[internal] = pid
        running = {}
        for internal, pid in found.items():
            if internal not in configured:
                raise InstallationBackupError(
                    "An unclassified managed writer is running; complete quiescence cannot be proved."
                )
            key, name = configured[internal]
            if key not in CONTROL_KEYS:
                running[name] = {"key": key, "name": name, "pid": pid}
        return self._ordered_lifecycle_rows(list(running.values()))

    def _quiesce(self, identifier):
        running = self._running()
        self._progress(
            identifier,
            "quiescing",
            "Stopping the exact previously running managed writers.",
            running_before=running,
            can_rollback=True,
            rollback_confirmation_text=f"ROLL BACK {identifier}",
        )
        for row in reversed(running):
            name = row["name"]
            result = self.handler.stop_process(name)
            if isinstance(result, tuple) and result[0] is False:
                raise InstallationBackupError("A managed writer refused to stop.")
            process = self.handler.process_names.get(self.handler._prefixed_name(name))
            if process is not None and process.poll() is None:
                raise InstallationBackupError(
                    "A managed writer remains live; no archive or restore mutation started."
                )
            pid = row.get("pid")
            if pid and self._pid_alive(pid):
                raise InstallationBackupError(
                    "A managed writer PID remains live; no snapshot was accepted."
                )
            if pid and hasattr(self.handler, "_process_group_alive"):
                deadline = time.monotonic() + 5
                while (
                    self.handler._process_group_alive(pid)
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                if self.handler._process_group_alive(pid):
                    raise InstallationBackupError(
                        "A managed child process group remains live; no snapshot was accepted."
                    )
        verify_cold_postgres(self.config)
        return running

    def _resume(self, identifier):
        from utils.infinidysk_migration_admission import (
            ACTIVE_NAMESPACE_MIGRATION_BLOCKER,
            ACTIVE_POSTGRES_MIGRATION_BLOCKER,
            EXTERNAL_MUTATION_BLOCKER,
            RECOVERY_PENDING_BLOCKER,
        )

        job = self._job(identifier)
        ordered = self._ordered_lifecycle_rows(job.get("running_before") or [])
        self._progress(
            identifier,
            "resuming",
            "Resuming the exact previous running services in dependency order.",
            status="running",
            resume_failure=None,
        )
        for row in ordered:
            name = row["name"]
            process = self.handler.process_names.get(self.handler._prefixed_name(name))
            external = getattr(self.handler, "external_processes", {}).get(name)
            if (process is not None and process.poll() is None) or (
                external and self._pid_alive(external)
            ):
                continue
            pid = row.get("pid")
            if pid and (
                self._pid_alive(pid)
                or (
                    hasattr(self.handler, "_process_group_alive")
                    and self.handler._process_group_alive(pid)
                )
            ):
                raise InstallationBackupError(
                    "An original writer or child group remains live without a managed registration; duplicate restart is refused."
                )
            shown_name = name[:256]
            label = json.dumps(shown_name, ensure_ascii=True)
            if len(label) > 400:
                label = label[:400] + "... [name truncated]"
            elif shown_name != name:
                label += " [name truncated]"
            self._progress(
                identifier,
                "resuming",
                f"Resuming retained service {label}; its dependencies must be ready first.",
                status="running",
            )
            reason = "start_refused"
            hint = "Startup returned a failure; inspect this service's logs for the underlying cause."
            try:
                success, error = self.handler.start_process(name)
            except Exception:
                # Start errors may contain raw environment values, URLs or
                # subprocess output. Never stringify or persist them here.
                success, error = False, None
                reason = "start_exception"
                hint = "Startup raised an exception; inspect this service's logs for the underlying cause."
            if not success:
                if type(error) is str:
                    if error == "Shutdown requested":
                        reason, hint = (
                            "shutdown_requested",
                            "Controller shutdown interrupted startup.",
                        )
                    elif error in {
                        _BLOCKER,
                        "An installation backup/restore owns the managed lifecycle.",
                    }:
                        reason, hint = (
                            "installation_hold",
                            "An installation ownership check refused startup; inspect its retained recovery state.",
                        )
                    elif error in {
                        ACTIVE_NAMESPACE_MIGRATION_BLOCKER,
                        ACTIVE_POSTGRES_MIGRATION_BLOCKER,
                        EXTERNAL_MUTATION_BLOCKER,
                        RECOVERY_PENDING_BLOCKER,
                    }:
                        reason, hint = (
                            "migration_hold",
                            "A managed migration ownership check refused startup; inspect its retained recovery state.",
                        )
                    elif error.startswith(f"Failed to set up {name}:"):
                        reason, hint = (
                            "setup_failed",
                            "Service setup failed; inspect this service's setup logs.",
                        )
                    elif error.startswith(f"Error running subprocess for {name}:"):
                        reason, hint = (
                            "process_launch_failed",
                            "The process launch failed; inspect this service's startup logs.",
                        )
                    elif error.startswith(f"{name} failed to stay running"):
                        reason, hint = (
                            "exited_during_startup",
                            "The process exited during startup; inspect this service's startup logs.",
                        )
                    elif (
                        error
                        == "Could not bind the retained InfiniDysk backend compatibility listener"
                    ):
                        reason, hint = (
                            "compatibility_listener_unavailable",
                            "The retained InfiniDysk compatibility listener could not bind; inspect its startup logs.",
                        )
                message = (
                    f"Previously running service {label} could not resume ({reason}). "
                    f"{hint} Remaining retained services were not started; recovery evidence was retained."
                )
                self._progress(
                    identifier,
                    "resuming",
                    message,
                    status="running",
                    resume_failure={
                        "process_name": shown_name,
                        "reason_code": reason,
                    },
                )
                # Never start downstream consumers after an owner/provider fails.
                raise InstallationBackupError(message) from None

    def _owner_check(self, identifier):
        active = self._active()
        if not active or active.get("job_id") != identifier:
            raise InstallationBackupError(
                "This worker no longer owns the installation lease."
            )

    def _finish(self, identifier, **fields):
        with self._lock:
            job = self._job(identifier)
            job.update(fields)
            self._save(job)
            if (
                not fields.get("restart_required")
                and fields.get("status") not in ACTIVE
                and (self._worker is None or not self._worker.is_alive())
                and not self._rollback_running
            ):
                self._state("active.json").unlink(missing_ok=True)

    def _backup_progress(self, identifier):
        messages = {
            "inventorying": "Indexing stopped application state before writing the archive.",
            "archiving": "Streaming the cold installation archive to the selected destination.",
            "checking_source": "Checking that the source inventory and file contents stayed unchanged.",
            "verifying": "Reading back and checking the closed archive before acceptance.",
        }
        last_stage, last_report = None, 0.0

        def report(
            *, stage, force=False, entries=0, logical_bytes=0, bytes_processed=0
        ):
            nonlocal last_stage, last_report
            now = time.monotonic()
            if not force and stage == last_stage and now - last_report < 1:
                return
            self._progress(
                identifier,
                stage,
                messages[stage],
                progress={
                    "entries": entries,
                    "logical_bytes": logical_bytes,
                    "bytes_processed": bytes_processed,
                },
            )
            last_stage, last_report = stage, now

        return report

    def _backup(self, identifier):
        job = self._job(identifier)
        plan = job["plan"]
        self._quiesce(identifier)
        progress = self._backup_progress(identifier)
        progress(stage="inventorying", force=True)
        if self._binding() != job["config_binding"]:
            raise InstallationBackupError(
                "Configuration changed before the cold backup."
            )
        metadata = {
            "format": FORMAT,
            "schema_version": SCHEMA,
            "created_at": self.clock(),
            "compatibility": plan["compatibility"],
            "roots": [
                {k: value for k, value in root.items() if k != "exclusions"}
                for root in plan["roots"]
            ],
            "excluded": deepcopy(EXCLUSIONS),
            "excluded_paths": plan["exclusions"],
            "scope": _SCOPE,
            "config_path": str(self.config_manager.file_path),
            "auth_config_path": str(self._critical_files()["auth"]),
            "auth_sha256": self._controller_json(self._critical_files()["auth"]),
            "config_sha256": self._controller_json(Path(self.config_manager.file_path)),
        }
        scratch = self._workspace(identifier)
        check = lambda: self._owner_check(identifier)
        receipt = self.storage.write_file(
            plan["destination"]["destination_id"],
            NAMESPACE,
            lambda stream: write_archive(
                stream,
                metadata,
                plan["roots"],
                scratch,
                plan["exclusions"],
                check,
                progress=progress,
                compression=plan.get("compression", "none"),
            ),
            extension="tar.zst" if plan.get("compression") == "zstd" else "tar",
            verifier=lambda stream: self._backup_verifier(
                stream, scratch, metadata, check, progress=progress
            ),
            metadata={
                "kind": "installation_backup",
                "compression": plan.get("compression", "none"),
                "retention_scope": "scheduled" if job.get("scheduled") else "manual",
            },
            owner_check=check,
        )
        self._progress(
            identifier,
            "resuming",
            "The complete archive verified; resuming the previous running set.",
            result={"archive": receipt, "complete": True},
        )
        self._resume(identifier)
        self._finish(
            identifier,
            status="completed",
            stage="completed",
            message="The complete installation archive verified and the previous running services resumed.",
            can_rollback=False,
            result={"archive": receipt, "complete": True},
        )
        if job.get("scheduled"):
            runtime = self._schedule_runtime()
            schedule = _SCHEDULE | (self.config.get("installation_backup") or {})
            runtime.update(
                status="completed",
                last_success_at=self.clock(),
                next_due_at=self.clock() + schedule["interval_hours"] * 3600,
                blocker=None,
                error=None,
            )
            self._write(self._state("schedule.json"), runtime)
            try:
                self._retention(receipt)
            except (BackupStorageError, OSError):
                runtime.update(
                    error="The archive succeeded, but retention could not verify/delete an older scheduled artifact."
                )
                self._write(self._state("schedule.json"), runtime)

    def _run(self, identifier):
        with installation_owner():
            job = self._progress(
                identifier,
                "starting",
                "Starting the admitted installation operation.",
                status="running",
            )
            try:
                if job["job_type"] == "installation_backup":
                    self._backup(identifier)
                else:
                    self._restore(identifier)
            except Exception as error:
                # Never expose archive payloads, environment values or credentials in API errors.
                detail = (
                    str(error)
                    if isinstance(
                        error,
                        (
                            InstallationBackupError,
                            InstallationArchiveError,
                            BackupStorageError,
                        ),
                    )
                    else "An unexpected filesystem or lifecycle error occurred."
                )
                message = "The complete installation operation did not finish. Review retained recovery evidence."
                self._progress(
                    identifier,
                    (
                        "resuming"
                        if job["job_type"] == "installation_backup"
                        else "failed"
                    ),
                    (
                        "The archive was not accepted; resuming the previous running services."
                        if job["job_type"] == "installation_backup"
                        else message
                    ),
                    status="running",
                    errors=[detail],
                )
                if job.get("scheduled"):
                    runtime = self._schedule_runtime()
                    runtime.update(
                        status="error",
                        error=detail[:800],
                        blocker=None,
                        next_due_at=self.clock() + 300,
                    )
                    self._write(self._state("schedule.json"), runtime)
                if job["job_type"] == "installation_backup":
                    try:
                        self._resume(identifier)
                    except Exception as resume_error:
                        self._finish(
                            identifier,
                            status="recovery_required",
                            stage="resume_required",
                            message=message,
                            errors=[
                                detail,
                                (
                                    str(resume_error)
                                    if isinstance(resume_error, InstallationBackupError)
                                    else "Backup failed and the previous running set could not be fully resumed."
                                ),
                            ],
                            can_rollback=True,
                            rollback_confirmation_text=f"ROLL BACK {identifier}",
                        )
                    else:
                        self._finish(
                            identifier,
                            status="failed",
                            stage="failed",
                            message="The archive was not accepted; the previous running services resumed.",
                            errors=[detail],
                            can_rollback=False,
                        )
                else:
                    current = self._job(identifier)
                    if not current.get("mutated") and not current.get(
                        "rollback_journal"
                    ):
                        try:
                            if current.get("running_before"):
                                self._resume(identifier)
                        except Exception:
                            self._finish(
                                identifier,
                                status="recovery_required",
                                stage="resume_required",
                                message="The original writers could not fully resume; recovery remains frozen.",
                                can_rollback=True,
                                rollback_confirmation_text=f"ROLL BACK {identifier}",
                                errors=[detail],
                            )
                        else:
                            self._finish(
                                identifier,
                                status="failed",
                                stage="verification_failed",
                                message="Restore verification failed before live state changed; original services remain available.",
                                can_rollback=False,
                                errors=[detail],
                            )
                    else:
                        self._finish(
                            identifier,
                            status="recovery_required",
                            stage="restore_interrupted",
                            message=message,
                            errors=[
                                "Restore is frozen. Use the exact retained rollback before starting managed services."
                            ],
                            can_rollback=True,
                            rollback_confirmation_text=f"ROLL BACK {identifier}",
                        )
            finally:
                # Workspace contains only this operation's verified copies, never originals or rollback.
                work = self.workspace_root / identifier
                if work.exists() and not work.is_symlink():
                    if any(
                        Path(row["path"]) == work or work in Path(row["path"]).parents
                        for row in mount_records()
                    ):
                        self._progress(
                            identifier,
                            "workspace_review",
                            "A private workspace mount needs operator review; no cleanup was attempted.",
                        )
                    else:
                        shutil.rmtree(work)

                with self._lock:
                    final = self._job(identifier)
                    if final["status"] not in ACTIVE and not final.get(
                        "restart_required"
                    ):
                        self._state("active.json").unlink(missing_ok=True)

    @contextmanager
    def _open_source(self, source):
        if source.get("reference"):
            with self.storage.open_artifact(source["reference"]) as handle:
                yield handle
            return
        path = Path(source["archive_path"])
        if _mount_binding(path.parent) != source["binding"]:
            raise InstallationBackupError(
                "The archive mount or parent changed; no fallback is allowed."
            )
        with open_leaf(path) as (parent, leaf):
            descriptor = os.open(
                leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            with os.fdopen(descriptor, "rb") as handle:
                value = os.fstat(handle.fileno())
                if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                    raise InstallationBackupError(
                        "Select a direct regular installation archive with no hard links."
                    )
                if (
                    value.st_mode & 0o077
                    and source.get("allow_shared_permissions") is not True
                ):
                    raise InstallationBackupError(
                        f"The mounted archive reports mode {stat.S_IMODE(value.st_mode):04o}. "
                        "Use a private archive file, or explicitly allow shared archive "
                        "permissions only if you trust the mounted storage and its access controls."
                    )
                if source.get("file_identity") != [
                    *identity(value),
                    value.st_size,
                    value.st_mtime_ns,
                    value.st_ctime_ns,
                ]:
                    raise InstallationBackupError(
                        "The mounted archive changed after preview."
                    )
                yield handle
                if (
                    source["file_identity"]
                    != [
                        *identity(os.fstat(handle.fileno())),
                        value.st_size,
                        os.fstat(handle.fileno()).st_mtime_ns,
                        os.fstat(handle.fileno()).st_ctime_ns,
                    ]
                    or _mount_binding(path.parent) != source["binding"]
                ):
                    raise InstallationBackupError(
                        "The imported archive changed while it was consumed."
                    )

    def _inspect_source(self, source, scratch, **options):
        with self._open_source(source) as handle:
            reader = _Reader(handle, lambda: None)
            proof = inspect_archive(reader, scratch, **options)
            proof["archive_sha256"] = reader.digest.hexdigest()
            return proof

    def _restore_source(self, options):
        allow_shared = options.get("allow_shared_permissions", False)
        if type(allow_shared) is not bool:
            raise InstallationBackupError(
                "Shared archive permission consent must be a boolean."
            )
        if bool(options.get("reference")) == bool(options.get("archive_path")):
            raise InstallationBackupError(
                "Select exactly one owned reference or mounted archive file."
            )
        if options.get("reference"):
            if allow_shared:
                raise InstallationBackupError(
                    "Registered archives use their destination's reviewed permission "
                    "policy; a mounted-file permission override is not allowed."
                )
            from utils.backup_storage import parse_backup_reference

            profile, namespace, _artifact = parse_backup_reference(options["reference"])
            if namespace != NAMESPACE:
                raise InstallationBackupError(
                    "The reference is not an installation backup."
                )
            if not any(
                x["reference"] == options["reference"]
                and x.get("artifact_kind") == "installation_backup"
                for x in self.storage.list_artifacts(profile, namespace)
            ):
                raise InstallationBackupError(
                    "The registered artifact kind is not an installation backup."
                )
            return {"reference": options["reference"]}
        path = Path(str(options["archive_path"]))
        if not path.is_absolute() or path.is_symlink() or _forbidden(path):
            raise InstallationBackupError(
                "Choose an existing mounted archive file outside protected system paths."
            )
        with open_leaf(path) as (parent, leaf):
            value = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            observed_permissions = {
                "directory_mode": f"{stat.S_IMODE(os.fstat(parent).st_mode):04o}",
                "file_mode": f"{stat.S_IMODE(value.st_mode):04o}",
            }
            if not stat.S_ISREG(value.st_mode):
                raise InstallationBackupError(
                    "The import source is not a regular archive file."
                )
        return {
            "archive_path": str(path),
            "allow_shared_permissions": allow_shared,
            "observed_permissions": observed_permissions,
            "binding": _mount_binding(path.parent),
            "file_identity": [
                *identity(value),
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            ],
        }

    def restore_preview(self, options):
        if not self._preview_reader.acquire(blocking=False):
            raise InstallationBackupError(
                "A complete restore archive inspection is still running; wait for it to finish before requesting another preview."
            )
        try:
            return self._restore_preview(options)
        finally:
            self._preview_reader.release()

    def _restore_preview(self, options):
        blockers = []
        public_metadata = None
        report = {
            "status": "unavailable",
            "complete": False,
            "estimated": True,
            "reserved": False,
            "observed_at": self.clock(),
            "source": None,
            "filesystems": [],
        }
        plan = {}
        details = []
        try:
            if self.has_blocking_job():
                raise InstallationBackupError(_BLOCKER)
            source = self._restore_source(options)
            scratch = self._workspace("restore-preview")
            proof = self._inspect_source(source, scratch)
            metadata = proof["metadata"]
            self._verify_controller_proof(metadata, proof)
            if metadata.get("compatibility") != compatibility(self.config):
                raise InstallationBackupError(
                    "Use the same controller/image identity, architecture, UID/GID and PostgreSQL major as this backup."
                )
            if metadata.get("config_path") != str(
                self._critical_files()["config"]
            ) or metadata.get("auth_config_path") != str(
                self._critical_files()["auth"]
            ):
                raise InstallationBackupError(
                    "Use the same actual controller config/auth paths as the archive, including DUMB_AUTH_CONFIG_PATH."
                )
            roots = deepcopy(metadata["roots"])
            for root in roots:
                root.pop("atomic_names", None)
                root.pop("atomic_restore", None)
            # Atomic root instructions are generated for current actual controller files too.
            self._restore_contract(roots)
            for root in roots:
                path = Path(root.get("path", ""))
                if (
                    not path.is_absolute()
                    or str(path) != os.path.normpath(str(path))
                    or _forbidden(path)
                    or paths_overlap(
                        path,
                        self.state_root,
                        first_leaf_object=root["kind"] in {"file", "symlink"},
                    )
                ):
                    # /config contains control state intentionally; its known child is protected below.
                    if path != self.state_root.parent or _forbidden(path):
                        raise InstallationBackupError(
                            "The archive selects an unsafe system or recovery-control target."
                        )
                for other in roots:
                    if (
                        root is not other
                        and path in Path(other["path"]).parents
                        and root["kind"] == "directory"
                    ):
                        raise InstallationBackupError(
                            "The archive contains overlapping restore roots."
                        )
            ensure_local_restore_roots(roots)
            # A fresh container can have new auth/config, but published container paths never remap.
            bindings = {
                root["id"]: _mount_binding(
                    Path(root["path"]).parent
                    if root["kind"] != "directory"
                    else root["path"]
                )
                for root in roots
            }
            protected = [str(self.state_root), *(metadata.get("excluded_paths") or [])]
            if source.get("archive_path"):
                protected.append(source["archive_path"])
            protected.extend(
                profile["path"]
                for profile in (self.config.get("backup_storage") or {})
                .get("profiles", {})
                .values()
                if isinstance(profile, dict) and isinstance(profile.get("path"), str)
            )
            for root in roots:
                for raw in protected:
                    item = Path(raw)
                    if (
                        Path(root["path"]) in item.parents
                        and len(item.relative_to(root["path"]).parts) > 1
                    ):
                        raise InstallationBackupError(
                            "A nested retained archive/control path must be relocated outside this restore root."
                        )
            report = self._restore_capacity(proof, roots, scratch)
            if report["status"] != "sufficient":
                blockers.append(
                    "There is insufficient space on a restore filesystem for verified staging and retained rollback."
                )
            details = [
                {k: r[k] for k in ("id", "path", "kind", "roles")} for r in roots
            ]
            public_metadata = {
                k: metadata[k]
                for k in ("schema_version", "created_at", "compatibility")
            } | {
                "verified": True,
                "compression": proof.get("compression", "none"),
                "sha256": proof["archive_sha256"],
                **{k: source[k] for k in ("reference", "archive_path") if k in source},
            }
            if source.get("archive_path"):
                public_metadata.update(
                    allow_shared_permissions=source["allow_shared_permissions"],
                    observed_permissions=source["observed_permissions"],
                )
            plan = {
                "source": source,
                "metadata": metadata,
                "proof": {k: v for k, v in proof.items() if k != "metadata"},
                "bindings": bindings,
                "roots": roots,
                "protected": protected,
            }
        except (
            OSError,
            ValueError,
            KeyError,
            BackupStorageError,
            InstallationArchiveError,
            InstallationBackupError,
        ) as error:
            blockers.append(str(error))
        public = {
            "protocol": PROTOCOL,
            "ready": not blockers,
            "confirmation_text": "RESTORE DUMB INSTALLATION",
            "required_acknowledgements": [
                "downtime",
                "replace_state",
                "external_state",
            ],
            "inventory": {"roots": details, "excluded": deepcopy(EXCLUSIONS)},
            "backup": public_metadata,
            "observed_permissions": (public_metadata or {}).get("observed_permissions"),
            "capacity": report,
            "blockers": blockers,
            "warnings": [
                "Restore only archives you trust: application code, authentication and private keys are replaced.",
                "All managed writers remain stopped after restore. Restart the container to reload restored settings and authentication.",
                "Traefik and tunnels may stop too. Use direct frontend/API access before starting.",
                "External media, external databases and the container image are not restored.",
            ]
            + (
                [
                    "Shared archive permissions were explicitly accepted. The mounted "
                    "storage and its ACLs control access to credentials and private keys; "
                    "reported mode bits do not prove confidentiality. DUMB does not change "
                    "the share's permissions or ownership."
                ]
                if plan.get("source", {}).get("allow_shared_permissions") is True
                else []
            ),
        }
        return self._preview_record("restore", plan, public)

    def restore_start(self, request):
        replay = self._replay("restore", request)
        if replay is not None:
            return replay
        with self._preview_scan(blocking=True):
            replay = self._replay("restore", request)
            if replay is not None:
                return replay
            if self.has_blocking_job():
                raise InstallationBackupError(_BLOCKER)
            record = self._validated_request("restore", request)
            plan = record["plan"]
            for root in plan["roots"]:
                path = Path(root["path"])
                if (
                    _mount_binding(path.parent if root["kind"] != "directory" else path)
                    != plan["bindings"][root["id"]]
                ):
                    raise InstallationBackupError(
                        "A restore filesystem changed; review a new preview."
                    )
            if (
                self._restore_capacity(
                    plan["proof"], plan["roots"], self._workspace("restore-preview")
                )["status"]
                != "sufficient"
            ):
                raise InstallationBackupError(
                    "Restore capacity changed; review a new preview."
                )
            return self._admit("restore", record)

    def _restore_capacity(self, proof, roots, scratch):
        groups = {}
        allocations = [
            (
                scratch,
                "local_verification",
                proof["logical_bytes"]
                + paired_workspace_bytes(proof.get("ledger_bytes")),
            )
        ]
        for root in roots:
            target = Path(root["path"])
            atomic_bytes = sum(
                (target / name).lstat().st_size
                for name in root.get("atomic_names", [])
                if os.path.lexists(target / name)
            )
            if root.get("atomic_restore") and os.path.lexists(target):
                atomic_bytes += target.lstat().st_size
            allocations.append(
                (
                    (
                        Path(root["path"]).parent
                        if root["kind"] != "directory"
                        else Path(root["path"])
                    ),
                    "restore_staging",
                    proof["root_sizes"][root["id"]] + atomic_bytes,
                )
            )
        for raw, role, amount in allocations:
            anchor = Path(raw)
            while not anchor.exists():
                anchor = anchor.parent
            group = groups.setdefault(
                anchor.stat().st_dev,
                {
                    "roles": [],
                    "base_required_bytes": 0,
                    "free_bytes": shutil.disk_usage(anchor).free,
                },
            )
            if role not in group["roles"]:
                group["roles"].append(role)
            group["base_required_bytes"] += amount
        filesystems = []
        for group in groups.values():
            base = group["base_required_bytes"]
            margin = (base + 9) // 10
            required = base + margin
            filesystems.append(
                {
                    "id": f"filesystem_{len(filesystems)+1}",
                    **group,
                    "safety_margin_bytes": margin,
                    "required_bytes": required,
                    "shortfall_bytes": max(0, required - group["free_bytes"]),
                    "sufficient": group["free_bytes"] >= required,
                }
            )
        return {
            "status": (
                "sufficient"
                if all(x["sufficient"] for x in filesystems)
                else "insufficient"
            ),
            "complete": True,
            "estimated": True,
            "reserved": False,
            "observed_at": self.clock(),
            "source": {
                "entries": proof["entries"],
                "logical_bytes": proof["logical_bytes"],
                "root_count": len(roots),
            },
            "filesystems": filesystems,
        }

    def _restore(self, identifier):
        job = self._job(identifier)
        plan = job["plan"]
        self._progress(
            identifier,
            "verifying",
            "Verifying and staging the entire archive before any live state is replaced.",
        )
        scratch = self._workspace(identifier)
        if (
            self._restore_capacity(plan["proof"], plan["roots"], scratch)["status"]
            != "sufficient"
        ):
            raise InstallationBackupError(
                "Restore filesystem capacity changed before staging."
            )
        stage = scratch / "verified-stage"
        stage.mkdir(mode=0o700)
        proof = self._inspect_source(
            plan["source"],
            scratch,
            extract_root=stage,
            owner_check=lambda: self._owner_check(identifier),
        )
        if {k: v for k, v in proof.items() if k != "metadata"} != plan[
            "proof"
        ] or proof["metadata"] != plan["metadata"]:
            raise InstallationBackupError(
                "The archive changed between preview and staged restore."
            )
        self._verify_staged_controller(plan, stage)
        if self._binding() != job["config_binding"]:
            raise InstallationBackupError(
                "Managed configuration changed before restore quiescence."
            )
        self._quiesce(identifier)
        self._progress(
            identifier,
            "replacing",
            "Replacing only reviewed persistent roots; every move has identity-bound rollback evidence.",
            rollback_journal=[],
            mutated=True,
        )
        from utils.installation_backup_restore import prepare, apply

        for root in plan["roots"]:
            target = Path(root["path"])
            anchor = target.parent if root["kind"] != "directory" else target
            if _mount_binding(anchor) != plan["bindings"][root["id"]]:
                raise InstallationBackupError("A restore target filesystem changed.")
            while not anchor.exists():
                anchor = anchor.parent
            required = plan["proof"]["root_sizes"][root["id"]]
            if shutil.disk_usage(anchor).free < required + (required + 9) // 10:
                raise InstallationBackupError(
                    "A target filesystem no longer has room for its complete verified staged replacement."
                )
            index = len(self._job(identifier)["rollback_journal"])
            persist = lambda row, index=index: self._persist_transaction(
                identifier, index, row
            )
            row = prepare(
                root, stage / root["id"], identifier, plan["protected"], persist
            )
            apply(row, persist)
        expected = hashlib.sha256(
            Path(self.config_manager.file_path).read_bytes()
        ).hexdigest()
        self._finish(
            identifier,
            status="completed",
            stage="restart_required",
            message="The complete installation restored. Managed services remain stopped; restart the container to reload settings and authentication.",
            restart_required=True,
            restart_config_sha256=expected,
            can_rollback=True,
            rollback_confirmation_text=f"ROLL BACK {identifier}",
            result={
                "complete": True,
                "restart_required": True,
                "retained_previous_paths": [
                    row["holder"]
                    for row in self._job(identifier).get("rollback_journal", [])
                ],
                "rollback_retirement": "after_controller_restart",
            },
        )

    def _persist_transaction(self, identifier, index, row):
        with self._lock:
            job = self._job(identifier)
            journal = job.setdefault("rollback_journal", [])
            if index == len(journal):
                journal.append(deepcopy(row))
            else:
                journal[index] = deepcopy(row)
            self._save(job)

    def rollback(self, identifier, request):
        from utils.infinidysk_migration_admission import (
            INFINIDYSK_MIGRATION_ADMISSION_LOCK,
            infinidysk_external_mutation_active,
            infinidysk_namespace_recovery_pending,
            infinidysk_postgres_recovery_pending,
            infinidysk_v2_recovery_pending,
        )

        with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
            with self._lock:
                job = self._job(identifier)
                if (
                    not job.get("can_rollback")
                    or request.get("confirmation") != f"ROLL BACK {identifier}"
                ):
                    raise InstallationBackupError(
                        "Review the retained job and type its exact rollback confirmation."
                    )
                if self._rollback_running or (
                    self._worker is not None and self._worker.is_alive()
                ):
                    raise InstallationBackupError(
                        "An installation writer is still live; rollback cannot overlap it."
                    )
                active = self._active()
                if active and active.get("job_id") != identifier:
                    raise InstallationBackupError(
                        "Another installation operation owns recovery."
                    )
                with installation_owner():
                    if (
                        infinidysk_external_mutation_active()
                        or infinidysk_namespace_recovery_pending()
                        or infinidysk_postgres_recovery_pending()
                        or infinidysk_v2_recovery_pending()
                    ):
                        raise InstallationBackupError(
                            "Another managed mutation or migration recovery blocks rollback."
                        )
                same_boot = job.get("boot_identity") == self._boot
                self._rollback_running = True
                try:
                    job.update(
                        status="running",
                        stage="rolling_back",
                        message="Restoring the exact retained original identities.",
                        session=self._session,
                        boot_identity=self._boot,
                        can_rollback=False,
                    )
                    self._save(job)
                    self._write(
                        self._state("active.json"),
                        {"job_id": identifier, "session": self._session},
                    )
                except BaseException:
                    self._rollback_running = False
                    raise
        try:
            with installation_owner():
                if job.get("mutated"):
                    self._rollback_roots(identifier, job)
                if (
                    job.get("job_type") == "installation_backup"
                    and same_boot
                    and not self.boot_error
                    and not _BOOT_HOLD
                ):
                    self._resume(identifier)
                    restart = False
                else:
                    restart = True
                self._finish(
                    identifier,
                    status="rolled_back",
                    stage="restart_required" if restart else "rolled_back",
                    message=(
                        "Exact original state restored; restart the container before starting services."
                        if restart
                        else "The previous running set resumed."
                    ),
                    can_rollback=False,
                    restart_required=restart,
                    restart_config_sha256=hashlib.sha256(
                        Path(self.config_manager.file_path).read_bytes()
                    ).hexdigest(),
                    result={"restart_required": restart},
                )
        except Exception:
            self._finish(
                identifier,
                status="recovery_required",
                stage="rollback_interrupted",
                message="Rollback did not finish; all retained originals and recovery evidence remain protected.",
                can_rollback=True,
                rollback_confirmation_text=f"ROLL BACK {identifier}",
            )
            raise InstallationBackupError(
                "Rollback requires recovery review; no concurrent writer was admitted."
            ) from None
        finally:
            with self._lock:
                self._rollback_running = False
                final = self._job(identifier)
                if final["status"] not in ACTIVE and not final.get("restart_required"):
                    self._state("active.json").unlink(missing_ok=True)
        return self.get_job(identifier)

    def _rollback_roots(self, identifier, job):
        from utils.installation_backup_restore import rollback, validate

        rows = job.get("rollback_journal") or []
        for row in rows:
            validate(row)
        for index in reversed(range(len(rows))):
            rollback(
                rows[index],
                lambda row, index=index: self._persist_transaction(
                    identifier, index, row
                ),
            )

    def update_schedule(self, options):
        schedule = _SCHEDULE | (self.config.get("installation_backup") or {}) | options
        compression_kind(schedule["compression"])
        if type(schedule.get("enabled")) is not bool or not isinstance(
            schedule.get("destination_id"), str
        ):
            raise InstallationBackupError("Invalid schedule settings.")
        if (
            type(schedule.get("interval_hours")) is not int
            or not 1 <= schedule["interval_hours"] <= 8760
            or type(schedule.get("keep_last")) is not int
            or not 1 <= schedule["keep_last"] <= 100
        ):
            raise InstallationBackupError(
                "Schedule interval or retention exceeds its supported bounds."
            )
        if schedule["enabled"]:
            self.storage.bound_identity(schedule["destination_id"])
        if self.has_blocking_job():
            raise InstallationBackupError(_BLOCKER)
        self.config_manager.config["installation_backup"] = schedule
        self.config_manager.save_config()
        return {"protocol": PROTOCOL, "schedule": schedule}

    def _retention(self, latest):
        schedule = _SCHEDULE | (self.config.get("installation_backup") or {})
        rows = [
            x
            for x in self.storage.list_artifacts(
                latest["backup_destination_id"], NAMESPACE
            )
            if x.get("artifact_kind") == "installation_backup"
            and x.get("retention_scope") == "scheduled"
        ]
        keep = {latest["reference"]}
        keep.update(
            x["reference"]
            for x in sorted(
                rows, key=lambda x: (x["created_at"], x["artifact_id"]), reverse=True
            )[: schedule["keep_last"]]
        )
        for row in rows:
            if row["reference"] not in keep:
                self.storage.delete_artifact(row["reference"])

    def _schedule_runtime(self):
        record = self._read(self._state("schedule.json")) or {}
        schedule = _SCHEDULE | (self.config.get("installation_backup") or {})
        if not schedule["enabled"]:
            return {**record, "status": "disabled", "next_due_at": None}
        return {
            "status": "scheduled",
            "last_attempt_at": None,
            "last_success_at": None,
            "next_due_at": None,
            "blocker": None,
            "error": None,
            **record,
        }

    def tick(self):
        schedule = _SCHEDULE | (self.config.get("installation_backup") or {})
        if not schedule["enabled"]:
            return
        previous = self._schedule_runtime()
        due = previous.get("next_due_at")
        if due is None:
            self._write(
                self._state("schedule.json"),
                previous
                | {"next_due_at": self.clock() + schedule["interval_hours"] * 3600},
            )
            return
        if self.clock() < due:
            return
        previous.update(last_attempt_at=self.clock())
        try:
            if self.has_blocking_job():
                raise InstallationBackupError(
                    "An installation job is active or needs recovery."
                )
            preview = self.preview(
                {
                    "destination_id": schedule["destination_id"],
                    "compression": schedule.get("compression", "none"),
                }
            )
            if not preview["ready"]:
                raise InstallationBackupError("; ".join(preview["blockers"])[:800])
            request = {
                "preview_token": preview["preview_token"],
                "confirmation": preview["confirmation_text"],
                "acknowledgements": {
                    "downtime": True,
                    "secrets": True,
                    "external_state": True,
                },
            }
            self._write(self._state("schedule.json"), previous)
            self._start_backup(request, scheduled=True)
            return
        except (
            InstallationBackupError,
            BackupStorageError,
            InstallationArchiveError,
        ) as error:
            previous.update(
                status="blocked",
                blocker=str(error)[:800],
                error=None,
                next_due_at=self.clock() + 300,
            )
        except Exception:
            previous.update(
                status="error",
                blocker=None,
                error="The scheduled installation preview could not be completed.",
                next_due_at=self.clock() + 300,
            )
        self._write(self._state("schedule.json"), previous)

    def start_scheduler(self):
        if self._scheduler is not None and self._scheduler.is_alive():
            return
        self._stop.clear()

        def run():
            while not self._stop.wait(30):
                try:
                    self.tick()
                except Exception:
                    # Retry on the next bounded tick; never create concurrent workers.
                    continue

        self._scheduler = threading.Thread(
            target=run, name="installation-backup-schedule", daemon=True
        )
        self._scheduler.start()

    def shutdown(self):
        self._stop.set()
        if self._scheduler is not None:
            self._scheduler.join(timeout=5)
        # Never cancel a writer or clear its durable lease while it remains live.


def get_installation_backup_manager(config_manager=None, process_handler=None):
    global _MANAGER
    if _MANAGER is None:
        if config_manager is None:
            from utils.config_loader import CONFIG_MANAGER

            config_manager = CONFIG_MANAGER
        _MANAGER = InstallationBackupManager(config_manager, process_handler)
    elif process_handler is not None:
        _MANAGER.handler = process_handler
    return _MANAGER
