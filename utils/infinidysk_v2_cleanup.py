"""Explicit retirement of an unchanged v1 installation after a verified cutover.

Backups are retained. A bounded, immutable local SQLite deletion manifest is
fully verified before rollback is durably retired. Deletion uses anchored file
descriptors, never recursive removal of whatever happens to occupy a path.
"""

from contextlib import closing, contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sqlite3
import stat
import threading
import time
from urllib.parse import unquote, urlsplit

from utils.backup_storage import (
    BackupStorageError,
    mount_records,
    paths_overlap,
    require_local_path,
)
from utils.infinidysk_v2_inventory import (
    MAX_TREE_ENTRIES,
    MAX_TREE_MANIFEST_BYTES,
    iter_tree,
)
from utils.infinidysk_v2_migration import (
    ACTIVE,
    RECOVERY,
    InfiniDyskV2MigrationError,
    _digest,
    _identity,
    _path,
    _source_fingerprint,
    _source_summary,
)
from utils.infinidysk_v2_runtime import (
    native_api_request,
    native_database_url,
    resolve_v2_data_dir,
    runtime_dir,
)
from utils.infinidysk_v2_support import generation
from utils.infinidysk_v2_topology import delivery_mount_ready

TTL = 15 * 60
ACKS = ("playback", "test_import", "rollback_retirement")
RUNNING = {"queued", "verifying", "retiring", "deleting"}
TABLE_SQL = "CREATE TABLE entries (root INTEGER, path BLOB, depth INTEGER, row BLOB, stamp BLOB, PRIMARY KEY(root,path)) WITHOUT ROWID"
INDEX_SQL = "CREATE INDEX deletion_order ON entries (root, depth DESC, path)"


def _overlap(first, second):
    return paths_overlap(first, second)


def _local_reference_path(token):
    """Decode only local file/SQLite references, never arbitrary remote URLs."""
    if token.startswith("/"):
        return token
    scheme, separator, tail = token.partition(":")
    if not separator:
        return None
    scheme = scheme.lower()
    if scheme == "file":
        parsed = urlsplit(token)
        if parsed.netloc.lower() not in {"", "localhost"}:
            return None
        path = unquote(parsed.path, errors="surrogateescape")
        return path if path.startswith("/") else None
    if not re.fullmatch(r"sqlite(?:\+[a-z0-9_]+)?", scheme):
        return None
    if tail.startswith("///"):
        # SQLAlchemy's database follows its third slash. Only the bare
        # sqlite dialect also accepts SQLx's three-slash absolute form.
        database = tail[3:]
        if database.startswith("file:"):
            parsed = urlsplit(database)
            if parsed.netloc.lower() not in {"", "localhost"}:
                return None
            path = unquote(parsed.path, errors="surrogateescape")
            return path if path.startswith("/") else None
        if not database.startswith("/") and scheme != "sqlite":
            return None
        parsed = urlsplit("file:/" + database.lstrip("/"))
        return unquote(parsed.path, errors="surrogateescape")
    if tail.startswith("/") and not tail.startswith("//"):
        return unquote(urlsplit(token).path, errors="surrogateescape")
    return None


def _reference_tokens(value):
    tokens = [value]
    try:
        tokens.extend(shlex.split(value))
    except ValueError:
        pass
    for token in tokens:
        token = token.strip("\"'")
        # Match an actual leading flag/environment assignment. Query '='
        # characters in a URI must remain part of that URI.
        assignment = re.match(r"^(?:--?)?[A-Za-z_][A-Za-z0-9_.-]*=", token)
        if assignment is not None:
            token = token[assignment.end() :].strip("\"'")
        yield token


def _stamp(value):
    return [
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_nlink,
    ]


def _hash_file(handle):
    digest = hashlib.sha256()
    while chunk := handle.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _entries_digest(connection, root_count):
    digest = hashlib.sha256()
    count = 0
    for root, path, depth, row, stamp in connection.execute(
        "SELECT root,path,depth,row,stamp FROM entries ORDER BY root,path"
    ):
        count += 1
        if (
            count > MAX_TREE_ENTRIES * root_count
            or type(root) is not int
            or not 0 <= root < root_count
            or type(depth) is not int
            or not 0 < depth <= 128
            or any(
                not isinstance(value, bytes) or len(value) > 2 * 1024 * 1024
                for value in (path, row, stamp)
            )
        ):
            raise InfiniDyskV2MigrationError(
                "The cleanup manifest rows are invalid or exceed their bound."
            )
        for value in (str(root).encode(), path, str(depth).encode(), row, stamp):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()


def _mount_id(descriptor):
    for line in Path(f"/proc/self/fdinfo/{descriptor}").read_text().splitlines():
        if line.startswith("mnt_id:"):
            return line.split(":", 1)[1].strip()
    raise InfiniDyskV2MigrationError("The cleanup mount identity is unavailable.")


@contextmanager
def _directory(path):
    path = Path(path)
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise InfiniDyskV2MigrationError("The cleanup root is invalid.")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in path.parts[1:]:
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _parent(root_fd, relative, expected_mount):
    path = Path(relative)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise InfiniDyskV2MigrationError("The cleanup manifest path is invalid.")
    descriptor = os.dup(root_fd)
    try:
        for name in path.parts[:-1]:
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
            if _mount_id(descriptor) != expected_mount:
                raise InfiniDyskV2MigrationError(
                    "A foreign mount entered the cleanup tree; deletion stopped."
                )
        yield descriptor, path.name
    finally:
        os.close(descriptor)


class InfiniDyskV2Cleanup:
    def __init__(self, manager):
        self.manager = manager
        self._thread = None
        self._active_id = None

    def has_blocking_job(self, job=None):
        return bool(
            self._active_id
            and self._thread is not None
            and self._thread.is_alive()
            and (job is None or self._active_id == job.get("job_id"))
        )

    def has_recovery_pending_job(self):
        # Cleanup never requests v1 restoration or prevents a healthy v2 boot.
        return False

    def public_state(self, job):
        cleanup = copy.deepcopy(job.get("cleanup") or {})
        if cleanup.get("state") in RUNNING and not self.has_blocking_job(job):
            cleanup.update(
                state="failed",
                message="Cleanup was interrupted; review a fresh preview before resuming.",
                resumable=bool(cleanup.get("manifest_ready")),
            )
        public = {
            key: cleanup[key]
            for key in (
                "state",
                "message",
                "error",
                "started_at",
                "updated_at",
                "completed_at",
                "deleted_entries",
                "total_entries",
                "reclaim_estimate_bytes",
                "resumable",
            )
            if key in cleanup
        }
        possible = bool(
            job.get("status") == "completed"
            and job.get("mode") == "cutover"
            and (job.get("plan") or {}).get("mode") == "migrate"
            and (job.get("result") or {}).get("validated") is True
            and cleanup.get("state") != "completed"
            and not self.has_blocking_job(job)
            and (
                job.get("rollback_retired") is not True
                or cleanup.get("manifest_ready") is True
            )
        )
        return {
            "can_cleanup": possible,
            "rollback_retired": job.get("rollback_retired") is True,
            "cleanup": public or None,
        }

    def _job(self, job_id):
        job = self.manager._read(self.manager._job_path(job_id))
        if (
            not job
            or job.get("job_id") != job_id
            or job.get("job_type") != "infinidysk_v1_to_v2"
            or type(job.get("protocol")) is not int
            or job.get("protocol") != 1
            or job.get("status") != "completed"
            or job.get("mode") != "cutover"
            or (job.get("plan") or {}).get("mode") != "migrate"
            or (job.get("result") or {}).get("validated") is not True
            or (job.get("result") or {}).get("execution_verified") is not True
            or (job.get("accepted_import") or {}).get("validated") is not True
            or not job.get("snapshot")
            or not job.get("activation_fingerprint")
        ):
            raise InfiniDyskV2MigrationError(
                "Cleanup requires a retained, fully verified completed v2 cutover."
            )
        plan_fingerprint = job.get("plan_fingerprint")
        snapshot = job["snapshot"]
        if (
            not isinstance(plan_fingerprint, str)
            or len(plan_fingerprint) != 64
            or (job.get("accepted_import") or {}).get("plan_fingerprint")
            != plan_fingerprint
            or (job.get("result") or {}).get("plan_fingerprint") != plan_fingerprint
            or snapshot.get("snapshot_fingerprint")
            != _digest(snapshot.get("original_fingerprints") or {})
            or job["accepted_import"].get("snapshot_fingerprint")
            != snapshot.get("snapshot_fingerprint")
        ):
            raise InfiniDyskV2MigrationError(
                "The accepted import and retained source proofs do not identify this cutover."
            )
        if (job.get("cleanup") or {}).get("state") == "completed":
            raise InfiniDyskV2MigrationError("This v1 cleanup is already complete.")
        if (
            job.get("rollback_retired") is True
            and (job.get("cleanup") or {}).get("manifest_ready") is not True
        ):
            raise InfiniDyskV2MigrationError(
                "Retired cleanup lacks its immutable manifest; manual review is required."
            )
        return job

    def _roots(self, job):
        source = job["plan"]["source"]
        fingerprints = job["snapshot"].get("original_fingerprints") or {}
        candidates = {}
        for role, key, identity in (
            ("v1_data", "data_dir", source.get("data_identity")),
            (
                "v1_runtime",
                "runtime_dir",
                (source.get("runtime_identity") or {}).get("directory"),
            ),
        ):
            path = _path(source[key])
            if not identity or not fingerprints.get(str(path)):
                raise InfiniDyskV2MigrationError(
                    "The retained job lacks original root ownership or fingerprint proof."
                )
            row = candidates.setdefault(
                str(path),
                {
                    "path": str(path),
                    "roles": [],
                    "identity": identity,
                    "published_paths": [],
                },
            )
            if row["identity"] != identity:
                raise InfiniDyskV2MigrationError("Original root identities disagree.")
            row["roles"].append(role)
            published_key = (
                "published_data_dir" if key == "data_dir" else "published_runtime_dir"
            )
            published = source.get(published_key) or str(path)
            if published not in row["published_paths"]:
                row["published_paths"].append(published)
        roots = []
        for path, row in sorted(
            candidates.items(), key=lambda item: len(Path(item[0]).parts)
        ):
            owner = next(
                (item for item in roots if Path(item["path"]) in Path(path).parents),
                None,
            )
            if owner:
                owner["roles"].extend(row["roles"])
                owner["published_paths"].extend(
                    item
                    for item in row["published_paths"]
                    if item not in owner["published_paths"]
                )
            else:
                roots.append(row)
        return roots

    def _aliases(self, job):
        aliases = job["plan"]["source"].get("aliases") or []
        for alias in aliases:
            if alias.get("path") not in {"/infinidysk", "/nzbdav"}:
                raise InfiniDyskV2MigrationError(
                    "An original source alias is unmanaged."
                )
            path = Path(alias["path"])
            if (
                not path.exists()
                and not path.is_symlink()
                and job.get("rollback_retired")
            ):
                continue
            if (
                not path.is_symlink()
                or _identity(path) != alias.get("link_identity")
                or os.readlink(path) != alias.get("link")
            ):
                raise InfiniDyskV2MigrationError("An original source alias changed.")
        return aliases

    def _current(self, job, roots, *, worker=False):
        try:
            return self._inspect_current(job, roots, worker=worker)
        except BackupStorageError:
            raise InfiniDyskV2MigrationError(
                "The retained backup destination or local control filesystem is unavailable or unsafe."
            ) from None

    def _inspect_current(self, job, roots, *, worker=False):
        manager = self.manager
        require_local_path(manager.state_root)
        config = manager._config()
        service = config.get("infinidysk") or {}
        target = job["plan"]["target"]
        if (
            generation(service) != "2"
            or _path(resolve_v2_data_dir(service, config.get("data_root", "/data")))
            != _path(target["data_dir"])
            or _path(runtime_dir(service)) != _path(target["runtime_dir"])
        ):
            raise InfiniDyskV2MigrationError(
                "The current native installation no longer matches this cutover."
            )
        if (
            manager._process_identity(service.get("process_name") or "InfiniDysk")
            is None
        ):
            raise InfiniDyskV2MigrationError("The managed v2 process must be running.")
        if native_api_request(service, "GET", "/healthz").get(
            "status"
        ) != 200 or not delivery_mount_ready(
            service, (config.get("rclone") or {}).get("instances") or {}
        ):
            raise InfiniDyskV2MigrationError(
                "Native health and the selected delivery mount must be ready."
            )
        if not worker and self.has_blocking_job():
            raise InfiniDyskV2MigrationError("A cleanup worker is still active.")
        for other in manager._jobs():
            if (
                other.get("job_id") != job["job_id"]
                and manager._effective_status(other) in ACTIVE | RECOVERY
            ):
                raise InfiniDyskV2MigrationError(
                    "Another migration requires attention."
                )
        protected = [
            _path(manager.state_root),
            _path(target["data_dir"]),
            _path(target["runtime_dir"]),
            _path(target["mount_path"]),
            _path(job["snapshot"]["source_snapshot"]),
        ]
        cold = job["snapshot"].get("cold_archive") or {}
        if cold.get("reference"):
            from utils.backup_storage import get_backup_storage

            storage = get_backup_storage(manager.config_manager)
            profile = cold.get("backup_destination_id")
            protected.append(_path(storage.bound_identity(profile)["path"]))
        records = mount_records()
        for root in roots:
            path = Path(root["path"])
            if any(_overlap(path, item) for item in protected):
                raise InfiniDyskV2MigrationError(
                    "A v1 root overlaps active v2 data, control state or retained backups."
                )
            if any(
                path == Path(row["path"]) or path in Path(row["path"]).parents
                for row in records
            ):
                raise InfiniDyskV2MigrationError(
                    "A source root or descendant is mounted; cleanup will not detach it."
                )
            actual = _identity(path)
            if actual != root["identity"] and not (
                actual is None and job.get("rollback_retired")
            ):
                raise InfiniDyskV2MigrationError("An original v1 root was replaced.")

        # V2 deliberately retains these inactive v1 settings. Every other
        # managed configuration reference into a retired tree blocks removal.
        ignored = {("infinidysk", "config_dir"), ("infinidysk", "env", "CONFIG_PATH")}
        aliases = [Path(item["path"]) for item in self._aliases(job)]
        retired = [Path(item["path"]) for item in roots] + aliases

        def references(value, keys=()):
            if keys in ignored:
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    references(item, (*keys, key))
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    references(item, (*keys, index))
            elif isinstance(value, str):
                for token in _reference_tokens(value):
                    local_path = _local_reference_path(token)
                    if local_path is not None:
                        path = Path("/" + os.path.normpath(local_path).lstrip("/"))
                        if any(
                            path == root or root in path.parents for root in retired
                        ):
                            raise InfiniDyskV2MigrationError(
                                "Another configured path still references the v1 installation."
                            )

        references(config)
        try:
            effective_database = native_database_url(
                service, config.get("data_root") or "/data"
            )
        except (OSError, ValueError):
            raise InfiniDyskV2MigrationError(
                "The effective native database selection could not be verified safely."
            ) from None
        references(effective_database)
        return {
            "config": _digest(config),
            # Native database.toml can change without managed configuration.
            # Bind its effective selection without retaining any DSN or secret.
            "native_database": hashlib.sha256(effective_database.encode()).hexdigest(),
            "roots": roots,
            "aliases": self._aliases(job),
        }

    def _backup(self, job, *, verify=False):
        try:
            return self._inspect_backup(job, verify=verify)
        except BackupStorageError:
            raise InfiniDyskV2MigrationError(
                "The retained backup storage identity is unavailable or unsafe."
            ) from None

    def _inspect_backup(self, job, *, verify=False):
        snapshot = job["snapshot"]
        cold = snapshot.get("cold_archive") or {}
        if cold:
            if verify:
                from utils.backup_storage import get_backup_storage
                from utils.infinidysk_v2_archive import verify_retained_archive

                proof = verify_retained_archive(
                    snapshot,
                    job["plan"],
                    get_backup_storage(self.manager.config_manager),
                )
                if (
                    proof.get("verified") is not True
                    or proof.get("original_fingerprints")
                    != snapshot.get("original_fingerprints")
                    or proof.get("archive_sha256") != cold.get("sha256")
                    or proof.get("archive_reference") != cold.get("reference")
                ):
                    raise InfiniDyskV2MigrationError(
                        "The retained v1 archive proof did not match this cutover."
                    )
            return [
                {
                    "role": "cold_archive",
                    "reference": cold.get("reference"),
                    "kept": True,
                }
            ]
        source = job["plan"]["source"]
        copies = {
            source["data_dir"]: snapshot.get("data_snapshot"),
            source["runtime_dir"]: snapshot.get("runtime_snapshot"),
        }
        result = []
        backup_root = _path(snapshot["source_snapshot"])
        require_local_path(backup_root)
        for original, expected in snapshot["original_fingerprints"].items():
            backup = copies.get(original)
            if not backup or _identity(_path(backup)) is None:
                raise InfiniDyskV2MigrationError("A retained v1 backup is unavailable.")
            backup_path = _path(backup)
            if backup_root not in backup_path.parents or any(
                _overlap(backup_path, Path(root["path"])) for root in self._roots(job)
            ):
                raise InfiniDyskV2MigrationError(
                    "The retained backup is not an independent owned snapshot."
                )
            if (
                verify
                and _source_fingerprint(
                    backup,
                    scratch_root=snapshot["source_snapshot"],
                    role="Retained v1 backup",
                )
                != expected
            ):
                raise InfiniDyskV2MigrationError("A retained v1 backup did not verify.")
            result.append({"role": "v1_backup", "path": str(backup), "kept": True})
        if source.get("provider") in {"postgres", "postgresql"}:
            postgres = snapshot.get("postgres_snapshot") or {}
            binding = (postgres.get("source_binding") or {}).get("binding_fingerprint")
            restore = postgres.get("restore_proof") or {}
            if (
                postgres.get("verified_restore") is not True
                or postgres.get("source_read_only") is not True
                or not isinstance(binding, str)
                or not binding
                or restore.get("verified") is not True
                or restore.get("source_binding") != binding
            ):
                raise InfiniDyskV2MigrationError(
                    "The retained PostgreSQL backup lacks its matching independent restore proof."
                )
            for path_key, hash_key, role in (
                ("dump_path", "dump_sha256", "postgres_dump"),
                ("staged_sqlite", "staged_sha256", "postgres_validation_database"),
            ):
                path = _path(postgres.get(path_key))
                expected = postgres.get(hash_key)
                if (
                    backup_root not in path.parents
                    or not isinstance(expected, str)
                    or len(expected) != 64
                ):
                    raise InfiniDyskV2MigrationError(
                        "The retained PostgreSQL backup is not an owned independent artifact."
                    )
                with _directory(path.parent) as parent_fd:
                    descriptor = os.open(
                        path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd
                    )
                    with os.fdopen(descriptor, "rb") as handle:
                        value = os.fstat(handle.fileno())
                        if (
                            not stat.S_ISREG(value.st_mode)
                            or value.st_uid != os.geteuid()
                            or value.st_nlink != 1
                            or stat.S_IMODE(value.st_mode) & 0o077
                        ):
                            raise InfiniDyskV2MigrationError(
                                "A retained PostgreSQL backup artifact is unsafe."
                            )
                        if verify and (
                            _hash_file(handle) != expected
                            or _stamp(value) != _stamp(os.fstat(handle.fileno()))
                        ):
                            raise InfiniDyskV2MigrationError(
                                "A retained PostgreSQL backup artifact did not verify."
                            )
                result.append({"role": role, "path": str(path), "kept": True})
        return result

    def preview(self, request):
        job = self._job(request.get("job_id"))
        roots = self._roots(job)
        blockers, targets, binding, kept = [], [], None, []
        try:
            binding = self._current(job, roots)
            kept = self._backup(job)
            for root in roots:
                summary = _source_summary(root["path"], role="Retained v1 cleanup")
                targets.append(
                    {
                        **root,
                        "entries": summary["entries"],
                        "logical_bytes": summary["bytes"],
                        "reclaim_estimate_bytes": summary["bytes"],
                    }
                )
            for alias in self._aliases(job):
                targets.append(
                    {
                        "path": alias["path"],
                        "roles": ["v1_alias"],
                        "published_paths": [alias["path"]],
                        "entries": 1,
                        "logical_bytes": 0,
                        "reclaim_estimate_bytes": 0,
                    }
                )
        except InfiniDyskV2MigrationError as error:
            blockers.append(str(error))
        except (OSError, ValueError):
            blockers.append(
                "Current v2 ownership, retained backups, source paths or mount safety could not be established. No files were removed."
            )
        token = secrets.token_urlsafe(32)
        public_targets = [
            {key: value for key, value in row.items() if key != "identity"}
            for row in targets
        ]
        preview = {
            "protocol": 1,
            "job_id": job["job_id"],
            "preview_token": token,
            "expires_at": int(time.time()) + TTL,
            "ready": not blockers,
            "confirmation_text": f"RETIRE INFINIDYSK V1 {job['job_id']}",
            "acknowledgements_required": list(ACKS),
            "targets": public_targets,
            "kept_backups": kept,
            "reclaim_estimate_bytes": sum(
                row["reclaim_estimate_bytes"] for row in targets
            ),
            "blockers": blockers,
            "resumable": job.get("rollback_retired") is True,
            "rollback_will_be_retired": True,
        }
        self.manager._write(
            self.manager.state_root / "cleanup-previews" / f"{token}.json",
            {"preview": preview, "binding": binding, "backup_binding": _digest(kept)},
        )
        return preview

    def start(self, request):
        token = request.get("preview_token")
        if (
            not isinstance(token, str)
            or len(token) != 43
            or any(not (c.isalnum() or c in "-_") for c in token)
        ):
            raise InfiniDyskV2MigrationError("Review a fresh cleanup preview.")
        retained = self.manager._read(
            self.manager.state_root / "cleanup-previews" / f"{token}.json"
        )
        preview = (retained or {}).get("preview") or {}
        if (
            preview.get("job_id") != request.get("job_id")
            or preview.get("ready") is not True
            or preview.get("expires_at", 0) <= time.time()
            or request.get("confirmation") != preview.get("confirmation_text")
            or not isinstance(request.get("acknowledgements"), dict)
            or any(request["acknowledgements"].get(key) is not True for key in ACKS)
        ):
            raise InfiniDyskV2MigrationError(
                "Confirm a current cleanup preview and all playback, import and rollback acknowledgements."
            )
        job = self._job(request.get("job_id"))
        roots = self._roots(job)
        binding = self._current(job, roots)
        if binding != retained.get("binding") or _digest(
            self._backup(job)
        ) != retained.get("backup_binding"):
            raise InfiniDyskV2MigrationError(
                "Cleanup ownership changed; review a fresh preview."
            )
        update_lock = getattr(self.manager.updater, "updating", None)
        if update_lock is None or not update_lock.acquire(blocking=False):
            raise InfiniDyskV2MigrationError("A service update is active.")
        try:
            with self.manager._lock:
                if self.has_blocking_job():
                    raise InfiniDyskV2MigrationError(
                        "A cleanup worker is still active."
                    )
                prior = job.get("cleanup") or {}
                job["cleanup"] = {
                    **prior,
                    "state": "queued",
                    "message": "Verifying retained backups and the exact v1 deletion manifest.",
                    "started_at": prior.get("started_at") or int(time.time()),
                    "updated_at": int(time.time()),
                    "reclaim_estimate_bytes": preview["reclaim_estimate_bytes"],
                    "worker_id": self.manager._worker_id,
                    "resumable": job.get("rollback_retired") is True,
                    "review_binding": binding,
                }
                self.manager._write(self.manager._job_path(job["job_id"]), job)
                self._active_id = job["job_id"]
                self._thread = threading.Thread(
                    target=self._run,
                    args=(job, roots, update_lock),
                    name="infinidysk-v1-cleanup",
                    daemon=True,
                )
                self._thread.start()
        except BaseException:
            update_lock.release()
            raise
        return {"job": self.manager._public_job(job)}

    def _progress(self, job, state, message, **fields):
        with self.manager._lock:
            job["cleanup"].update(
                state=state, message=message, updated_at=int(time.time()), **fields
            )
            self.manager._write(self.manager._job_path(job["job_id"]), job)

    def _manifest(self, job, roots):
        parent = self.manager._directory(
            self.manager.state_root / "cleanup" / job["job_id"]
        )
        require_local_path(parent)
        path = parent / f"{secrets.token_hex(16)}.sqlite"
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        os.close(descriptor)
        total = 0
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA page_size=4096")
            connection.execute(
                f"PRAGMA max_page_count={MAX_TREE_MANIFEST_BYTES // 4096}"
            )
            connection.execute("PRAGMA cache_size=-2048")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(TABLE_SQL)
            connection.execute(INDEX_SQL)
            with connection:
                for index, root in enumerate(roots):
                    with _directory(root["path"]) as root_fd:
                        mount_id = _mount_id(root_fd)
                        with closing(
                            iter_tree(root["path"], content=True, role="V1 cleanup")
                        ) as rows:
                            for row in rows:
                                with _parent(root_fd, row["path"], mount_id) as (
                                    parent_fd,
                                    name,
                                ):
                                    value = os.stat(
                                        name, dir_fd=parent_fd, follow_symlinks=False
                                    )
                                    if (
                                        stat.S_ISREG(value.st_mode)
                                        and value.st_nlink != 1
                                    ):
                                        raise InfiniDyskV2MigrationError(
                                            "A v1 file has shared hard links; cleanup requires manual ownership review."
                                        )
                                    if (
                                        value.st_mode != row["mode"]
                                        or value.st_mtime_ns != row["mtime_ns"]
                                        or (
                                            "size" in row
                                            and value.st_size != row["size"]
                                        )
                                    ):
                                        raise InfiniDyskV2MigrationError(
                                            "A v1 entry changed while cleanup was inventoried."
                                        )
                                    connection.execute(
                                        "INSERT INTO entries VALUES (?,?,?,?,?)",
                                        (
                                            index,
                                            row["path"].encode(
                                                "utf-8", "surrogatepass"
                                            ),
                                            len(Path(row["path"]).parts),
                                            json.dumps(
                                                row,
                                                sort_keys=True,
                                                separators=(",", ":"),
                                            ).encode(),
                                            json.dumps(_stamp(value)).encode(),
                                        ),
                                    )
                                    total += 1
                    digest, separator = hashlib.sha256(b"["), b""
                    for (row,) in connection.execute(
                        "SELECT row FROM entries WHERE root=? ORDER BY path", (index,)
                    ):
                        digest.update(separator)
                        digest.update(row)
                        separator = b","
                    digest.update(b"]")
                    expected = job["snapshot"]["original_fingerprints"][root["path"]]
                    if digest.hexdigest() != expected:
                        raise InfiniDyskV2MigrationError(
                            "The v1 source differs from its accepted original fingerprint."
                        )
            entries_fingerprint = _entries_digest(connection, len(roots))
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
            fingerprint = _hash_file(handle)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return {
            "manifest_path": str(path),
            "manifest_sha256": fingerprint,
            "manifest_entries_sha256": entries_fingerprint,
            "manifest_ready": True,
            "total_entries": total,
        }

    def _open_manifest(self, job):
        cleanup = job["cleanup"]
        parent = self.manager.state_root / "cleanup" / job["job_id"]
        path = _path(cleanup.get("manifest_path"))
        if path.parent != _path(parent) or path.suffix != ".sqlite":
            raise InfiniDyskV2MigrationError(
                "The retained cleanup manifest is not owned."
            )
        require_local_path(path)
        value = path.lstat()
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != os.geteuid()
            or value.st_nlink != 1
            or stat.S_IMODE(value.st_mode) & 0o077
            or value.st_size > MAX_TREE_MANIFEST_BYTES
        ):
            raise InfiniDyskV2MigrationError("The cleanup manifest identity is unsafe.")
        with path.open("rb") as handle:
            if _stamp(os.fstat(handle.fileno())) != _stamp(value) or _hash_file(
                handle
            ) != cleanup.get("manifest_sha256"):
                raise InfiniDyskV2MigrationError(
                    "The immutable cleanup manifest changed."
                )
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 2 * 1024 * 1024)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA cache_size=-2048")
            connection.execute("BEGIN")
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            page_count = connection.execute("PRAGMA page_count").fetchone()[0]
            if page_size * page_count > MAX_TREE_MANIFEST_BYTES:
                raise InfiniDyskV2MigrationError(
                    "The opened cleanup manifest exceeds its storage bound."
                )
            schema = sorted(
                connection.execute(
                    "SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 3"
                )
            )
            if schema != sorted(
                [
                    ("entries", "table", TABLE_SQL),
                    ("deletion_order", "index", INDEX_SQL),
                ]
            ):
                raise InfiniDyskV2MigrationError("The cleanup manifest schema changed.")
            if _entries_digest(connection, len(self._roots(job))) != cleanup.get(
                "manifest_entries_sha256"
            ):
                raise InfiniDyskV2MigrationError(
                    "The opened cleanup whitelist did not match its approved rows."
                )
            # Keep this exact read transaction and SQLite-owned descriptor for
            # every subsequent deletion query. A second pathname open cannot
            # substitute a different whitelist after the file hash check.
            return connection
        except BaseException:
            connection.close()
            raise

    def _delete(self, job, roots):
        deleted = 0
        with closing(self._open_manifest(job)) as connection:
            for index, root in enumerate(roots):
                path = Path(root["path"])
                if not path.exists():
                    continue
                with _directory(path) as root_fd:
                    expected_mount = _mount_id(root_fd)
                    if _identity(path) != root["identity"]:
                        raise InfiniDyskV2MigrationError(
                            "The v1 root changed before deletion."
                        )
                    for raw_path, raw_row, raw_stamp in connection.execute(
                        "SELECT path,row,stamp FROM entries WHERE root=? ORDER BY depth DESC,path",
                        (index,),
                    ):
                        relative = raw_path.decode("utf-8", "surrogatepass")
                        row, expected = json.loads(raw_row), json.loads(raw_stamp)
                        try:
                            with _parent(root_fd, relative, expected_mount) as (
                                parent_fd,
                                name,
                            ):
                                if _identity(path) != root["identity"]:
                                    raise InfiniDyskV2MigrationError(
                                        "The v1 root was replaced during deletion."
                                    )
                                actual = os.stat(
                                    name, dir_fd=parent_fd, follow_symlinks=False
                                )
                                if stat.S_ISDIR(row["mode"]):
                                    if [
                                        actual.st_dev,
                                        actual.st_ino,
                                        actual.st_mode,
                                    ] != expected[:3]:
                                        raise InfiniDyskV2MigrationError(
                                            "A v1 directory changed; deletion stopped."
                                        )
                                    child = os.open(
                                        name,
                                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                        dir_fd=parent_fd,
                                    )
                                    try:
                                        if _mount_id(child) != expected_mount:
                                            raise InfiniDyskV2MigrationError(
                                                "A new mount blocks v1 cleanup."
                                            )
                                        os.fsync(child)
                                    finally:
                                        os.close(child)
                                    os.rmdir(name, dir_fd=parent_fd)
                                else:
                                    if _stamp(actual) != expected:
                                        raise InfiniDyskV2MigrationError(
                                            "A v1 file changed; deletion stopped."
                                        )
                                    if stat.S_ISREG(actual.st_mode):
                                        descriptor = os.open(
                                            name,
                                            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                            dir_fd=parent_fd,
                                        )
                                        with os.fdopen(descriptor, "rb") as handle:
                                            if (
                                                _mount_id(handle.fileno())
                                                != expected_mount
                                                or _stamp(os.fstat(handle.fileno()))
                                                != expected
                                                or _hash_file(handle) != row["sha256"]
                                                or _stamp(os.fstat(handle.fileno()))
                                                != expected
                                            ):
                                                raise InfiniDyskV2MigrationError(
                                                    "A v1 file no longer matches its deletion receipt."
                                                )
                                    elif not stat.S_ISLNK(
                                        actual.st_mode
                                    ) or os.readlink(name, dir_fd=parent_fd) != row.get(
                                        "link"
                                    ):
                                        raise InfiniDyskV2MigrationError(
                                            "A v1 link changed; deletion stopped."
                                        )
                                    if (
                                        _stamp(
                                            os.stat(
                                                name,
                                                dir_fd=parent_fd,
                                                follow_symlinks=False,
                                            )
                                        )
                                        != expected
                                    ):
                                        raise InfiniDyskV2MigrationError(
                                            "A v1 entry changed before unlink."
                                        )
                                    os.unlink(name, dir_fd=parent_fd)
                                deleted += 1
                        except FileNotFoundError:
                            # Only an already retired job may resume this exact
                            # immutable manifest after an interrupted unlink.
                            continue
                        if deleted % 128 == 0:
                            self._progress(
                                job,
                                "deleting",
                                "Removing only verified v1 entries; backups are retained.",
                                deleted_entries=deleted,
                            )
                    os.fsync(root_fd)
                with _directory(path.parent) as parent_fd:
                    if _identity(path) != root["identity"]:
                        raise InfiniDyskV2MigrationError(
                            "The v1 root changed before removal."
                        )
                    os.rmdir(path.name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
        for alias in self._aliases(job):
            path = Path(alias["path"])
            with _directory(path.parent) as parent_fd:
                try:
                    if (
                        _identity(path) != alias["link_identity"]
                        or os.readlink(path.name, dir_fd=parent_fd) != alias["link"]
                    ):
                        raise InfiniDyskV2MigrationError(
                            "A legacy alias changed before removal."
                        )
                    os.unlink(path.name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except FileNotFoundError:
                    continue
        return deleted

    def _run(self, job, roots, update_lock):
        from utils.infinidysk_migration_admission import v2_migration_owner

        try:
            with v2_migration_owner("major"):
                self._progress(
                    job,
                    "verifying",
                    "Fully verifying retained backups before retiring rollback.",
                )
                self._current(job, roots, worker=True)
                self._backup(job, verify=True)
                if not job.get("rollback_retired"):
                    manifest = self._manifest(job, roots)
                    self._progress(
                        job,
                        "verifying",
                        "The original v1 tree and immutable deletion manifest verified.",
                        **manifest,
                    )
                elif not (job.get("cleanup") or {}).get("manifest_ready"):
                    raise InfiniDyskV2MigrationError(
                        "Retired cleanup lacks its original deletion manifest; manual review is required."
                    )
                self._current(job, roots, worker=True)
                # This durable checkpoint MUST precede every destructive call.
                # A failure or crash from this point never offers old rollback.
                with self.manager._lock:
                    job["rollback_retired"] = True
                    self._progress(
                        job,
                        "retiring",
                        "Automated v1 rollback is retired. Verified backups remain available.",
                        resumable=True,
                    )
                self._progress(
                    job,
                    "deleting",
                    "Removing the reviewed v1 installation; backups and PostgreSQL databases are retained.",
                )
                deleted = self._delete(job, roots)
                self._progress(
                    job,
                    "completed",
                    "The reviewed v1 installation was removed. Verified backups and the retirement receipt are retained.",
                    deleted_entries=deleted,
                    completed_at=int(time.time()),
                    resumable=False,
                )
        except Exception as error:
            message = "Cleanup stopped safely. Review a fresh preview before resuming; retained backups were not deleted."
            self._progress(
                job,
                "failed",
                message,
                error=type(error).__name__,
                resumable=bool(
                    job.get("rollback_retired")
                    and (job.get("cleanup") or {}).get("manifest_ready")
                ),
            )
        finally:
            self._active_id = None
            update_lock.release()
