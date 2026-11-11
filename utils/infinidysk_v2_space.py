"""Identity-bound restoration of a verified v1 archive into local storage.

The controller owns destination creation, durable checkpoints and disposal.
This helper never removes entries or adopts a nonempty directory. Interrupted
restores remain in their recorded owned directory for explicit recovery.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tarfile

from utils.backup_storage import _directory, require_local_path
from utils.infinidysk_v2_archive import (
    ArchiveError,
    _BoundedTarInfo,
    _DigestReader,
    _insert,
    _manifest,
    _record_from_header,
    _safe_name,
    verify_retained_archive,
)
from utils.infinidysk_v2_inventory import (
    MAX_TREE_DEPTH,
    MAX_TREE_ENTRIES,
    tree_fingerprint,
    validate_data_link,
)


def _identity(value):
    return [value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode)]


def _mount_id(descriptor):
    for line in Path(f"/proc/self/fdinfo/{descriptor}").read_text().splitlines():
        if line.startswith("mnt_id:"):
            return line.split(":", 1)[1].strip()
    raise ArchiveError("The archive recovery mount identity is unavailable.")


@contextmanager
def _parent(root, relative, database, mount):
    parts = relative.split("/")
    if len(parts) > MAX_TREE_DEPTH + 1:
        raise ArchiveError("An archive recovery path exceeds the supported depth.")
    descriptor = os.dup(root)
    try:
        for index, name in enumerate(parts[:-1], start=1):
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
            path = "/".join(parts[:index]).encode("utf-8", "surrogatepass")
            row = database.execute(
                "SELECT expected FROM entries WHERE path=?", (path,)
            ).fetchone()
            if (
                row is None
                or _identity(os.fstat(descriptor)) != json.loads(row[0])
                or _mount_id(descriptor) != mount
            ):
                raise ArchiveError(
                    "An archive recovery directory was replaced or mounted."
                )
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def _metadata(parent, name, record, expected, mount):
    descriptor = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent)
    try:
        value = os.fstat(descriptor)
        if _identity(value) != expected or _mount_id(descriptor) != mount:
            raise ArchiveError("An archive recovery entry was replaced or mounted.")
        if (value.st_uid, value.st_gid) != (record["uid"], record["gid"]):
            os.chown(
                name,
                record["uid"],
                record["gid"],
                dir_fd=parent,
                follow_symlinks=False,
            )
        if not stat.S_ISLNK(record["mode"]):
            os.chmod(
                name, stat.S_IMODE(record["mode"]), dir_fd=parent, follow_symlinks=False
            )
        os.utime(
            name,
            ns=(record["mtime_ns"], record["mtime_ns"]),
            dir_fd=parent,
            follow_symlinks=False,
        )
        actual = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            _identity(actual) != expected
            or actual.st_uid != record["uid"]
            or actual.st_gid != record["gid"]
            or actual.st_mode != record["mode"]
            or actual.st_mtime_ns != record["mtime_ns"]
        ):
            raise ArchiveError("Archive recovery ownership or metadata did not verify.")
    finally:
        os.close(descriptor)


def restore_archive_tree(
    snapshot,
    plan,
    storage,
    *,
    source,
    destination,
    expected_identity,
    owner_check=None,
):
    """Restore a source into its owned empty original or derived candidate path.

    A private candidate allows the controller to verify the complete restore
    before atomically publishing it at the original source path.
    The caller must record the newly created destination's identity durably
    before entering this function. A failed restore is deliberately retained;
    only the controller's identity-bound recovery may discard that partial tree.
    """
    source, destination = str(source), Path(destination)
    allowed = {
        (plan.get("source") or {}).get(key) for key in ("data_dir", "runtime_dir")
    }
    private = Path(snapshot.get("source_snapshot") or "")
    target = Path((plan.get("target") or {}).get("data_dir") or "")
    candidate = target.parent / f".{target.name}.dumb-v2-{private.name}"
    private_candidate = (
        source == (plan.get("source") or {}).get("data_dir")
        and private.is_absolute()
        and target.is_absolute()
        and ".." not in private.parts
        and ".." not in target.parts
        and re.fullmatch(r"[a-f0-9]{32}", private.name) is not None
        and destination == candidate
        and all(
            candidate != item
            and candidate not in item.parents
            and item not in candidate.parents
            for item in [private, target] + [Path(value) for value in allowed if value]
        )
    )
    roots = snapshot.get("archive_roots") or {}
    fingerprints = snapshot.get("original_fingerprints") or {}
    if (
        source not in allowed
        or source not in roots
        or source not in fingerprints
        or (str(destination) != source and not private_candidate)
        or not destination.is_absolute()
        or ".." in destination.parts
        or destination == Path("/")
        or not isinstance(expected_identity, dict)
        or any(
            type(expected_identity.get(key)) is not int
            for key in ("device", "inode", "mode")
        )
        or expected_identity["mode"] != stat.S_IFDIR
    ):
        raise ArchiveError(
            "The archive recovery source or destination ownership is incomplete."
        )
    check_owner = owner_check or (lambda: None)
    require_local_path(destination)
    require_local_path(snapshot["source_snapshot"])
    expected = [expected_identity[key] for key in ("device", "inode", "mode")]
    with _directory(destination.parent) as parent:
        root = os.open(
            destination.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        try:
            mount = _mount_id(root)

            def check_root():
                check_owner()
                if (
                    _identity(os.fstat(root)) != expected
                    or _identity(
                        os.stat(destination.name, dir_fd=parent, follow_symlinks=False)
                    )
                    != expected
                    or _mount_id(root) != mount
                    or _mount_id(parent) != mount
                ):
                    raise ArchiveError(
                        "The owned archive recovery root changed or is mounted."
                    )

            check_root()
            with os.scandir(root) as entries:
                if next(entries, None) is not None:
                    raise ArchiveError(
                        "Archive recovery requires its owned empty directory."
                    )
            # Verify the entire archive, including unrelated roots and extras,
            # before writing any file or changing destination metadata.
            proof = verify_retained_archive(snapshot, plan, storage)
            check_root()
            receipt = snapshot["cold_archive"]
            prefix = "trees/" + roots[source]
            restored = 0
            root_record = None
            with _manifest(snapshot["source_snapshot"]) as database:
                # Keep both ancestor lookups and postorder metadata application
                # inside the same bounded SQLite file, without external sorting.
                database.execute("CREATE UNIQUE INDEX recovery_paths ON entries(path)")
                database.execute(
                    "CREATE INDEX recovery_order ON entries(length(path) DESC,path DESC)"
                )
                with storage.open_artifact(receipt["reference"]) as handle:
                    reader = _DigestReader(handle)
                    with tarfile.open(
                        fileobj=reader,
                        mode="r|",
                        bufsize=1024 * 1024,
                        tarinfo=_BoundedTarInfo,
                        encoding="utf-8",
                        errors="surrogateescape",
                    ) as archive:
                        while (info := archive.next()) is not None:
                            archive.members.clear()
                            _safe_name(info.name)
                            if info.name != prefix and not info.name.startswith(
                                prefix + "/"
                            ):
                                continue
                            check_root()
                            record = _record_from_header(info)
                            relative = record.get("relative")
                            if not relative:
                                if root_record is not None or not info.isdir():
                                    raise ArchiveError(
                                        "The archive recovery root is duplicate or invalid."
                                    )
                                root_record = record
                                continue
                            if root_record is None:
                                raise ArchiveError(
                                    "The archive recovery root header is missing."
                                )
                            _safe_name(relative)
                            restored += 1
                            if restored > MAX_TREE_ENTRIES:
                                raise ArchiveError(
                                    "The archive recovery inventory exceeds its bound."
                                )
                            with _parent(root, relative, database, mount) as (
                                directory,
                                name,
                            ):
                                if info.isdir():
                                    os.mkdir(name, 0o700, dir_fd=directory)
                                elif info.issym():
                                    if source == (plan.get("source") or {}).get(
                                        "data_dir"
                                    ):
                                        validate_data_link(
                                            destination,
                                            {
                                                "path": relative,
                                                "link": record["link"],
                                            },
                                        )
                                    os.symlink(record["link"], name, dir_fd=directory)
                                elif info.isreg():
                                    descriptor = os.open(
                                        name,
                                        os.O_WRONLY
                                        | os.O_CREAT
                                        | os.O_EXCL
                                        | os.O_NOFOLLOW,
                                        0o600,
                                        dir_fd=directory,
                                    )
                                    with os.fdopen(descriptor, "wb") as output:
                                        payload = archive.extractfile(info)
                                        if payload is None:
                                            raise ArchiveError(
                                                "An archive recovery payload is unreadable."
                                            )
                                        digest, total = hashlib.sha256(), 0
                                        with payload:
                                            while chunk := payload.read(1024 * 1024):
                                                check_root()
                                                output.write(chunk)
                                                digest.update(chunk)
                                                total += len(chunk)
                                        output.flush()
                                        os.fsync(output.fileno())
                                    if total != record["size"]:
                                        raise ArchiveError(
                                            "An archive recovery file is incomplete."
                                        )
                                    record["sha256"] = digest.hexdigest()
                                else:
                                    raise ArchiveError(
                                        "An archive recovery entry is unsupported."
                                    )
                                created = os.stat(
                                    name, dir_fd=directory, follow_symlinks=False
                                )
                                if created.st_dev != expected[0]:
                                    raise ArchiveError(
                                        "An archive recovery entry crosses a filesystem."
                                    )
                                entry_identity = _identity(created)
                                _insert(database, record, expected=entry_identity)
                                if not info.isdir():
                                    _metadata(
                                        directory, name, record, entry_identity, mount
                                    )
                    while reader.read(1024 * 1024):
                        pass
                    if (
                        reader.digest.hexdigest() != receipt["sha256"]
                        or reader.size != receipt["size_bytes"]
                    ):
                        raise ArchiveError("The archive changed during local recovery.")
                if root_record is None:
                    raise ArchiveError(
                        "The requested source tree is absent from the archive."
                    )
                # Restore children before parents, retaining private writable
                # permissions while their descendants are still being created.
                for payload, identity in database.execute(
                    "SELECT payload,expected FROM entries ORDER BY length(path) DESC,path DESC"
                ):
                    record = json.loads(payload)
                    if stat.S_ISDIR(record["mode"]):
                        check_root()
                        with _parent(root, record["relative"], database, mount) as (
                            directory,
                            name,
                        ):
                            _metadata(
                                directory, name, record, json.loads(identity), mount
                            )
                check_root()
                _metadata(parent, destination.name, root_record, expected, mount)
                os.fsync(root)
                os.fsync(parent)
            check_root()
            actual = tree_fingerprint(
                destination,
                scratch_root=snapshot["source_snapshot"],
                validate_links=source == (plan.get("source") or {}).get("data_dir"),
                role="Restored v1 source",
            )
            check_root()
            if actual != fingerprints[source]:
                raise ArchiveError(
                    "The recovered source tree differs from its retained fingerprint."
                )
            return {
                **proof,
                "source": source,
                "restored": True,
                "fingerprint": actual,
                "entries": restored,
                "root_identity": dict(expected_identity),
            }
        finally:
            os.close(root)
