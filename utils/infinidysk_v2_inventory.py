"""Complete bounded-memory inventories without loading DUMB configuration."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

MAX_TREE_ENTRIES = 2_000_000
MAX_TREE_DEPTH = 128
MAX_TREE_MANIFEST_BYTES = 512 * 1024 * 1024


class InventoryError(RuntimeError):
    """The complete source inventory could not be established safely."""


def _file_identity(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _open_directory(name, relative, depth, expected, parent_fd=None):
    descriptor = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
    )
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_mode) != (
            expected.st_dev,
            expected.st_ino,
            expected.st_mode,
        ):
            raise InventoryError("An inventory directory changed while being opened.")
        iterator = os.scandir(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, iterator, relative, depth


def _close_directory(frame):
    try:
        frame[1].close()
    finally:
        os.close(frame[0])


def _content_hash(name, directory_fd, expected):
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(
            expected
        ):
            raise InventoryError("An inventory file changed while being opened.")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if _file_identity(os.fstat(descriptor)) != _file_identity(opened):
            raise InventoryError("An inventory file changed while being read.")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def iter_tree(path, *, content=False, role="Source"):
    """Yield every descendant without following links or collecting directory lists."""
    root = Path(path)
    stack = []
    count = 0
    try:
        try:
            expected = root.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(expected.st_mode):
            raise InventoryError(f"{role} inventory root is not a real directory.")
        stack.append(_open_directory(root, Path(), 0, expected))
        while stack:
            directory_fd, iterator, relative, depth = stack[-1]
            try:
                entry = next(iterator)
            except StopIteration:
                _close_directory(stack.pop())
                continue
            count += 1
            if count > MAX_TREE_ENTRIES:
                raise InventoryError(
                    f"{role} inventory exceeds the supported bound of "
                    f"{MAX_TREE_ENTRIES:,} entries (observed at least {count:,}); "
                    "no entries were skipped."
                )
            value = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
            item_relative = relative / entry.name
            row = {
                "path": str(item_relative),
                "mode": value.st_mode,
                "mtime_ns": value.st_mtime_ns,
            }
            if not stat.S_ISDIR(value.st_mode):
                row["size"] = value.st_size
            if stat.S_ISLNK(value.st_mode):
                row["link"] = os.readlink(entry.name, dir_fd=directory_fd)
            elif stat.S_ISREG(value.st_mode):
                if content:
                    row["sha256"] = _content_hash(entry.name, directory_fd, value)
            elif stat.S_ISDIR(value.st_mode):
                if depth + 1 > MAX_TREE_DEPTH:
                    raise InventoryError(
                        f"{role} inventory exceeds the supported directory depth "
                        f"of {MAX_TREE_DEPTH}; no entries were skipped."
                    )
            else:
                raise InventoryError(f"{role} inventory contains a special file.")
            yield row
            if stat.S_ISDIR(value.st_mode):
                stack.append(
                    _open_directory(
                        entry.name, item_relative, depth + 1, value, directory_fd
                    )
                )
    except OSError as error:
        raise InventoryError(
            f"{role} inventory could not be read completely "
            f"({type(error).__name__}); no entries were skipped."
        ) from error
    finally:
        while stack:
            _close_directory(stack.pop())


def validate_data_link(root, row):
    """Relocated live data must keep every link inside its own copied tree."""
    if "link" not in row:
        return
    root = Path(root)
    target = Path(row["link"])
    if target.is_absolute():
        raise InventoryError(
            "Source data contains an absolute symbolic link; copied live data "
            "must remain independent of retained originals."
        )
    depth = len(Path(row["path"]).parent.parts)
    for component in target.parts:
        if component == "..":
            depth -= 1
            if depth < 0:
                raise InventoryError(
                    "Source data contains a symbolic link that leaves its root "
                    "before re-entry."
                )
        elif component != ".":
            depth += 1
    try:
        (root / row["path"]).resolve().relative_to(root)
    except (ValueError, RuntimeError, OSError):
        raise InventoryError(
            "Source data contains an escaping or cyclic symbolic link."
        ) from None


def tree_summary(path, *, validate_links=False, role="Source"):
    """Metadata-only scan; neither a manifest nor file contents are read."""
    summary = {"entries": 0, "bytes": 0}
    with closing(iter_tree(path, role=role)) as entries:
        for row in entries:
            if validate_links:
                validate_data_link(path, row)
            summary["entries"] += 1
            if stat.S_ISREG(row["mode"]):
                summary["bytes"] += row["size"]
    return summary


def tree_fingerprint(path, *, scratch_root, validate_links=False, role="Source"):
    """Stream the exact legacy sorted JSON-array digest using private disk ordering."""
    root = Path(path).resolve()
    scratch = Path(scratch_root).resolve()
    if root == scratch or root in scratch.parents:
        raise InventoryError(
            "Fingerprint scratch must remain outside the scanned tree."
        )
    pages = MAX_TREE_MANIFEST_BYTES // 4096
    if pages < 2:
        raise InventoryError("The fingerprint manifest storage bound is too small.")
    try:
        with tempfile.TemporaryDirectory(
            prefix=".inventory-", dir=scratch
        ) as temporary:
            database = Path(temporary) / "manifest.sqlite"
            descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA page_size=4096")
                connection.execute(f"PRAGMA max_page_count={pages}")
                connection.execute("PRAGMA cache_size=-2048")
                connection.execute("PRAGMA journal_mode=OFF")
                connection.execute("PRAGMA synchronous=OFF")
                # WITHOUT ROWID provides the complete ordering directly; there is
                # no temporary external sort or uncontrolled SQLite spill file.
                connection.execute(
                    "CREATE TABLE manifest (path BLOB PRIMARY KEY, row BLOB NOT NULL) "
                    "WITHOUT ROWID"
                )
                with (
                    connection,
                    closing(iter_tree(path, content=True, role=role)) as entries,
                ):
                    for row in entries:
                        if validate_links:
                            validate_data_link(path, row)
                        connection.execute(
                            "INSERT INTO manifest VALUES (?, ?)",
                            (
                                row["path"].encode("utf-8", "surrogatepass"),
                                json.dumps(
                                    row, sort_keys=True, separators=(",", ":")
                                ).encode(),
                            ),
                        )
                digest = hashlib.sha256(b"[")
                separator = b""
                for (row,) in connection.execute(
                    "SELECT row FROM manifest ORDER BY path"
                ):
                    digest.update(separator)
                    digest.update(row)
                    separator = b","
                digest.update(b"]")
                return digest.hexdigest()
    except (OSError, sqlite3.Error) as error:
        raise InventoryError(
            f"{role} fingerprint manifest could not be completed within its "
            f"{MAX_TREE_MANIFEST_BYTES // (1024 * 1024):,} MiB storage bound."
        ) from error
