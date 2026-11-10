"""Identity-bound disposal of a stopped, validated rehearsal candidate only."""

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

from utils.backup_storage import _directory as _owned_directory, require_local_path
from utils.infinidysk_v2_inventory import MAX_TREE_MANIFEST_BYTES, iter_tree


class RehearsalCleanupError(ValueError):
    pass


def _candidate_mount_id(descriptor):
    for line in Path(f"/proc/self/fdinfo/{descriptor}").read_text().splitlines():
        if line.startswith("mnt_id:"):
            return line.split(":", 1)[1].strip()
    raise RehearsalCleanupError(
        "The rehearsal candidate mount identity is unavailable."
    )


def _candidate_identity(value):
    return [value.st_dev, value.st_ino, value.st_mode]


@contextmanager
def _candidate_parent(root_fd, relative, database, mount):
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or ".." in parts:
        raise RehearsalCleanupError("The rehearsal disposal manifest path is invalid.")
    descriptor = os.dup(root_fd)
    try:
        for index, name in enumerate(parts[:-1], start=1):
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
            row = database.execute(
                "SELECT identity FROM entries WHERE path=?",
                (os.fsencode(str(Path(*parts[:index]))),),
            ).fetchone()
            if (
                row is None
                or _candidate_identity(os.fstat(descriptor)) != json.loads(row[0])
                or _candidate_mount_id(descriptor) != mount
            ):
                raise RehearsalCleanupError(
                    "A rehearsal candidate directory changed; disposal stopped."
                )
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def _check_candidate_entry(parent, name, expected, mount, device):
    descriptor = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent)
    try:
        value = os.fstat(descriptor)
        if (
            _candidate_identity(value) != expected
            or value.st_dev != device
            or _candidate_mount_id(descriptor) != mount
            or (stat.S_ISREG(value.st_mode) and value.st_nlink != 1)
            or not (
                stat.S_ISREG(value.st_mode)
                or stat.S_ISDIR(value.st_mode)
                or stat.S_ISLNK(value.st_mode)
            )
        ):
            raise RehearsalCleanupError(
                "A rehearsal candidate entry is foreign, mounted or changed; disposal stopped."
            )
        if (
            _candidate_identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
            != expected
        ):
            raise RehearsalCleanupError(
                "A rehearsal candidate entry changed before removal."
            )
    finally:
        os.close(descriptor)


def discard_rehearsal_candidate(plan, workspace, snapshot, assert_unmounted):
    """Remove only a complete, identity-bound disposable rehearsal tree."""
    candidate = Path(workspace["candidate_data"])
    target = Path(plan["target"]["data_dir"])
    private = Path(workspace["root"])
    expected = snapshot.get("candidate_identity") or {}
    protected = [target, private] + [
        Path(value)
        for value in (
            plan["source"].get("data_dir"),
            plan["source"].get("runtime_dir"),
        )
        if value
    ]
    if (
        not re.fullmatch(r"[a-f0-9]{32}", private.name)
        or not candidate.is_absolute()
        or ".." in candidate.parts
        or not target.is_absolute()
        or ".." in target.parts
        or candidate != target.parent / f".{target.name}.dumb-v2-{private.name}"
        or any(
            candidate == value
            or candidate in value.parents
            or value in candidate.parents
            for value in protected
        )
        or any(
            type(expected.get(key)) is not int for key in ("device", "inode", "mode")
        )
        or expected.get("mode") != stat.S_IFDIR
    ):
        raise RehearsalCleanupError(
            "The rehearsal candidate ownership is incomplete; its data was retained."
        )
    require_local_path(private)
    assert_unmounted(str(candidate))
    with _owned_directory(candidate.parent) as parent:
        root = os.open(
            candidate.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        try:
            mount = _candidate_mount_id(root)

            def check_root():
                for value in (
                    os.fstat(root),
                    os.stat(candidate.name, dir_fd=parent, follow_symlinks=False),
                ):
                    if (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode)) != (
                        expected["device"],
                        expected["inode"],
                        expected["mode"],
                    ):
                        raise RehearsalCleanupError(
                            "The rehearsal candidate root changed; disposal stopped."
                        )
                if _candidate_mount_id(parent) != mount:
                    raise RehearsalCleanupError(
                        "The rehearsal candidate is mounted; its data was retained."
                    )

            check_root()
            with tempfile.TemporaryDirectory(
                prefix=".rehearsal-disposal-", dir=private
            ) as temporary:
                manifest = Path(temporary) / "entries.sqlite"
                descriptor = os.open(
                    manifest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                os.close(descriptor)
                with closing(sqlite3.connect(manifest)) as database:
                    database.execute("PRAGMA page_size=4096")
                    database.execute(
                        f"PRAGMA max_page_count={MAX_TREE_MANIFEST_BYTES // 4096}"
                    )
                    database.execute("PRAGMA cache_size=-2048")
                    database.execute("PRAGMA journal_mode=OFF")
                    database.execute(
                        "CREATE TABLE entries(path BLOB PRIMARY KEY,depth INTEGER,identity BLOB) WITHOUT ROWID"
                    )
                    database.execute(
                        "CREATE INDEX deletion_order ON entries(depth DESC,path)"
                    )
                    # Complete preflight precedes every destructive operation.
                    with closing(iter_tree(candidate, include_identity=True)) as rows:
                        for row in rows:
                            check_root()
                            identity = [
                                row["identity"]["device"],
                                row["identity"]["inode"],
                                row["mode"],
                            ]
                            with _candidate_parent(
                                root, row["path"], database, mount
                            ) as (directory, name):
                                _check_candidate_entry(
                                    directory,
                                    name,
                                    identity,
                                    mount,
                                    expected["device"],
                                )
                            database.execute(
                                "INSERT INTO entries VALUES(?,?,?)",
                                (
                                    os.fsencode(row["path"]),
                                    len(Path(row["path"]).parts),
                                    json.dumps(identity).encode(),
                                ),
                            )
                    check_root()
                    assert_unmounted(str(candidate))
                    for path, identity in database.execute(
                        "SELECT path,identity FROM entries ORDER BY depth DESC,path"
                    ):
                        check_root()
                        identity = json.loads(identity)
                        with _candidate_parent(
                            root, os.fsdecode(path), database, mount
                        ) as (directory, name):
                            _check_candidate_entry(
                                directory, name, identity, mount, expected["device"]
                            )
                            if stat.S_ISDIR(identity[2]):
                                os.rmdir(name, dir_fd=directory)
                            else:
                                os.unlink(name, dir_fd=directory)
                    check_root()
                    os.rmdir(candidate.name, dir_fd=parent)
        finally:
            os.close(root)
