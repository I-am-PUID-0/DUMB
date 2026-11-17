"""Read-only queue inspection and an independently derived native import input.

Queued work is omitted only from a private seven-table projection. The full v1
snapshot, backup, history and payload blobs are never changed by this module.
"""

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import tempfile
import stat
import time
from urllib.parse import quote
import uuid

from utils.infinidysk_v2_pg_snapshot import (
    LEGACY_TABLES,
    _connect_default,
    _readonly,
    connection_settings,
)


class QueueInputError(ValueError):
    """A credential-free refusal to inspect or derive migration input."""


def queue_policy(options):
    return (
        "omit_from_v2"
        if options.get("omit_queued_downloads") is True
        else "require_empty"
    )


def public_queue(options, observation=None, *, captured=False, omitted=None):
    value = observation or {}
    count = value.get("count")
    timestamp = value.get("observed_at")
    return {
        "policy": queue_policy(options),
        "observed_count": count if type(count) is int and count >= 0 else None,
        "observed_at": timestamp if type(timestamp) is int and timestamp >= 0 else None,
        "observation": (
            "captured" if captured else "live" if count is not None else "unavailable"
        ),
        "omitted_count": omitted if type(omitted) is int and omitted >= 0 else None,
        "source_queue_preserved": True,
    }


def public_queue_state(value):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("policy"), str)
        or value.get("policy") not in {"require_empty", "omit_from_v2"}
        or value.get("source_queue_preserved") is not True
    ):
        return None
    count = value.get("observed_count")
    stamp = value.get("observed_at")
    omitted = value.get("omitted_count")
    observation = value.get("observation")
    return {
        "policy": value["policy"],
        "observed_count": count if type(count) is int and count >= 0 else None,
        "observed_at": stamp if type(stamp) is int and stamp >= 0 else None,
        "observation": (
            observation
            if isinstance(observation, str)
            and observation in {"live", "unavailable", "captured"}
            else "unavailable"
        ),
        "omitted_count": omitted if type(omitted) is int and omitted >= 0 else None,
        "source_queue_preserved": True,
    }


def empty_queue_message(count):
    return (
        f"The InfiniDysk 1 download queue contains {count} item(s). "
        "Finish those downloads, remove them yourself in InfiniDysk, or choose to leave all queued downloads out of v2. "
        "DUMB did not delete any queue entries; completed and failed history will be preserved."
    )


def _identifier(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) == 16:
            return uuid.UUID(bytes=raw).hex
        return "bytes:" + raw.hex()
    try:
        return uuid.UUID(str(value)).hex
    except (ValueError, TypeError, AttributeError):
        return f"{type(value).__name__}:{value}"


def _ids(rows, check):
    count, xor, total = 0, 0, 0
    for (value,) in rows:
        check()
        digest = int.from_bytes(
            hashlib.sha256(_identifier(value).encode()).digest(), "big"
        )
        xor ^= digest
        total = (total + digest) % (1 << 256)
        count += 1
    return {"count": count, "ids_xor": f"{xor:064x}", "ids_sum": f"{total:064x}"}


def _stamp(value):
    return tuple(
        getattr(value, field)
        for field in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    )


def _regular(path):
    value = Path(path).lstat()
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise QueueInputError(
            "A migration queue database is not a direct regular file."
        )
    return value


def sqlite_queue(connection, *, identities=False, check=None):
    cancel = check or (lambda: None)
    cancel()
    if identities:
        result = _ids(connection.execute('SELECT "Id" FROM "QueueItems"'), cancel)
    else:
        result = {
            "count": connection.execute('SELECT COUNT(*) FROM "QueueItems"').fetchone()[
                0
            ]
        }
    result["observed_at"] = int(time.time())
    return result


@contextmanager
def _private_sqlite_copy(database, *, check, scratch_root=None, expected_identity=None):
    database = Path(database)
    source = database.parent
    root = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(root)
        expected = expected_identity
        if expected and (opened.st_dev, opened.st_ino) != (
            expected["device"],
            expected["inode"],
        ):
            raise QueueInputError(
                "The source directory changed before queue inspection."
            )
        names = (database.name, database.name + "-wal", database.name + "-journal")
        before = {}
        for name in names:
            try:
                row = os.stat(name, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                if name == database.name:
                    raise
                continue
            if not stat.S_ISREG(row.st_mode) or row.st_nlink != 1:
                raise QueueInputError(
                    "A source database or sidecar is linked or unsafe."
                )
            before[name] = row
        parent = (
            Path(scratch_root)
            if scratch_root is not None
            else Path(tempfile.gettempdir())
        )
        if (
            shutil.disk_usage(parent).free
            < sum(row.st_size for row in before.values()) + 64 * 1024**2
        ):
            raise QueueInputError(
                "Local free space is insufficient for the private download-queue database check."
            )
        # Even mode=ro can update the SOURCE WAL's SHM reader marks. Copy only
        # the small database/WAL set, never opening source SQLite or its SHM.
        with tempfile.TemporaryDirectory(
            prefix="dumb-v1-queue-", dir=parent
        ) as temporary:
            for name, row in before.items():
                check()
                descriptor = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root
                )
                try:
                    if _stamp(os.fstat(descriptor)) != _stamp(row):
                        raise QueueInputError(
                            "The source database changed before its private queue check."
                        )
                    target = os.open(
                        Path(temporary) / ("db.sqlite" + name[len(database.name) :]),
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                    )
                    try:
                        while chunk := os.read(descriptor, 1024 * 1024):
                            check()
                            pending = memoryview(chunk)
                            while pending:
                                written = os.write(target, pending)
                                if written <= 0:
                                    raise OSError(
                                        "No progress writing the private queue check."
                                    )
                                pending = pending[written:]
                    finally:
                        os.close(target)
                    if _stamp(os.fstat(descriptor)) != _stamp(row):
                        raise QueueInputError(
                            "The source database changed during its private queue check."
                        )
                finally:
                    os.close(descriptor)
            for name in names:
                try:
                    now = os.stat(name, dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    now = None
                if (
                    (name in before) != (now is not None)
                    or now is not None
                    and _stamp(now) != _stamp(before[name])
                ):
                    raise QueueInputError(
                        "The source database/WAL changed during queue inspection; retry the check."
                    )
            if (source.lstat().st_dev, source.lstat().st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise QueueInputError(
                    "The source directory changed during queue inspection."
                )
            yield Path(temporary) / "db.sqlite"
            for name in names:
                try:
                    now = os.stat(name, dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    now = None
                if (
                    (name in before) != (now is not None)
                    or now is not None
                    and _stamp(now) != _stamp(before[name])
                ):
                    raise QueueInputError(
                        "The retained database/WAL changed during its private queue read."
                    )
            current = source.lstat()
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise QueueInputError(
                    "The source directory changed during its private queue read."
                )
    finally:
        os.close(root)


def _inspect_sqlite(plan, *, captured, check, scratch_root=None):
    source = plan["source"]
    with _private_sqlite_copy(
        Path(source["data_dir"]) / "db.sqlite",
        check=check,
        scratch_root=scratch_root,
        expected_identity=source.get("data_identity"),
    ) as database:
        with closing(
            sqlite3.connect(f"file:{quote(str(database))}?mode=ro", uri=True)
        ) as connection:
            connection.execute("PRAGMA query_only=ON")
            return sqlite_queue(connection, identities=captured, check=check)


def inspect_queue(plan, *, captured=False, check=None, connect=None, scratch_root=None):
    """A WAL-aware read-only observation; cold callers must handle failure closed."""
    cancel = check or (lambda: None)
    cancellation = None

    def checkpoint():
        nonlocal cancellation
        try:
            cancel()
        except Exception as error:
            cancellation = error
            raise

    checkpoint()
    source = plan["source"]
    if source.get("provider") in {"postgres", "postgresql"}:
        connection = None
        cleanup_failed = False
        try:
            connection = (connect or _connect_default)(**connection_settings(plan))
            _readonly(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT "Id" FROM public."QueueItems"'
                    if captured
                    else 'SELECT COUNT(*) FROM public."QueueItems"'
                )
                result = (
                    _ids(cursor, checkpoint)
                    if captured
                    else {"count": cursor.fetchone()[0]}
                )
            result["observed_at"] = int(time.time())
        except Exception as error:
            if error is cancellation:
                raise
            raise QueueInputError(
                "The PostgreSQL download queue could not be inspected safely; source tasks were not changed."
            ) from None
        finally:
            if connection is not None:
                for action in (connection.rollback, connection.close):
                    try:
                        action()
                    except Exception:
                        cleanup_failed = True
        if cleanup_failed:
            raise QueueInputError(
                "The read-only PostgreSQL queue inspection could not close safely; source tasks were not changed."
            )
        return result
    return _inspect_sqlite(
        plan, captured=captured, check=checkpoint, scratch_root=scratch_root
    )


def observe_queue(plan, *, check=None, scratch_root=None):
    cancellation = None

    def checkpoint():
        nonlocal cancellation
        try:
            if check is not None:
                check()
        except Exception as error:
            cancellation = error
            raise

    try:
        return inspect_queue(plan, check=checkpoint, scratch_root=scratch_root)
    except Exception as error:
        if error is cancellation:
            raise
        return None


def queue_matches(first, second):
    return all(
        first.get(key) == second.get(key) for key in ("count", "ids_xor", "ids_sum")
    )


def _file_digest(path, check):
    path = Path(path)
    before = _regular(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if _stamp(opened) != _stamp(before):
            raise QueueInputError("A retained queue input changed before verification.")
        while chunk := os.read(descriptor, 1024 * 1024):
            check()
            digest.update(chunk)
        if _stamp(os.fstat(descriptor)) != _stamp(before) or _stamp(
            path.lstat()
        ) != _stamp(before):
            raise QueueInputError("A retained queue input changed during verification.")
    finally:
        os.close(descriptor)
    return {
        "sha256": digest.hexdigest(),
        "size": before.st_size,
        "device": before.st_dev,
        "inode": before.st_ino,
    }


def _source_files(database, check):
    result = {"db.sqlite": _file_digest(database, check)}
    for suffix in ("-wal", "-journal"):
        path = Path(str(database) + suffix)
        if path.exists() or path.is_symlink():
            result["db.sqlite" + suffix] = _file_digest(path, check)
    return result


def _value(value):
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if value is None or isinstance(value, (str, int, float)):
        return [type(value).__name__, value]
    raise QueueInputError("A native source table contains an unsupported SQLite value.")


def _row_digest(rows, check):
    count, xor, total = 0, 0, 0
    for row in rows:
        check()
        raw = json.dumps(
            [_value(value) for value in row], ensure_ascii=False, separators=(",", ":")
        ).encode()
        number = int.from_bytes(hashlib.sha256(raw).digest(), "big")
        xor ^= number
        total = (total + number) % (1 << 256)
        count += 1
    return {"count": count, "xor": f"{xor:064x}", "sum": f"{total:064x}"}


def _quote(value):
    return '"' + str(value).replace('"', '""') + '"'


def _affinity(value):
    name = str(value).upper()
    if "INT" in name:
        return "INTEGER"
    if any(part in name for part in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if not name or "BLOB" in name:
        return "BLOB"
    if any(part in name for part in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def create_import_input(
    source, destination, expected_queue, *, check=None, progress=None
):
    """Project exactly the native seven tables, leaving both queue tables empty.

    No source SQL mutation, copied trigger, foreign-key cascade or cleanup worker
    participates. Every non-queue row is independently reconciled before publish.
    """
    cancel = check or (lambda: None)
    report = progress or (lambda *args, **kwargs: None)
    cancel()
    source, destination = Path(source), Path(destination)
    if destination.exists() or destination.is_symlink():
        raise QueueInputError("The private queue-free import destination is occupied.")
    original = _source_files(source, cancel)
    descriptor = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    os.close(descriptor)
    complete = False
    try:
        with _private_sqlite_copy(
            source, check=cancel, scratch_root=destination.parent
        ) as private_source:
            with (
                closing(
                    sqlite3.connect(
                        f"file:{quote(str(private_source))}?mode=ro", uri=True
                    )
                ) as old,
                closing(sqlite3.connect(destination)) as new,
            ):
                old.execute("PRAGMA query_only=ON")
                old.execute("BEGIN")
                captured = sqlite_queue(old, identities=True, check=cancel)
                if not queue_matches(captured, expected_queue):
                    raise QueueInputError(
                        "The captured download queue changed after the stopped-source check; source tasks were retained."
                    )
                new.execute("PRAGMA journal_mode=DELETE")
                tables = {}
                for index, table in enumerate(LEGACY_TABLES, start=1):
                    cancel()
                    report(
                        "preparing_import_database",
                        "Preparing a separate native import database. Original queued downloads and history remain retained.",
                        table_index=index,
                        table_count=len(LEGACY_TABLES),
                    )
                    columns = old.execute(
                        f"PRAGMA table_info({_quote(table)})"
                    ).fetchall()
                    if not columns:
                        raise QueueInputError(
                            "The captured source is missing a required native import table."
                        )
                    names = [column[1] for column in columns]
                    definition = ",".join(
                        f"{_quote(column[1])} {_affinity(column[2])}"
                        for column in columns
                    )
                    new.execute(f"CREATE TABLE {_quote(table)} ({definition})")
                    selected = ",".join(_quote(name) for name in names)
                    sql = f"SELECT {selected} FROM {_quote(table)}"
                    if table not in {"QueueItems", "QueueNzbContents"}:
                        values = ",".join("?" for _ in names)
                        for row in old.execute(sql):
                            cancel()
                            new.execute(
                                f"INSERT INTO {_quote(table)} VALUES ({values})", row
                            )
                        expected = _row_digest(old.execute(sql), cancel)
                        if _row_digest(new.execute(sql), cancel) != expected:
                            raise QueueInputError(
                                "A non-queue source table changed in the derived native input."
                            )
                        tables[table] = {"columns": names, **expected}
                    else:
                        tables[table] = {
                            "columns": names,
                            "omitted_count": old.execute(
                                f"SELECT COUNT(*) FROM {_quote(table)}"
                            ).fetchone()[0],
                        }
                new.commit()
                if new.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise QueueInputError(
                        "The derived queue-free import database did not validate."
                    )
                if sqlite_queue(new)["count"] != 0:
                    raise QueueInputError(
                        "The derived native input still contains queued work."
                    )
        if _source_files(source, cancel) != original:
            raise QueueInputError(
                "The retained source database changed while the queue-free input was derived."
            )
        with destination.open("rb") as handle:
            os.fsync(handle.fileno())
        derived = _file_digest(destination, cancel)
        complete = True
        return {
            "version": 1,
            "policy": "omit_from_v2",
            "source_files": original,
            "queue": captured,
            "tables": tables,
            "derived": derived,
            "omitted_count": captured["count"],
        }
    finally:
        if not complete:
            destination.unlink(missing_ok=True)


def verify_import_input(source, candidate, receipt, *, check=None):
    cancel = check or (lambda: None)
    cancel()
    if receipt.get("version") != 1 or receipt.get("policy") != "omit_from_v2":
        raise QueueInputError(
            "The queue-free input has no valid retained derivation receipt."
        )
    if _source_files(Path(source), cancel) != receipt.get("source_files"):
        raise QueueInputError(
            "The original retained database changed after queue omission."
        )
    current = _file_digest(Path(candidate), cancel)
    if current["sha256"] != (receipt.get("derived") or {}).get("sha256"):
        raise QueueInputError(
            "The private queue-free import database changed before native startup."
        )
    with closing(
        sqlite3.connect(f"file:{quote(str(candidate))}?mode=ro", uri=True)
    ) as connection:
        if (
            sqlite_queue(connection)["count"] != 0
            or connection.execute('SELECT COUNT(*) FROM "QueueNzbContents"').fetchone()[
                0
            ]
            != 0
        ):
            raise QueueInputError(
                "Queued work reappeared in the private native import input."
            )
    return True
