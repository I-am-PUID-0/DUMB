"""Verified, disposable legacy blob input on a registered external filesystem.

The closed migration archive remains the only rollback source. This staging tree
is never substituted for that archive and is never part of the active v2 store.
"""

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time
from urllib.parse import quote

from utils.backup_storage import _directory, get_backup_storage
from utils.infinidysk_v2_archive import verify_retained_archive
from utils.infinidysk_v2_inventory import (
    MAX_TREE_MANIFEST_BYTES,
    iter_tree,
)
from utils.infinidysk_v2_space_control import identity

NAMESPACE = "infinidysk-v2-input"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


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


def _mount(descriptor):
    for line in Path(f"/proc/self/fdinfo/{descriptor}").read_text().splitlines():
        if line.startswith("mnt_id:"):
            return line.split(":", 1)[1].strip()
    raise ValueError("The migration input mount identity is unavailable.")


@contextmanager
def _parent(root, relative, *, expected=None, database=None, column="source"):
    parts = Path(relative).parts
    if (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or Path(relative).is_absolute()
    ):
        raise ValueError("A migration input path is invalid.")
    with _directory(root) as anchor:
        actual = os.fstat(anchor)
        if (
            expected is not None
            and {
                "device": actual.st_dev,
                "inode": actual.st_ino,
                "mode": stat.S_IFMT(actual.st_mode),
            }
            != expected
        ):
            raise ValueError("The owned migration input root was replaced.")
        descriptor, mount = os.dup(anchor), _mount(anchor)
        try:
            for index, name in enumerate(parts[:-1], 1):
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
                if _mount(descriptor) != mount:
                    raise ValueError("Migration input contains a mounted directory.")
                if database is not None:
                    row = database.execute(
                        f"SELECT {column} FROM entries WHERE path=?",
                        (os.fsencode(str(Path(*parts[:index]))),),
                    ).fetchone()
                    if (
                        row is None
                        or _stamp(os.fstat(descriptor))[:3] != json.loads(row[0])[:3]
                    ):
                        raise ValueError(
                            "A recorded migration input parent was replaced."
                        )
            yield descriptor, parts[-1]
        finally:
            os.close(descriptor)


def _hash_file(path, check):
    digest = hashlib.sha256()
    with _directory(path.parent) as parent:
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(descriptor, "rb") as handle:
            before = _stamp(os.fstat(handle.fileno()))
            if not stat.S_ISREG(before[2]) or before[6] != 1:
                raise ValueError(
                    "A migration input manifest is not a private regular file."
                )
            while chunk := handle.read(1024 * 1024):
                check()
                digest.update(chunk)
            if before != _stamp(os.fstat(handle.fileno())) or before != _stamp(
                os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            ):
                raise ValueError("A migration input manifest changed while being read.")
    return digest.hexdigest()


def blob_summary(path):
    """Measure actual local allocations; never claim logical size was released."""
    root = Path(path)
    if not root.is_dir() or root.is_symlink():
        raise ValueError(
            "External input staging requires a direct legacy blobs directory."
        )
    result = {"entries": 0, "logical_bytes": 0, "allocated_bytes": 0}
    root_device = root.stat().st_dev
    with closing(iter_tree(root, include_identity=True, reject_hardlinks=True)) as rows:
        for row in rows:
            result["entries"] += 1
            kind = stat.S_IFMT(row["mode"])
            if kind not in {stat.S_IFDIR, stat.S_IFREG}:
                raise ValueError(
                    "Legacy blob staging supports only direct files and directories."
                )
            with _parent(root, row["path"]) as (parent, name):
                value = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if value.st_dev != root_device:
                    raise ValueError("Legacy blob input crosses a filesystem.")
                if kind == stat.S_IFREG:
                    if value.st_nlink != 1:
                        raise ValueError("Legacy blob input contains shared hardlinks.")
                    descriptor = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
                    )
                    try:
                        if _stamp(os.fstat(descriptor)) != _stamp(value):
                            raise ValueError(
                                "Legacy blob input changed during its space estimate."
                            )
                    finally:
                        os.close(descriptor)
                    result["logical_bytes"] += value.st_size
                    result["allocated_bytes"] += min(
                        value.st_size, value.st_blocks * 512
                    )
    return result


def require_external_filesystem(source, destination):
    if Path(source).stat().st_dev == Path(destination).stat().st_dev:
        raise ValueError("Use another filesystem for external migration input.")


class ExternalBlobStaging:
    def __init__(self, manager):
        self.manager = manager

    def _save(self, job):
        self.manager._assert_job_owner(job)
        self.manager._write(self.manager._job_path(job["job_id"]), job)

    def _storage(self, job):
        storage = get_backup_storage(self.manager.config_manager)
        selected = job["plan"]["backup_storage"]
        bound = storage.bound_identity(selected["destination_id"])
        if bound["binding_fingerprint"] != selected["binding_fingerprint"]:
            raise ValueError(
                "The migration input share changed; retained recovery remains protected."
            )
        return storage, bound

    def _check(self, job, owner_check=None):
        self.manager._assert_job_owner(job)
        if owner_check is not None:
            owner_check()
        cancel = self.manager._workspace(job).get("check_cancel")
        if callable(cancel):
            cancel()

    def _record(self, job):
        record = (job.get("snapshot") or {}).get("external_blob_staging")
        if not isinstance(record, dict) or record.get("schema") != 1:
            raise ValueError("External migration input evidence is unavailable.")
        storage, bound = self._storage(job)
        expected = Path(bound["artifact_root"]) / NAMESPACE / job["job_id"]
        if (
            record.get("destination_id") != bound["destination_id"]
            or record.get("binding_fingerprint") != bound["binding_fingerprint"]
            or Path(record.get("directory") or "") != expected
            or identity(expected) != record.get("root_identity")
            or identity(expected / "blobs") != record.get("blobs_identity")
        ):
            raise ValueError(
                "The verified external input directory was replaced or is unavailable."
            )
        return record, storage, bound

    def _database(self, job, record, check):
        path = Path(record.get("manifest") or "")
        expected = (
            Path(self.manager._workspace(job)["root"]) / "external-blob-manifest.sqlite"
        )
        if (
            path != expected
            or path.is_symlink()
            or identity(path) != record.get("manifest_identity")
            or _hash_file(path, check) != record.get("manifest_sha256")
        ):
            raise ValueError("The verified migration input manifest changed.")
        return sqlite3.connect(f"file:{quote(str(path))}?mode=ro&immutable=1", uri=True)

    def _verify_tree(self, job, root, database, *, stage, check, content=True):
        record = job["snapshot"]["external_blob_staging"]
        expected_identity = (
            record["blobs_identity"] if stage else record["source_identity"]
        )
        if identity(root) != expected_identity:
            raise ValueError("A verified migration input root changed.")
        with _directory(root) as descriptor:
            value = os.fstat(descriptor)
            if {
                "device": value.st_dev,
                "inode": value.st_ino,
                "mode": stat.S_IFMT(value.st_mode),
            } != expected_identity:
                raise ValueError(
                    "The verified migration input root changed while opening it."
                )
        count = 0
        with closing(
            iter_tree(root, include_identity=True, reject_hardlinks=True)
        ) as rows:
            for row in rows:
                check()
                count += 1
                found = database.execute(
                    "SELECT payload,source,staged FROM entries WHERE path=?",
                    (os.fsencode(row["path"]),),
                ).fetchone()
                if found is None:
                    raise ValueError(
                        "Unrecorded files appeared in the migration input."
                    )
                payload, source_stamp, staged_stamp = map(json.loads, found)
                with _parent(
                    root,
                    row["path"],
                    expected=expected_identity,
                    database=database,
                    column="staged" if stage else "source",
                ) as (parent, name):
                    value = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    wanted = staged_stamp if stage else source_stamp
                    if _stamp(value) != wanted:
                        raise ValueError("A recorded migration input file changed.")
                    if stat.S_ISREG(value.st_mode) and content:
                        descriptor = os.open(
                            name,
                            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=parent,
                        )
                        try:
                            if _stamp(os.fstat(descriptor)) != wanted:
                                raise ValueError(
                                    "Migration input changed while opening it."
                                )
                            digest = hashlib.sha256()
                            while chunk := os.read(descriptor, 1024 * 1024):
                                check()
                                digest.update(chunk)
                            if (
                                _stamp(os.fstat(descriptor)) != wanted
                                or digest.hexdigest() != payload["sha256"]
                            ):
                                raise ValueError(
                                    "Migration input content did not verify."
                                )
                        finally:
                            os.close(descriptor)
        expected_count = database.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        if count != expected_count:
            raise ValueError("Migration input files disappeared after verification.")
        if identity(root) != expected_identity:
            raise ValueError("Migration input root changed during verification.")

    def prepare(self, job, workspace, snapshot, owner_check):
        plan = job["plan"]
        if not plan.get("options", {}).get("offload_legacy_blobs"):
            return
        recovery = snapshot.get("space_recovery") or {}
        if (
            not plan["options"].get("reclaim_source_space")
            or recovery.get("native_mutations_authorized") is not True
            or recovery.get("state") != "reused"
        ):
            raise ValueError(
                "External input needs the verified archive-backed local candidate first."
            )
        self._check(job, owner_check)
        self.manager._assert_stopped(plan)
        storage, bound = self._storage(job)
        verify_retained_archive(snapshot, plan, storage)
        source = Path(workspace["candidate_data"]) / "blobs"
        summary = blob_summary(source)
        require_external_filesystem(source, bound["artifact_root"])
        private = Path(workspace["root"])
        manifest = private / "external-blob-manifest.sqlite"
        if (
            manifest.exists()
            or manifest.is_symlink()
            or snapshot.get("external_blob_staging")
        ):
            raise ValueError(
                "This job already has external input; recover it before starting another copy."
            )
        directory = Path(bound["artifact_root"]) / NAMESPACE / job["job_id"]
        record = {
            "schema": 1,
            "state": "creating",
            "destination_id": bound["destination_id"],
            "binding_fingerprint": bound["binding_fingerprint"],
            "directory": str(directory),
            "root_identity": None,
            "blobs_identity": None,
            "source": str(source),
            "source_identity": identity(source),
            "manifest": str(manifest),
            **summary,
            "released_bytes": 0,
        }
        snapshot["external_blob_staging"] = record
        job["snapshot"] = snapshot
        self._save(job)
        uid, gid = self.manager.executor._execution_ids()
        last = float("-inf")
        copied = 0

        def check():
            self._check(job, owner_check)

        def progress(stage, message):
            nonlocal last
            check()
            now = time.monotonic()
            if now - last >= 1:
                record["copied_bytes"] = copied
                self.manager._progress(job, stage, message)
                last = now

        with storage._lease(bound["destination_id"], NAMESPACE, create=True) as (
            _profile,
            namespace,
            lease_check,
            allowed,
        ):
            if directory.exists() or directory.is_symlink():
                raise ValueError(
                    "The external input path is occupied; nothing was replaced."
                )
            os.mkdir(job["job_id"], 0o700, dir_fd=namespace)
            os.fsync(namespace)
            stage_fd = os.open(
                job["job_id"],
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=namespace,
            )
            try:
                value = os.fstat(stage_fd)
                record["root_identity"] = {
                    "device": value.st_dev,
                    "inode": value.st_ino,
                    "mode": stat.S_IFMT(value.st_mode),
                }
                self._save(job)
                os.mkdir("blobs", 0o700, dir_fd=stage_fd)
                os.fsync(stage_fd)
                blobs_fd = os.open(
                    "blobs",
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=stage_fd,
                )
                try:
                    value = os.fstat(blobs_fd)
                    record["blobs_identity"] = {
                        "device": value.st_dev,
                        "inode": value.st_ino,
                        "mode": stat.S_IFMT(value.st_mode),
                    }
                finally:
                    os.close(blobs_fd)
                self._save(job)
            finally:
                os.close(stage_fd)
            descriptor = os.open(
                manifest, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            os.close(descriptor)
            with closing(sqlite3.connect(manifest)) as database:
                database.execute("PRAGMA journal_mode=DELETE")
                database.execute("PRAGMA synchronous=FULL")
                database.execute("PRAGMA cache_size=-2048")
                database.execute(
                    f"PRAGMA max_page_count={MAX_TREE_MANIFEST_BYTES // 4096}"
                )
                database.execute(
                    "CREATE TABLE entries(path BLOB PRIMARY KEY,payload BLOB,source BLOB,staged BLOB) WITHOUT ROWID"
                )
                record["state"] = "copying"
                self._save(job)
                with closing(
                    iter_tree(source, include_identity=True, reject_hardlinks=True)
                ) as rows:
                    for row in rows:
                        check()
                        lease_check()
                        relative = row["path"]
                        with _parent(
                            source,
                            relative,
                            expected=record["source_identity"],
                            database=database,
                        ) as (parent, name):
                            value = os.stat(name, dir_fd=parent, follow_symlinks=False)
                            source_stamp = _stamp(value)
                            payload = {
                                "path": relative,
                                "kind": stat.S_IFMT(value.st_mode),
                            }
                            if stat.S_ISDIR(value.st_mode):
                                with _parent(
                                    directory / "blobs",
                                    relative,
                                    expected=record["blobs_identity"],
                                    database=database,
                                    column="staged",
                                ) as (target_parent, target_name):
                                    os.mkdir(target_name, 0o700, dir_fd=target_parent)
                                    staged_stamp = _stamp(
                                        os.stat(
                                            target_name,
                                            dir_fd=target_parent,
                                            follow_symlinks=False,
                                        )
                                    )
                                    os.fsync(target_parent)
                            elif stat.S_ISREG(value.st_mode) and value.st_nlink == 1:
                                payload["size"] = value.st_size
                                src = os.open(
                                    name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent
                                )
                                try:
                                    if _stamp(os.fstat(src)) != source_stamp:
                                        raise ValueError(
                                            "Legacy input changed before its copy."
                                        )
                                    with _parent(
                                        directory / "blobs",
                                        relative,
                                        expected=record["blobs_identity"],
                                        database=database,
                                        column="staged",
                                    ) as (target_parent, target_name):
                                        dst = os.open(
                                            target_name,
                                            os.O_WRONLY
                                            | os.O_CREAT
                                            | os.O_EXCL
                                            | os.O_NOFOLLOW,
                                            0o600,
                                            dir_fd=target_parent,
                                        )
                                    try:
                                        digest, amount = hashlib.sha256(), 0
                                        while chunk := os.read(src, 1024 * 1024):
                                            check()
                                            lease_check()
                                            pending = memoryview(chunk)
                                            while pending:
                                                written = os.write(dst, pending)
                                                if written <= 0:
                                                    raise OSError(
                                                        "The migration input copy could not write more data."
                                                    )
                                                pending = pending[written:]
                                            digest.update(chunk)
                                            amount += len(chunk)
                                            copied += len(chunk)
                                            progress(
                                                "staging_blobs",
                                                "Copying legacy input to the migration backup location.",
                                            )
                                        os.fsync(dst)
                                        if (
                                            amount != value.st_size
                                            or _stamp(os.fstat(src)) != source_stamp
                                        ):
                                            raise ValueError(
                                                "Legacy input changed during its copy."
                                            )
                                        payload["sha256"] = digest.hexdigest()
                                        staged_stamp = _stamp(os.fstat(dst))
                                    finally:
                                        os.close(dst)
                                finally:
                                    os.close(src)
                            else:
                                raise ValueError(
                                    "Legacy input contains a link or unsupported file."
                                )
                        database.execute(
                            "INSERT INTO entries VALUES (?,?,?,?)",
                            (
                                os.fsencode(relative),
                                _json(payload),
                                _json(source_stamp),
                                _json(staged_stamp),
                            ),
                        )
                        if copied > summary["logical_bytes"]:
                            raise ValueError(
                                "Legacy input grew after its space estimate."
                            )
                # Make only the staged tree readable to the contained configured
                # user. The registered namespace remains controller-owned.
                for relative, recorded in database.execute(
                    "SELECT path,staged FROM entries ORDER BY length(path) DESC"
                ):
                    with _parent(
                        directory / "blobs",
                        os.fsdecode(relative),
                        expected=record["blobs_identity"],
                        database=database,
                        column="staged",
                    ) as (parent, name):
                        descriptor = os.open(
                            name,
                            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=parent,
                        )
                        try:
                            value = os.fstat(descriptor)
                            if _stamp(value)[:3] != json.loads(recorded)[:3]:
                                raise ValueError(
                                    "A staged input entry was replaced before metadata preparation."
                                )
                            try:
                                os.fchown(descriptor, uid, gid)
                                os.fchmod(
                                    descriptor,
                                    0o700 if stat.S_ISDIR(value.st_mode) else 0o600,
                                )
                            except PermissionError:
                                if not allowed:
                                    raise
                            value = os.fstat(descriptor)
                            if not (
                                (value.st_uid == uid and value.st_mode & stat.S_IRUSR)
                                or (
                                    value.st_gid == gid and value.st_mode & stat.S_IRGRP
                                )
                                or value.st_mode & stat.S_IROTH
                            ):
                                raise ValueError(
                                    "The contained importer cannot read staged legacy input."
                                )
                            if _stamp(
                                os.stat(name, dir_fd=parent, follow_symlinks=False)
                            ) != _stamp(value):
                                raise ValueError(
                                    "A staged input entry changed while preparing it."
                                )
                            database.execute(
                                "UPDATE entries SET staged=? WHERE path=?",
                                (_json(_stamp(value)), relative),
                            )
                        finally:
                            os.close(descriptor)
                with _directory(directory / "blobs") as blobs_fd:
                    value = os.fstat(blobs_fd)
                    if {
                        "device": value.st_dev,
                        "inode": value.st_ino,
                        "mode": stat.S_IFMT(value.st_mode),
                    } != record["blobs_identity"]:
                        raise ValueError("The staged input root was replaced.")
                    try:
                        os.fchown(blobs_fd, uid, gid)
                        os.fchmod(blobs_fd, 0o700)
                    except PermissionError:
                        if not allowed:
                            raise
                    os.fsync(blobs_fd)
                database.commit()
                self.manager._progress(
                    job,
                    "verifying_blob_staging",
                    "Reading back the complete staged legacy input before freeing local files.",
                )
                self._verify_tree(
                    job, directory / "blobs", database, stage=True, check=check
                )
                self._verify_tree(
                    job, source, database, stage=False, check=check, content=False
                )
            with manifest.open("rb") as handle:
                os.fsync(handle.fileno())
            record.update(
                state="verified",
                manifest_identity=identity(manifest),
                manifest_sha256=_hash_file(manifest, check),
            )
            self._save(job)
            lease_check()
            # Reverify the archive immediately before any local file is removed.
            verify_retained_archive(snapshot, plan, storage)
            self.manager._assert_stopped(plan)
            record["state"] = "releasing"
            self._save(job)
            before_free = shutil.disk_usage(source).free
            with closing(self._database(job, record, check)) as database:
                for relative, payload, original in database.execute(
                    "SELECT path,payload,source FROM entries ORDER BY length(path) DESC,path DESC"
                ):
                    check()
                    lease_check()
                    relative, payload, original = (
                        os.fsdecode(relative),
                        json.loads(payload),
                        json.loads(original),
                    )
                    with _parent(
                        source,
                        relative,
                        expected=record["source_identity"],
                        database=database,
                    ) as (parent, name):
                        value = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        if stat.S_ISREG(value.st_mode):
                            if _stamp(value) != original or value.st_nlink != 1:
                                raise ValueError(
                                    "A local legacy blob changed before release."
                                )
                            os.unlink(name, dir_fd=parent)
                        elif stat.S_ISDIR(value.st_mode):
                            if [value.st_dev, value.st_ino, value.st_mode] != original[
                                :3
                            ]:
                                raise ValueError(
                                    "A local input directory was replaced before release."
                                )
                            os.rmdir(name, dir_fd=parent)
                        else:
                            raise ValueError(
                                "A local legacy input was replaced by a link."
                            )
                        os.fsync(parent)
                    progress(
                        "releasing_local_blobs",
                        "Freeing only the verified local legacy input files.",
                    )
            record.update(
                state="released",
                released_bytes=min(
                    summary["allocated_bytes"],
                    max(0, shutil.disk_usage(source).free - before_free),
                ),
                release_observed_free_bytes=shutil.disk_usage(source).free,
            )
            self._save(job)
            return directory / "blobs"

    def verify(self, job, owner_check=None):
        record, storage, _bound = self._record(job)
        if record.get("state") not in {"released", "verified"}:
            raise ValueError("External migration input is not completely verified.")
        check = lambda: self._check(job, owner_check)
        with storage._lease(record["destination_id"], NAMESPACE) as (
            _profile,
            _directory_fd,
            lease_check,
            _allowed,
        ):
            lease_check()
            with closing(self._database(job, record, check)) as database:
                self._verify_tree(
                    job,
                    Path(record["directory"]) / "blobs",
                    database,
                    stage=True,
                    check=check,
                )
            lease_check()
        return Path(record["directory"]) / "blobs"

    def cleanup(self, job, owner_check=None):
        record = (job.get("snapshot") or {}).get("external_blob_staging")
        if not record or record.get("state") == "cleaned":
            return
        record, storage, _bound = self._record(job)
        check = lambda: self._check(job, owner_check)
        # Never walk an unrecorded or changed tree during cleanup.
        with storage._lease(record["destination_id"], NAMESPACE) as (
            _profile,
            namespace,
            lease_check,
            _allowed,
        ):
            with closing(self._database(job, record, check)) as database:
                self._verify_tree(
                    job,
                    Path(record["directory"]) / "blobs",
                    database,
                    stage=True,
                    check=check,
                )
                root = Path(record["directory"]) / "blobs"
                for relative, staged in database.execute(
                    "SELECT path,staged FROM entries ORDER BY length(path) DESC,path DESC"
                ):
                    check()
                    lease_check()
                    with _parent(
                        root,
                        os.fsdecode(relative),
                        expected=record["blobs_identity"],
                        database=database,
                        column="staged",
                    ) as (parent, name):
                        value = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        expected = json.loads(staged)
                        if stat.S_ISDIR(value.st_mode):
                            if _stamp(value)[:3] != expected[:3]:
                                raise ValueError(
                                    "A staged input directory was replaced before cleanup."
                                )
                            os.rmdir(name, dir_fd=parent)
                        elif stat.S_ISREG(value.st_mode) and _stamp(value) == expected:
                            os.unlink(name, dir_fd=parent)
                        else:
                            raise ValueError(
                                "A staged input file was replaced before cleanup."
                            )
                        os.fsync(parent)
                stage_fd = os.open(
                    job["job_id"],
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=namespace,
                )
                try:
                    value = os.fstat(stage_fd)
                    if {
                        "device": value.st_dev,
                        "inode": value.st_ino,
                        "mode": stat.S_IFMT(value.st_mode),
                    } != record["root_identity"]:
                        raise ValueError(
                            "External input ownership changed before cleanup."
                        )
                    os.rmdir("blobs", dir_fd=stage_fd)
                    os.fsync(stage_fd)
                finally:
                    os.close(stage_fd)
                os.rmdir(job["job_id"], dir_fd=namespace)
                os.fsync(namespace)
            record["state"] = "cleaned"
            self._save(job)
