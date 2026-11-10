"""Portable verified cold archives; every SQLite working manifest stays local."""

from contextlib import closing, contextmanager
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tarfile
import tempfile

from utils.backup_storage import _directory, require_local_path
from utils.infinidysk_v2_inventory import (
    MAX_TREE_ENTRIES,
    MAX_TREE_MANIFEST_BYTES,
    iter_tree,
)

FORMAT = "pax_tar_v1"
MAX_RECORD_BYTES = 64 * 1024
FOOTER = "metadata/manifest.jsonl"


class ArchiveError(ValueError):
    """A complete portable backup could not be established safely."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _stamp(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


class _BoundedTarInfo(tarfile.TarInfo):
    def _proc_gnusparse_00(self, *args):
        raise ArchiveError("Sparse archive extensions are unsupported.")

    def _proc_gnusparse_01(self, *args):
        raise ArchiveError("Sparse archive extensions are unsupported.")

    def _proc_gnusparse_10(self, *args):
        raise ArchiveError("Sparse archive extensions are unsupported.")

    def _proc_member(self, archive):
        if self.type == tarfile.XHDTYPE:
            if self.size > MAX_RECORD_BYTES:
                raise ArchiveError("An archive metadata header exceeds its bound.")
            depth = getattr(archive, "_dumb_pax_depth", 0) + 1
            if depth > 4:
                raise ArchiveError("Nested archive metadata exceeds its bound.")
            archive._dumb_pax_depth = depth
            try:
                return super()._proc_member(archive)
            finally:
                archive._dumb_pax_depth -= 1
        if self.type not in {
            tarfile.REGTYPE,
            tarfile.AREGTYPE,
            tarfile.DIRTYPE,
            tarfile.SYMTYPE,
        }:
            raise ArchiveError("The archive contains an unsupported or special entry.")
        return super()._proc_member(archive)


@contextmanager
def _manifest(scratch):
    require_local_path(scratch)
    with tempfile.TemporaryDirectory(
        prefix=".archive-manifest-", dir=scratch
    ) as temporary:
        path = Path(temporary) / "manifest.sqlite"
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(path)) as database:
            database.execute("PRAGMA page_size=4096")
            database.execute(f"PRAGMA max_page_count={MAX_TREE_MANIFEST_BYTES // 4096}")
            database.execute("PRAGMA cache_size=-2048")
            database.execute("PRAGMA journal_mode=OFF")
            database.execute("PRAGMA synchronous=OFF")
            database.execute(
                "CREATE TABLE entries (name BLOB PRIMARY KEY,payload BLOB NOT NULL,source TEXT,expected BLOB,source_key TEXT,path BLOB,source_row BLOB,matched INTEGER NOT NULL DEFAULT 0) WITHOUT ROWID"
            )
            database.execute("CREATE INDEX source_order ON entries(source_key,path)")
            yield database


def _safe_name(name):
    if (
        not isinstance(name, str)
        or name.startswith("/")
        or "\x00" in name
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise ArchiveError("The archive contains an escaping or invalid member name.")
    return name


def _source_row(record):
    row = {
        "path": record["relative"],
        "mode": record["mode"],
        "mtime_ns": record["mtime_ns"],
    }
    if not stat.S_ISDIR(record["mode"]):
        row["size"] = record["size"]
    if stat.S_ISREG(record["mode"]):
        row["sha256"] = record["sha256"]
    elif stat.S_ISLNK(record["mode"]):
        row["link"] = record["link"]
    return row


def _insert(database, record, *, source=None, expected=None):
    name = _safe_name(record["name"])
    raw = _json(record)
    if len(raw) > MAX_RECORD_BYTES:
        raise ArchiveError("An archive manifest record exceeds its bound.")
    root = record.get("source_key")
    relative = record.get("relative")
    original = _json(_source_row(record)) if root and relative else None
    try:
        database.execute(
            "INSERT INTO entries(name,payload,source,expected,source_key,path,source_row) VALUES(?,?,?,?,?,?,?)",
            (
                name.encode("utf-8", "surrogatepass"),
                raw,
                os.fsencode(source) if source is not None else None,
                _json(expected) if expected is not None else None,
                root,
                relative.encode("utf-8", "surrogatepass") if relative else None,
                original,
            ),
        )
    except sqlite3.Error as error:
        raise ArchiveError(
            "The archive manifest is duplicate or exceeds its bounded local storage."
        ) from error


def _tree_hash(database, key):
    digest = hashlib.sha256(b"[")
    separator = b""
    for (row,) in database.execute(
        "SELECT source_row FROM entries WHERE source_key=? AND path IS NOT NULL ORDER BY path",
        (key,),
    ):
        digest.update(separator)
        digest.update(row)
        separator = b","
    digest.update(b"]")
    return digest.hexdigest()


def _header(record):
    info = tarfile.TarInfo(record["name"])
    mode = record["mode"]
    info.type = (
        tarfile.DIRTYPE
        if stat.S_ISDIR(mode)
        else tarfile.SYMTYPE if stat.S_ISLNK(mode) else tarfile.REGTYPE
    )
    info.mode = stat.S_IMODE(mode)
    info.uid, info.gid = record["uid"], record["gid"]
    info.mtime = record["mtime_ns"] // 1_000_000_000
    info.size = record["size"] if stat.S_ISREG(mode) else 0
    info.linkname = record.get("link", "")
    sign = "-" if record["mtime_ns"] < 0 else ""
    seconds, nanoseconds = divmod(abs(record["mtime_ns"]), 1_000_000_000)
    info.pax_headers = {
        "mtime": f"{sign}{seconds}.{nanoseconds:09d}",
        "DUMB.mtime_ns": str(record["mtime_ns"]),
        "DUMB.mode": str(mode),
        "DUMB.source_size": str(record["size"]),
    }
    return info


class _Lines:
    def __init__(self, database):
        self.rows = iter(database.execute("SELECT payload FROM entries ORDER BY name"))
        self.buffer = b""

    def read(self, size):
        if size < 0:
            raise ArchiveError("Archive manifest reads must remain bounded.")
        while len(self.buffer) < size:
            try:
                self.buffer += next(self.rows)[0] + b"\n"
            except StopIteration:
                break
        result, self.buffer = self.buffer[:size], self.buffer[size:]
        return result


class _DigestReader:
    def __init__(self, handle):
        self.handle = handle
        self.digest = hashlib.sha256()
        self.size = 0

    def read(self, size=-1):
        data = self.handle.read(size)
        self.digest.update(data)
        self.size += len(data)
        return data


def _record_from_header(info, digest=None):
    _safe_name(info.name)
    try:
        mode = int(info.pax_headers["DUMB.mode"])
        mtime = int(info.pax_headers["DUMB.mtime_ns"])
        size = int(info.pax_headers["DUMB.source_size"])
    except (KeyError, ValueError) as error:
        raise ArchiveError("Portable archive metadata is incomplete.") from error
    if Decimal(info.pax_headers.get("mtime", "NaN")) != Decimal(mtime) / Decimal(
        1_000_000_000
    ):
        raise ArchiveError("The portable archive timestamp did not verify.")
    if size < 0 or stat.S_IMODE(mode) != info.mode:
        raise ArchiveError("Archive permission or size metadata did not verify.")
    valid = (
        (info.isdir() and stat.S_ISDIR(mode))
        or (info.issym() and stat.S_ISLNK(mode))
        or (info.isreg() and stat.S_ISREG(mode))
    )
    if not valid or (info.isreg() and info.size != size):
        raise ArchiveError("Archive entry type or payload size did not verify.")
    record = {
        "name": info.name,
        "mode": mode,
        "mtime_ns": mtime,
        "uid": info.uid,
        "gid": info.gid,
        "size": size,
    }
    if info.issym():
        record["link"] = info.linkname
    if info.isreg():
        record["sha256"] = digest
    parts = info.name.split("/", 2)
    if parts[0] == "trees" and len(parts) >= 2:
        record["source_key"] = parts[1]
        record["relative"] = parts[2] if len(parts) == 3 else ""
    return record


def _verify_stream(handle, scratch, roots, fingerprints):
    allowed_metadata = {
        "metadata/roots.json",
        "metadata/extras/config.json",
        "metadata/extras/v1-postgres.dump",
        "metadata/extras/source-database.sqlite",
        "metadata/extras/v1-postgres-snapshot.json",
    }
    with _manifest(scratch) as database:
        count = 0
        footer = False
        with tarfile.open(
            fileobj=handle,
            mode="r|",
            bufsize=1024 * 1024,
            tarinfo=_BoundedTarInfo,
            encoding="utf-8",
            errors="surrogateescape",
        ) as archive:
            while (info := archive.next()) is not None:
                archive.members.clear()
                _safe_name(info.name)
                if info.sparse is not None:
                    raise ArchiveError("Sparse archive extensions are unsupported.")
                if footer:
                    raise ArchiveError(
                        "Unexpected archive entries follow its complete manifest."
                    )
                if info.name == FOOTER:
                    _record_from_header(info, "")
                    if (
                        not info.isreg()
                        or info.mode != 0o600
                        or info.size > MAX_TREE_MANIFEST_BYTES
                    ):
                        raise ArchiveError(
                            "The archive manifest is unsafe or too large."
                        )
                    stream = archive.extractfile(info)
                    if stream is None:
                        raise ArchiveError("The archive manifest could not be read.")
                    with stream:
                        while raw := stream.readline(MAX_RECORD_BYTES + 2):
                            if len(raw) > MAX_RECORD_BYTES + 1 or not raw.endswith(
                                b"\n"
                            ):
                                raise ArchiveError(
                                    "An archive manifest record exceeds its bound."
                                )
                            expected = json.loads(raw)
                            if not isinstance(expected, dict):
                                raise ArchiveError(
                                    "An archive manifest record is invalid."
                                )
                            key = _safe_name(expected.get("name")).encode(
                                "utf-8", "surrogatepass"
                            )
                            actual = database.execute(
                                "SELECT payload,matched FROM entries WHERE name=?",
                                (key,),
                            ).fetchone()
                            if (
                                actual is None
                                or actual[1]
                                or actual[0] != _json(expected)
                            ):
                                raise ArchiveError(
                                    "Archive contents, metadata or entry identities differ from its manifest."
                                )
                            database.execute(
                                "UPDATE entries SET matched=1 WHERE name=?", (key,)
                            )
                    footer = True
                    continue
                count += 1
                if count > MAX_TREE_ENTRIES * 2 + 32:
                    raise ArchiveError("The archive entry inventory exceeds its bound.")
                record = _record_from_header(info)
                if record.get("source_key") is not None:
                    if record["source_key"] not in roots.values():
                        raise ArchiveError(
                            "The archive contains an unexpected source root."
                        )
                    if not record.get("relative") and not info.isdir():
                        raise ArchiveError(
                            "An archive source root must be a real directory."
                        )
                elif info.name not in allowed_metadata or not info.isreg():
                    raise ArchiveError(
                        "The archive contains an unexpected metadata member."
                    )
                if info.isreg():
                    digest = hashlib.sha256()
                    stream = archive.extractfile(info)
                    if stream is None:
                        raise ArchiveError("An archive payload could not be read.")
                    with stream:
                        if info.name == "metadata/roots.json":
                            expected = _json(
                                {
                                    "format": FORMAT,
                                    "roots": roots,
                                    "original_fingerprints": fingerprints,
                                }
                            )
                            if (
                                info.size != len(expected)
                                or stream.read(len(expected) + 1) != expected
                            ):
                                raise ArchiveError(
                                    "The archive source mapping differs from its retained plan."
                                )
                            digest.update(expected)
                        else:
                            while chunk := stream.read(1024 * 1024):
                                digest.update(chunk)
                    record["sha256"] = digest.hexdigest()
                _insert(database, record)
            if any(getattr(archive.fileobj, "buf", b"")):
                raise ArchiveError(
                    "Unexpected nonzero bytes follow the archive end marker."
                )
            while tail := handle.read(1024 * 1024):
                if any(tail):
                    raise ArchiveError(
                        "Unexpected data follows the archive end marker."
                    )
        if (
            not footer
            or database.execute(
                "SELECT COUNT(*) FROM entries WHERE matched=0"
            ).fetchone()[0]
        ):
            raise ArchiveError("The archive does not cover every source entry.")
        actual_roots = {
            row[0]
            for row in database.execute(
                "SELECT DISTINCT source_key FROM entries WHERE source_key IS NOT NULL"
            )
        }
        if actual_roots != set(roots.values()):
            raise ArchiveError(
                "The archive source roots differ from the retained plan."
            )
        for source, key in roots.items():
            if _tree_hash(database, key) != fingerprints[source]:
                raise ArchiveError(
                    "The archive source content fingerprint did not verify."
                )
        extras = {}
        for (payload,) in database.execute(
            "SELECT payload FROM entries WHERE source_key IS NULL"
        ):
            record = json.loads(payload)
            if record["name"].startswith("metadata/extras/"):
                extras[record["name"].removeprefix("metadata/extras/")] = record[
                    "sha256"
                ]
    return {
        "verified": True,
        "original_fingerprints": fingerprints,
        "extras_sha256": extras,
    }


def verify_retained_archive(snapshot, plan, storage):
    receipt = snapshot.get("cold_archive") or {}
    if receipt.get("verified") is not True or receipt.get("extension") != "tar":
        raise ArchiveError("No verified cold archive is retained for this job.")
    scratch = snapshot.get("source_snapshot")
    roots = snapshot.get("archive_roots") or {}
    fingerprints = snapshot.get("original_fingerprints") or {}
    if not roots or set(roots) != set(fingerprints):
        raise ArchiveError("The retained archive source binding is incomplete.")
    with storage.open_artifact(receipt["reference"]) as handle:
        reader = _DigestReader(handle)
        proof = _verify_stream(reader, scratch, roots, fingerprints)
        while reader.read(1024 * 1024):
            pass
        if (
            reader.digest.hexdigest() != receipt["sha256"]
            or reader.size != receipt["size_bytes"]
        ):
            raise ArchiveError(
                "The retained archive bytes differ from the original job receipt."
            )
    if (plan.get("source") or {}).get("provider") in {"postgres", "postgresql"}:
        postgres = snapshot.get("postgres_snapshot") or {}
        binding = (postgres.get("source_binding") or {}).get("binding_fingerprint")
        restore = postgres.get("restore_proof") or {}
        expected = postgres.get("dump_sha256")
        if (
            postgres.get("verified_restore") is not True
            or postgres.get("source_read_only") is not True
            or not isinstance(binding, str)
            or not binding
            or restore.get("verified") is not True
            or restore.get("source_binding") != binding
            or not isinstance(expected, str)
            or len(expected) != 64
            or proof["extras_sha256"].get("v1-postgres.dump") != expected
        ):
            raise ArchiveError(
                "The archived PostgreSQL dump does not match its verified independent restore proof."
            )
    return {
        **proof,
        "archive_sha256": receipt["sha256"],
        "archive_reference": receipt["reference"],
    }


@contextmanager
def _source_leaf(root, relative, expected_root):
    root = Path(root)
    with _directory(root) as root_fd:
        opened = os.fstat(root_fd)
        if expected_root is not None and (
            opened.st_dev,
            opened.st_ino,
            stat.S_IFMT(opened.st_mode),
        ) != (expected_root["device"], expected_root["inode"], expected_root["mode"]):
            raise ArchiveError("The retained source root changed during archiving.")
        parts = Path(relative).parts if relative else ()
        descriptors = []
        parent = root_fd
        try:
            for component in parts[:-1]:
                descriptor = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent,
                )
                descriptors.append(descriptor)
                parent = descriptor
            yield parent, parts[-1] if parts else None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


def _root_identity(plan, source):
    if source == plan["source"]["data_dir"]:
        return plan["source"].get("data_identity")
    return (plan["source"].get("runtime_identity") or {}).get("directory")


def _portable_record(name, row, *, key=None, relative=None):
    identity = row.get("identity") or {}
    record = {
        "name": name,
        "mode": row["mode"],
        "mtime_ns": row["mtime_ns"],
        "uid": identity.get("uid", os.geteuid()),
        "gid": identity.get("gid", os.getegid()),
        "size": row.get("size", 0),
    }
    if key is not None:
        record.update(source_key=key, relative=relative or "")
    if stat.S_ISREG(row["mode"]):
        record["sha256"] = "0" * 64
    if "link" in row:
        record["link"] = row["link"]
    return record


def _metadata_row(value):
    return {
        "mode": value.st_mode,
        "mtime_ns": value.st_mtime_ns,
        "size": value.st_size if not stat.S_ISDIR(value.st_mode) else 0,
        "identity": {"uid": value.st_uid, "gid": value.st_gid},
    }


def _expected_row(row):
    identity = row["identity"]
    return [
        identity["device"],
        identity["inode"],
        row["mode"],
        row.get("size", 0),
        row["mtime_ns"],
        identity["ctime_ns"],
    ]


def _matches(value, expected):
    actual = list(_stamp(value))
    if stat.S_ISDIR(value.st_mode):
        actual[3] = 0
    return actual == expected


class _SourceReader:
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.digest = hashlib.sha256()
        self.size = 0

    def read(self, size):
        data = os.read(self.descriptor, size)
        self.digest.update(data)
        self.size += len(data)
        return data


def _add(archive, record, fileobj=None):
    archive.addfile(_header(record), fileobj)
    archive.members.clear()


def create_retained_archive(
    plan,
    workspace,
    storage,
    fingerprints,
    *,
    progress=None,
    extra_files=None,
    owner_check=None,
):
    """Stream source trees directly to closed storage, never a full local backup."""
    scratch = Path(workspace["root"])
    require_local_path(scratch)
    selected = plan.get("backup_storage") or {}
    destination = selected.get("destination_id")
    if not destination:
        raise ArchiveError("Select a registered archive destination first.")
    ordered = list(
        dict.fromkeys([plan["source"]["data_dir"], plan["source"]["runtime_dir"]])
    )
    roots = {source: f"tree_{index}" for index, source in enumerate(ordered)}
    if set(roots) != set(fingerprints):
        raise ArchiveError("Every original source root needs a complete fingerprint.")
    extras = {"config.json": Path(workspace["config_backup"]), **(extra_files or {})}
    allowed = {
        "config.json",
        "v1-postgres.dump",
        "source-database.sqlite",
        "v1-postgres-snapshot.json",
    }
    if set(extras) - allowed:
        raise ArchiveError("The archive contains an unexpected local artifact.")
    report = progress or (lambda *args, **kwargs: None)

    def producer(handle):
        with _manifest(scratch) as database:
            for source, key in roots.items():
                path = Path(source)
                try:
                    value = path.lstat()
                except FileNotFoundError:
                    if _root_identity(plan, source) is not None:
                        raise ArchiveError(
                            "A retained source root disappeared."
                        ) from None
                    root_record = {
                        "name": f"trees/{key}",
                        "mode": stat.S_IFDIR | 0o700,
                        "mtime_ns": 0,
                        "uid": os.geteuid(),
                        "gid": os.getegid(),
                        "size": 0,
                        "source_key": key,
                        "relative": "",
                    }
                    _insert(database, root_record)
                    continue
                expected_root = _root_identity(plan, source)
                if expected_root is not None and (
                    value.st_dev,
                    value.st_ino,
                    stat.S_IFMT(value.st_mode),
                ) != (
                    expected_root["device"],
                    expected_root["inode"],
                    expected_root["mode"],
                ):
                    raise ArchiveError(
                        "A retained source root changed before archiving."
                    )
                if not stat.S_ISDIR(value.st_mode):
                    raise ArchiveError(
                        "A retained archive root is not a real directory."
                    )
                root_record = _portable_record(
                    f"trees/{key}", _metadata_row(value), key=key, relative=""
                )
                expected = list(_stamp(value))
                expected[3] = 0
                _insert(database, root_record, source=source, expected=expected)
                with closing(iter_tree(source, include_identity=True)) as rows:
                    for row in rows:
                        record = _portable_record(
                            f"trees/{key}/{row['path']}",
                            row,
                            key=key,
                            relative=row["path"],
                        )
                        _insert(
                            database,
                            record,
                            source=str(path / row["path"]),
                            expected=_expected_row(row),
                        )
            for label, path in extras.items():
                value = path.lstat()
                if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                    raise ArchiveError(
                        "A local closed backup artifact is linked or unsafe."
                    )
                record = _portable_record(
                    f"metadata/extras/{label}", _metadata_row(value)
                )
                _insert(
                    database, record, source=str(path), expected=list(_stamp(value))
                )
            roots_payload = _json(
                {
                    "format": FORMAT,
                    "roots": roots,
                    "original_fingerprints": fingerprints,
                }
            )
            roots_record = {
                "name": "metadata/roots.json",
                "mode": stat.S_IFREG | 0o600,
                "mtime_ns": 0,
                "uid": 0,
                "gid": 0,
                "size": len(roots_payload),
                "sha256": hashlib.sha256(roots_payload).hexdigest(),
            }
            _insert(database, roots_record)
            report(
                "archiving",
                "Writing the complete retained v1 archive to the selected mounted storage.",
            )
            with tarfile.open(
                fileobj=handle,
                mode="w|",
                bufsize=1024 * 1024,
                copybufsize=1024 * 1024,
                format=tarfile.PAX_FORMAT,
                encoding="utf-8",
                errors="surrogateescape",
            ) as archive:
                for name, payload, source_bytes, expected_bytes in database.execute(
                    "SELECT name,payload,source,expected FROM entries ORDER BY name"
                ):
                    record = json.loads(payload)
                    if record["name"] == "metadata/roots.json":
                        _add(archive, record, io.BytesIO(roots_payload))
                        continue
                    if source_bytes is None:
                        _add(archive, record)
                        continue
                    source_path = Path(os.fsdecode(source_bytes))
                    key = record.get("source_key")
                    root = next(
                        (path for path, value in roots.items() if value == key), None
                    )
                    root = Path(root) if root is not None else source_path.parent
                    relative = (
                        record.get("relative") if key is not None else source_path.name
                    )
                    expected = json.loads(expected_bytes)
                    expected_root = (
                        _root_identity(plan, str(root)) if key is not None else None
                    )
                    with _source_leaf(root, relative, expected_root) as (parent, leaf):
                        value = (
                            os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                            if leaf is not None
                            else os.fstat(parent)
                        )
                        if not _matches(value, expected):
                            raise ArchiveError(
                                "A source entry changed before its archive write."
                            )
                        if stat.S_ISREG(record["mode"]):
                            descriptor = os.open(
                                leaf,
                                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                dir_fd=parent,
                            )
                            try:
                                if not _matches(os.fstat(descriptor), expected):
                                    raise ArchiveError(
                                        "A source file changed while it was opened."
                                    )
                                reader = _SourceReader(descriptor)
                                _add(archive, record, reader)
                                if (
                                    not _matches(os.fstat(descriptor), expected)
                                    or reader.size != record["size"]
                                ):
                                    raise ArchiveError(
                                        "A source file changed while it was archived."
                                    )
                                record["sha256"] = reader.digest.hexdigest()
                            finally:
                                os.close(descriptor)
                        else:
                            if (
                                stat.S_ISLNK(record["mode"])
                                and os.readlink(leaf, dir_fd=parent) != record["link"]
                            ):
                                raise ArchiveError(
                                    "A source symbolic link changed while archiving."
                                )
                            _add(archive, record)
                    original = (
                        _json(_source_row(record))
                        if key is not None and record.get("relative")
                        else None
                    )
                    database.execute(
                        "UPDATE entries SET payload=?,source_row=? WHERE name=?",
                        (_json(record), original, name),
                    )
                for source, key in roots.items():
                    if _tree_hash(database, key) != fingerprints[source]:
                        raise ArchiveError(
                            "The archived source differs from its original complete fingerprint."
                        )
                length = database.execute(
                    "SELECT COALESCE(SUM(length(payload)+1),0) FROM entries"
                ).fetchone()[0]
                footer = {
                    "name": FOOTER,
                    "mode": stat.S_IFREG | 0o600,
                    "mtime_ns": 0,
                    "uid": 0,
                    "gid": 0,
                    "size": length,
                }
                _add(archive, footer, _Lines(database))

    def verifier(handle):
        report(
            "verifying_archive",
            "Reading back and verifying every retained archive member and source fingerprint.",
        )
        return _verify_stream(handle, scratch, roots, fingerprints)

    receipt = storage.write_file(
        destination,
        "major-" + scratch.name,
        producer,
        verifier=verifier,
        extension="tar",
        owner_check=owner_check,
        metadata={
            "kind": "infinidysk_v2_archive",
            "original_fingerprints": fingerprints,
            "archive_roots": roots,
        },
    )
    return {"receipt": receipt, "archive_roots": roots}
