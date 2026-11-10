"""Closed, checksummed installation archives; no configuration import or extraction API.

Only regular files, directories and literal symbolic links are represented.
Hardlinks are flattened. Members are verified before any destination is changed.
"""

from contextlib import closing, contextmanager
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import tarfile
import tempfile

from utils.backup_storage import mount_records, require_local_path
from utils.infinidysk_v2_archive import _BoundedTarInfo, ArchiveError as _TarSafetyError
from utils.infinidysk_v2_inventory import (
    MAX_TREE_DEPTH,
    MAX_TREE_ENTRIES,
    MAX_TREE_MANIFEST_BYTES,
    _open_directory,
    _close_directory,
    _content_hash,
    _file_identity,
)

FORMAT = "dumb-installation-backup"
SCHEMA = 1
MAX_METADATA = 4 * 1024 * 1024
MAX_RECORD = 65536
FOOTER = "installation-manifest.jsonl"


class InstallationArchiveError(RuntimeError):
    """The complete archive cannot be proved safe."""


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def identity(value):
    return [value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode)]


def safe_member(name):
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise InstallationArchiveError("Invalid archive member path.")
    path = PurePosixPath(name)
    if path.is_absolute() or any(x in {"", ".", ".."} for x in name.split("/")):
        raise InstallationArchiveError(
            "Archive traversal or noncanonical path refused."
        )
    if (
        len(name.encode("utf-8", "surrogatepass")) > 16384
        or len(path.parts) > MAX_TREE_DEPTH + 3
    ):
        raise InstallationArchiveError(
            "Archive member path exceeds its resource bound."
        )
    return name


@contextmanager
def manifest(scratch):
    require_local_path(scratch)
    with tempfile.TemporaryDirectory(
        prefix=".installation-manifest-", dir=scratch
    ) as temporary:
        with closing(sqlite3.connect(Path(temporary) / "entries.sqlite")) as database:
            database.execute("PRAGMA journal_mode=OFF")
            database.execute("PRAGMA temp_store=MEMORY")
            database.execute("PRAGMA cache_size=-2048")
            database.execute(f"PRAGMA max_page_count={MAX_TREE_MANIFEST_BYTES // 4096}")
            database.execute(
                "CREATE TABLE entries (name BLOB PRIMARY KEY, payload BLOB NOT NULL, source BLOB, stamp BLOB, matched INTEGER NOT NULL DEFAULT 0) WITHOUT ROWID"
            )
            yield database


def _insert(database, row, source=None, stamp=None):
    safe_member(row["name"])
    if len(canonical(row)) > MAX_RECORD:
        raise InstallationArchiveError("An archive record exceeds its bounded size.")
    try:
        database.execute(
            "INSERT INTO entries(name,payload,source,stamp) VALUES(?,?,?,?)",
            (
                row["name"].encode("utf-8", "surrogatepass"),
                canonical(row),
                os.fsencode(source) if source is not None else None,
                canonical(stamp) if stamp is not None else None,
            ),
        )
    except (sqlite3.Error, UnicodeError) as error:
        raise InstallationArchiveError(
            "The bounded archive inventory is full or contains duplicate members."
        ) from error


def _row(name, value, link=None):
    if (
        min(value.st_uid, value.st_gid) < 0
        or max(value.st_uid, value.st_gid) > 2**31 - 1
        or abs(value.st_mtime_ns) > 2**63 - 1
    ):
        raise InstallationArchiveError(
            "A persistent entry has metadata outside the supported restore range."
        )
    if stat.S_IMODE(value.st_mode) & ~0o777:
        raise InstallationArchiveError(
            "A persistent entry uses privileged permission bits that cannot be restored safely."
        )
    row = {
        "name": safe_member(name),
        "mode": stat.S_IFMT(value.st_mode) | stat.S_IMODE(value.st_mode),
        "uid": value.st_uid,
        "gid": value.st_gid,
        "mtime_ns": value.st_mtime_ns,
        "size": value.st_size if stat.S_ISREG(value.st_mode) else 0,
    }
    if stat.S_ISLNK(value.st_mode):
        if link is None or "\x00" in link or len(os.fsencode(link)) > 16384:
            raise InstallationArchiveError("Unsafe symbolic link metadata.")
        row["link"] = link
    elif not (stat.S_ISDIR(value.st_mode) or stat.S_ISREG(value.st_mode)):
        raise InstallationArchiveError(
            "A persistent source contains an unsupported special file."
        )
    return row


def source_rows(root, exclusions=(), *, content=False):
    """Descriptor walk; never follows a link or enters a nested mount."""
    root_path = Path(root["path"])
    excluded = {os.path.normpath(str(x)) for x in exclusions}
    nested_mounts = {os.path.normpath(x["path"]) for x in mount_records()}
    stack = []
    count = 0
    try:
        value = root_path.lstat()
        if root.get("identity") and identity(value) != root["identity"]:
            raise InstallationArchiveError("A persistent root was replaced.")
        if os.listxattr(root_path, follow_symlinks=False):
            raise InstallationArchiveError(
                "A persistent entry has extended attributes/ACLs; complete metadata preservation is unsupported."
            )
        link = os.readlink(root_path) if stat.S_ISLNK(value.st_mode) else None
        first = _row("roots/" + root["id"], value, link)
        if content and stat.S_ISREG(value.st_mode):
            with open_leaf(root_path) as (parent, leaf):
                first["sha256"] = _content_hash(leaf, parent, value)
        yield first, root_path, list(_file_identity(value))
        if not stat.S_ISDIR(value.st_mode):
            return
        stack.append(_open_directory(root_path, Path(), 0, value))
        while stack:
            descriptor, iterator, relative, depth = stack[-1]
            try:
                entry = next(iterator)
            except StopIteration:
                _close_directory(stack.pop())
                continue
            path = root_path / relative / entry.name
            if os.path.normpath(str(path)) in excluded:
                continue
            if os.path.normpath(str(path)) in nested_mounts:
                raise InstallationArchiveError(
                    "A persistent tree contains a nested mount; its coverage must be resolved before backup."
                )
            count += 1
            if count > MAX_TREE_ENTRIES:
                raise InstallationArchiveError(
                    "Installation inventory exceeds its bounded work budget; no entries were skipped."
                )
            value = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
            if os.listxattr(path, follow_symlinks=False):
                raise InstallationArchiveError(
                    "A persistent entry has extended attributes/ACLs; complete metadata preservation is unsupported."
                )
            item_relative = relative / entry.name
            row = _row(
                f"roots/{root['id']}/{item_relative}",
                value,
                (
                    os.readlink(entry.name, dir_fd=descriptor)
                    if stat.S_ISLNK(value.st_mode)
                    else None
                ),
            )
            if root.get("link_only") and stat.S_ISREG(value.st_mode):
                if path.suffix.lower() != ".strm" or value.st_size > 1024 * 1024:
                    raise InstallationArchiveError(
                        "A link namespace contains payload files; media is excluded and complete link-only coverage cannot be proved."
                    )
            if content and stat.S_ISREG(value.st_mode):
                row["sha256"] = _content_hash(entry.name, descriptor, value)
            yield row, path, list(_file_identity(value))
            if stat.S_ISDIR(value.st_mode):
                if depth + 1 > MAX_TREE_DEPTH:
                    raise InstallationArchiveError(
                        "Installation inventory exceeds its supported directory depth."
                    )
                stack.append(
                    _open_directory(
                        entry.name, item_relative, depth + 1, value, descriptor
                    )
                )
    except OSError as error:
        raise InstallationArchiveError(
            "A persistent tree could not be read completely."
        ) from error
    finally:
        while stack:
            _close_directory(stack.pop())


@contextmanager
def open_leaf(path):
    path = Path(path)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            replacement = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = replacement
        yield descriptor, path.name
    finally:
        os.close(descriptor)


def inventory(roots, scratch, exclusions=(), *, content=False):
    entries = size = 0
    digest = hashlib.sha256()
    with manifest(scratch) as database:
        for root in roots:
            with closing(source_rows(root, exclusions, content=content)) as rows:
                for row, source, stamp in rows:
                    entries += 1
                    if entries > MAX_TREE_ENTRIES:
                        raise InstallationArchiveError(
                            "The complete installation entry budget was exceeded."
                        )
                    size += row["size"]
                    _insert(database, row, source, stamp)
        for (payload,) in database.execute("SELECT payload FROM entries ORDER BY name"):
            digest.update(payload + b"\n")
    return {
        "entries": entries,
        "logical_bytes": size,
        "fingerprint": digest.hexdigest(),
    }


def _header(row):
    info = tarfile.TarInfo(row["name"])
    mode = row["mode"]
    info.type = (
        tarfile.DIRTYPE
        if stat.S_ISDIR(mode)
        else tarfile.SYMTYPE if stat.S_ISLNK(mode) else tarfile.REGTYPE
    )
    info.mode = stat.S_IMODE(mode)
    info.uid, info.gid, info.size = row["uid"], row["gid"], row["size"]
    info.linkname = row.get("link", "")
    nanoseconds = row["mtime_ns"]
    seconds, remainder = divmod(abs(nanoseconds), 1_000_000_000)
    info.mtime = int(nanoseconds // 1_000_000_000)
    info.pax_headers = {
        "DUMB.mtime_ns": str(nanoseconds),
        "mtime": f"{'-' if nanoseconds < 0 else ''}{seconds}.{remainder:09d}",
    }
    return info


def _add(archive, row, handle=None):
    archive.addfile(_header(row), handle)
    archive.members.clear()


class _Rows:
    def __init__(self, database):
        self.rows = iter(database.execute("SELECT payload FROM entries ORDER BY name"))
        self.pending = b""

    def read(self, amount):
        while len(self.pending) < amount:
            row = next(self.rows, None)
            if row is None:
                break
            self.pending += row[0] + b"\n"
        result, self.pending = self.pending[:amount], self.pending[amount:]
        return result


class _HashReader:
    def __init__(self, descriptor):
        self.descriptor, self.digest, self.size = descriptor, hashlib.sha256(), 0

    def read(self, amount):
        chunk = os.read(self.descriptor, amount)
        self.digest.update(chunk)
        self.size += len(chunk)
        return chunk


def write_archive(handle, metadata, roots, scratch, exclusions=(), owner_check=None):
    if len(canonical(metadata)) > MAX_METADATA:
        raise InstallationArchiveError("Installation metadata exceeds its bound.")
    check = owner_check or (lambda: None)
    with manifest(scratch) as database:
        count = 0
        for root in roots:
            with closing(source_rows(root, exclusions)) as rows:
                for row, path, stamp in rows:
                    count += 1
                    if count > MAX_TREE_ENTRIES:
                        raise InstallationArchiveError(
                            "The complete installation entry budget was exceeded."
                        )
                    _insert(database, row, path, stamp)
        with tarfile.open(
            fileobj=handle,
            mode="w|",
            format=tarfile.PAX_FORMAT,
            encoding="utf-8",
            errors="surrogateescape",
        ) as archive:
            payload = canonical(metadata)
            row = {
                "name": "installation.json",
                "mode": stat.S_IFREG | 0o600,
                "uid": 0,
                "gid": 0,
                "mtime_ns": 0,
                "size": len(payload),
            }
            _add(archive, row, io.BytesIO(payload))
            for key, payload, source, expected in database.execute(
                "SELECT name,payload,source,stamp FROM entries ORDER BY name"
            ):
                check()
                row, path, expected = (
                    json.loads(payload),
                    Path(os.fsdecode(source)),
                    json.loads(expected),
                )
                with open_leaf(path) as (parent, leaf):
                    value = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                    if list(_file_identity(value)) != expected:
                        raise InstallationArchiveError(
                            "A persistent entry changed during the cold archive."
                        )
                    if stat.S_ISREG(value.st_mode):
                        descriptor = os.open(
                            leaf,
                            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=parent,
                        )
                        try:
                            if list(_file_identity(os.fstat(descriptor))) != expected:
                                raise InstallationArchiveError(
                                    "A persistent file changed while being opened."
                                )
                            reader = _HashReader(descriptor)
                            _add(archive, row, reader)
                            if (
                                list(_file_identity(os.fstat(descriptor))) != expected
                                or reader.size != row["size"]
                            ):
                                raise InstallationArchiveError(
                                    "A persistent file changed while being archived."
                                )
                            row["sha256"] = reader.digest.hexdigest()
                        finally:
                            os.close(descriptor)
                    else:
                        if (
                            stat.S_ISLNK(value.st_mode)
                            and os.readlink(leaf, dir_fd=parent) != row["link"]
                        ):
                            raise InstallationArchiveError("A persistent link changed.")
                        _add(archive, row)
                database.execute(
                    "UPDATE entries SET payload=? WHERE name=?", (canonical(row), key)
                )
            length = database.execute(
                "SELECT COALESCE(SUM(length(payload)+1),0) FROM entries"
            ).fetchone()[0]
            _add(
                archive,
                {
                    "name": FOOTER,
                    "mode": stat.S_IFREG | 0o600,
                    "uid": 0,
                    "gid": 0,
                    "mtime_ns": 0,
                    "size": length,
                },
                _Rows(database),
            )
        # Complete second content pass detects additions/removals as well as in-place writes.
        expected_digest = hashlib.sha256()
        for (payload,) in database.execute("SELECT payload FROM entries ORDER BY name"):
            expected_digest.update(payload + b"\n")
        current = inventory(roots, scratch, exclusions, content=True)
        if current["fingerprint"] != expected_digest.hexdigest():
            raise InstallationArchiveError(
                "The installation changed while its complete archive was being captured."
            )
        return {"entries": count, "fingerprint": expected_digest.hexdigest()}


def _parsed_row(info):
    safe_member(info.name)
    if info.sparse is not None or not (info.isreg() or info.isdir() or info.issym()):
        raise InstallationArchiveError(
            "Special, sparse and hardlinked archive members are refused."
        )
    if (
        info.mode & ~0o777
        or min(info.uid, info.gid, info.size) < 0
        or max(info.uid, info.gid) > 2**31 - 1
    ):
        raise InstallationArchiveError("Unsafe archive ownership or permissions.")
    try:
        nanoseconds = int(info.pax_headers["DUMB.mtime_ns"])
    except (KeyError, ValueError, TypeError):
        raise InstallationArchiveError(
            "Archive nanosecond metadata is missing."
        ) from None
    if abs(nanoseconds) > 2**63 - 1:
        raise InstallationArchiveError("Archive timestamp is outside its bound.")
    row = {
        "name": info.name,
        "mode": (
            stat.S_IFDIR
            if info.isdir()
            else stat.S_IFLNK if info.issym() else stat.S_IFREG
        )
        | info.mode,
        "uid": info.uid,
        "gid": info.gid,
        "mtime_ns": nanoseconds,
        "size": info.size if info.isreg() else 0,
    }
    if info.issym():
        if "\x00" in info.linkname or len(os.fsencode(info.linkname)) > 16384:
            raise InstallationArchiveError("Invalid archived symbolic link.")
        row["link"] = info.linkname
    return row


def _inspect_archive(handle, scratch, *, extract_root=None, owner_check=None):
    """Verify a complete stream; optional extraction is only to an empty private tree."""
    check = owner_check or (lambda: None)
    metadata = None
    footer = False
    count = logical = 0
    root_sizes = {}
    controller_sha256 = {}
    root_ids = set()
    with manifest(scratch) as database:
        with tarfile.open(
            fileobj=handle,
            mode="r|",
            tarinfo=_BoundedTarInfo,
            encoding="utf-8",
            errors="surrogateescape",
        ) as archive:
            while (info := archive.next()) is not None:
                archive.members.clear()
                check()
                row = _parsed_row(info)
                if footer:
                    raise InstallationArchiveError(
                        "Members follow the complete installation manifest."
                    )
                if metadata is None:
                    if (
                        info.name != "installation.json"
                        or not info.isreg()
                        or info.size > MAX_METADATA
                    ):
                        raise InstallationArchiveError(
                            "Versioned installation metadata must be the first member."
                        )
                    with archive.extractfile(info) as stream:
                        metadata = json.loads(stream.read(MAX_METADATA + 1))
                    if (
                        not isinstance(metadata, dict)
                        or metadata.get("format") != FORMAT
                        or type(metadata.get("schema_version")) is not int
                        or metadata.get("schema_version") != SCHEMA
                    ):
                        raise InstallationArchiveError(
                            "The installation archive format is unsupported."
                        )
                    roots = metadata.get("roots")
                    if not isinstance(roots, list) or not roots or len(roots) > 256:
                        raise InstallationArchiveError(
                            "Invalid installation root inventory."
                        )
                    root_ids = {r.get("id") for r in roots if isinstance(r, dict)}
                    if len(root_ids) != len(roots) or any(
                        not isinstance(x, str)
                        or not x.startswith("root_")
                        or not x[5:].isdigit()
                        for x in root_ids
                    ):
                        raise InstallationArchiveError(
                            "Invalid installation root identity."
                        )
                    continue
                if info.name == FOOTER:
                    if not info.isreg() or info.size > MAX_TREE_MANIFEST_BYTES:
                        raise InstallationArchiveError(
                            "The installation manifest exceeds its bound."
                        )
                    with archive.extractfile(info) as stream:
                        while raw := stream.readline(MAX_RECORD + 2):
                            if len(raw) > MAX_RECORD + 1 or not raw.endswith(b"\n"):
                                raise InstallationArchiveError(
                                    "Invalid bounded installation manifest record."
                                )
                            expected = json.loads(raw)
                            if not isinstance(expected, dict):
                                raise InstallationArchiveError(
                                    "Invalid installation manifest record."
                                )
                            name = safe_member(expected.get("name"))
                            found = database.execute(
                                "SELECT payload,matched FROM entries WHERE name=?",
                                (name.encode("utf-8", "surrogatepass"),),
                            ).fetchone()
                            if not found or found[1] or canonical(expected) != found[0]:
                                raise InstallationArchiveError(
                                    "Installation member checksums or metadata do not match."
                                )
                            database.execute(
                                "UPDATE entries SET matched=1 WHERE name=?",
                                (name.encode("utf-8", "surrogatepass"),),
                            )
                    footer = True
                    continue
                parts = PurePosixPath(info.name).parts
                if len(parts) < 2 or parts[0] != "roots" or parts[1] not in root_ids:
                    raise InstallationArchiveError(
                        "An unexpected installation member was found."
                    )
                if len(parts) == 2:
                    expected_root = next(
                        root for root in roots if root["id"] == parts[1]
                    )
                    actual_kind = (
                        "directory"
                        if info.isdir()
                        else "symlink" if info.issym() else "file"
                    )
                    if expected_root.get("kind") != actual_kind:
                        raise InstallationArchiveError(
                            "An archive root type differs from its versioned inventory."
                        )
                # Every parent must already be a directory, preventing children beneath links.
                if len(parts) > 2:
                    parent = "/".join(parts[:-1]).encode("utf-8", "surrogatepass")
                    parent_row = database.execute(
                        "SELECT payload FROM entries WHERE name=?", (parent,)
                    ).fetchone()
                    if not parent_row or not stat.S_ISDIR(
                        json.loads(parent_row[0])["mode"]
                    ):
                        raise InstallationArchiveError(
                            "An archive member has an unsafe or absent parent."
                        )
                count += 1
                if count > MAX_TREE_ENTRIES:
                    raise InstallationArchiveError(
                        "Installation archive exceeds its entry bound."
                    )
                logical += row["size"]
                root_sizes[parts[1]] = root_sizes.get(parts[1], 0) + row["size"]
                destination = (
                    Path(extract_root) / Path(*parts[1:])
                    if extract_root is not None
                    else None
                )
                if destination is not None:
                    if destination.exists() or destination.is_symlink():
                        raise InstallationArchiveError(
                            "A private extraction target already exists."
                        )
                    if info.isdir():
                        destination.mkdir(mode=0o700)
                    elif info.issym():
                        # Deferred until all payload writes have completed.
                        pass
                    else:
                        descriptor = os.open(
                            destination,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                        )
                digest = hashlib.sha256()
                if info.isreg():
                    try:
                        with archive.extractfile(info) as stream:
                            while chunk := stream.read(1024 * 1024):
                                digest.update(chunk)
                                if destination is not None:
                                    pending = memoryview(chunk)
                                    while pending:
                                        written = os.write(descriptor, pending)
                                        pending = pending[written:]
                        if destination is not None:
                            os.fsync(descriptor)
                    finally:
                        if destination is not None:
                            os.close(descriptor)
                    row["sha256"] = digest.hexdigest()
                if info.isreg():
                    selected_root = next(
                        root for root in roots if root["id"] == parts[1]
                    )
                    absolute = Path(selected_root["path"]).joinpath(*parts[2:])
                    for role, key in (
                        ("config", "config_path"),
                        ("auth", "auth_config_path"),
                    ):
                        if metadata.get(key) == str(absolute):
                            controller_sha256[role] = row["sha256"]
                _insert(database, row)
            if any(getattr(archive.fileobj, "buf", b"")):
                raise InstallationArchiveError(
                    "Unexpected bytes follow the archive end marker."
                )
            while tail := handle.read(1024 * 1024):
                if any(tail):
                    raise InstallationArchiveError(
                        "Unexpected bytes follow the installation archive."
                    )
        if (
            metadata is None
            or not footer
            or database.execute(
                "SELECT COUNT(*) FROM entries WHERE matched=0"
            ).fetchone()[0]
        ):
            raise InstallationArchiveError("The installation archive is incomplete.")
        found_roots = {
            json.loads(x[0])["name"].split("/")[1]
            for x in database.execute("SELECT payload FROM entries")
            if len(json.loads(x[0])["name"].split("/")) == 2
        }
        if found_roots != root_ids:
            raise InstallationArchiveError("An installation root is absent.")
        if extract_root is not None:
            for (payload,) in database.execute(
                "SELECT payload FROM entries ORDER BY name DESC"
            ):
                row = json.loads(payload)
                destination = Path(extract_root) / Path(
                    *PurePosixPath(row["name"]).parts[1:]
                )
                if stat.S_ISLNK(row["mode"]):
                    os.symlink(row["link"], destination)
                if os.geteuid() == 0:
                    os.chown(destination, row["uid"], row["gid"], follow_symlinks=False)
                elif (row["uid"], row["gid"]) != (os.geteuid(), os.getegid()):
                    raise InstallationArchiveError(
                        "Restore requires the original ownership or a privileged controller."
                    )
                if not stat.S_ISLNK(row["mode"]):
                    os.chmod(destination, stat.S_IMODE(row["mode"]))
                os.utime(
                    destination,
                    ns=(row["mtime_ns"], row["mtime_ns"]),
                    follow_symlinks=False,
                )
        digest = hashlib.sha256()
        for (payload,) in database.execute("SELECT payload FROM entries ORDER BY name"):
            digest.update(payload + b"\n")
        return {
            "verified": True,
            "metadata": metadata,
            "entries": count,
            "logical_bytes": logical,
            "root_sizes": root_sizes,
            "controller_sha256": controller_sha256,
            "fingerprint": digest.hexdigest(),
        }


def inspect_archive(handle, scratch, *, extract_root=None, owner_check=None):
    try:
        return _inspect_archive(
            handle, scratch, extract_root=extract_root, owner_check=owner_check
        )
    except InstallationArchiveError:
        raise
    except (
        _TarSafetyError,
        tarfile.TarError,
        sqlite3.Error,
        ValueError,
        KeyError,
        TypeError,
        RecursionError,
        UnicodeError,
    ) as error:
        raise InstallationArchiveError(
            "The bounded installation archive is malformed or unsupported."
        ) from error
