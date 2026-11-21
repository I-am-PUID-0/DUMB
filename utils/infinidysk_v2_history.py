"""Defer native daily cleanup while the isolated C# import is inspected.

The pinned native server starts daily cleanup as soon as import finishes. These
temporary candidate-only triggers record the real completion time atomically,
before that worker reads its schedule. They never manufacture import completion.
Both processes must be proven stopped by their owner before calling this module.
"""

from contextlib import closing, contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
from urllib.parse import quote


class HistoryGuardError(ValueError):
    """The temporary native maintenance boundary could not be verified."""


_LAST_KEY = "cleanup.last_run"
_STATE_KEY = "upgrade.state"
_TRIGGERS = {
    "dumb_import_defer_cleanup_insert": (
        "CREATE TRIGGER dumb_import_defer_cleanup_insert AFTER INSERT ON config "
        "WHEN NEW.key = 'upgrade.state' AND NEW.value = '\"done\"' "
        "BEGIN INSERT INTO config(key,value) "
        "VALUES('cleanup.last_run',CAST(CAST(strftime('%s','now') AS INTEGER)*1000 AS TEXT)) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value; END"
    ),
    "dumb_import_defer_cleanup_update": (
        "CREATE TRIGGER dumb_import_defer_cleanup_update AFTER UPDATE OF value ON config "
        "WHEN NEW.key = 'upgrade.state' AND NEW.value = '\"done\"' "
        "BEGIN INSERT INTO config(key,value) "
        "VALUES('cleanup.last_run',CAST(CAST(strftime('%s','now') AS INTEGER)*1000 AS TEXT)) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value; END"
    ),
}
_GUARD_SHA256 = hashlib.sha256("\n".join(_TRIGGERS.values()).encode()).hexdigest()
_REQUIRED = {
    "_sqlx_migrations": {"version", "success", "checksum"},
    "items": {"id", "parent_id", "name", "path", "kind"},
    "history": {"id", "created_at", "status"},
    "queue": {"id"},
    "blobs": {"id"},
    "accounts": {"kind", "username", "password_hash"},
    "config": {"key", "value"},
}


def _stopped(native_stopped, uid, gid):
    if native_stopped is not True:
        raise HistoryGuardError(
            "Native shutdown must be verified before history protection changes."
        )
    if any(type(value) is not int or not 0 <= value < 2**31 for value in (uid, gid)):
        raise HistoryGuardError(
            "Native history protection requires verified ownership."
        )


def _path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise HistoryGuardError(
            "Native history protection requires a direct absolute database path."
        )
    try:
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise HistoryGuardError(
                "Native history protection cannot follow database path links."
            )
        if not path.parent.is_dir():
            raise HistoryGuardError("The native database parent is unavailable.")
    except OSError:
        raise HistoryGuardError(
            "The native database path could not be inspected safely."
        ) from None
    return path


def _identity(value, uid, gid):
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_nlink != 1
        or (value.st_uid, value.st_gid) != (uid, gid)
    ):
        raise HistoryGuardError(
            "Native history protection requires its owned regular database."
        )
    return {"device": value.st_dev, "inode": value.st_ino, "uid": uid, "gid": gid}


@contextmanager
def _database(value, *, uid, gid, writable=False, expected=None):
    path = _path(value)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        identity = _identity(os.fstat(descriptor), uid, gid)
        if expected is not None and identity != expected:
            raise HistoryGuardError("The guarded native database identity changed.")
        if _identity(path.lstat(), uid, gid) != identity:
            raise HistoryGuardError("The native database changed while opening it.")
        mode = "rw" if writable else "ro"
        with closing(
            sqlite3.connect(f"file:{quote(str(path))}?mode={mode}", uri=True)
        ) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            # The pinned guard uses only built-in innocuous SQLite functions.
            if _identity(path.lstat(), uid, gid) != identity:
                raise HistoryGuardError("The native database changed while opening it.")
            yield db, identity
            if _identity(path.lstat(), uid, gid) != identity:
                raise HistoryGuardError(
                    "The native database changed during history protection."
                )
    finally:
        os.close(descriptor)


def _schema(db):
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise HistoryGuardError(
            "The native bootstrap database did not pass integrity checks."
        )
    objects = db.execute("SELECT name,type,sql FROM sqlite_master").fetchall()
    tables = {name for name, kind, _ in objects if kind == "table"}
    if not set(_REQUIRED).issubset(tables):
        raise HistoryGuardError("The pinned native bootstrap schema is incomplete.")
    if any(kind in {"trigger", "view"} for _, kind, _ in objects):
        raise HistoryGuardError(
            "The native bootstrap has unexpected executable schema objects."
        )
    for table, required in _REQUIRED.items():
        columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
        if not required.issubset(columns):
            raise HistoryGuardError(
                "The pinned native bootstrap columns are unsupported."
            )
    return tables


def _fresh(db, *, generated_config):
    tables = _schema(db)
    expected = {
        (bytes(16), None, "", "/", 1),
        ((1).to_bytes(16, "big"), bytes(16), "content", "/content", 1),
    }
    rows = db.execute("SELECT id,parent_id,name,path,kind FROM items").fetchall()
    if len(rows) != 2 or set(rows) != expected:
        raise HistoryGuardError(
            "The native bootstrap contains media or unexpected seed folders."
        )
    # Official startup optimization populates these SQLite-owned planner tables
    # even when only ROOT/CONTENT exist. They do not represent imported records.
    for table in tables - {
        "_sqlx_migrations",
        "items",
        "config",
        "sqlite_sequence",
        "sqlite_stat1",
        "sqlite_stat4",
    }:
        # SQLite schema identifiers are fully quoted, never interpolated raw.
        escaped = table.replace('"', '""')
        query = f'SELECT 1 FROM "{escaped}" LIMIT 1'  # noqa: S608
        if db.execute(query).fetchone() is not None:
            raise HistoryGuardError(
                "The native bootstrap contains user or runtime records."
            )
    config = db.execute("SELECT key,value FROM config").fetchall()
    if (
        (not generated_config and config)
        or len(config) > 128
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or key.startswith(("upgrade.", "dumb_"))
            for key, value in config
        )
    ):
        raise HistoryGuardError(
            "The native bootstrap configuration is not fresh generated state."
        )


def _guard(db):
    rows = db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
    ).fetchall()
    if dict(rows) != _TRIGGERS:
        raise HistoryGuardError(
            "The temporary native history protection definitions changed."
        )


def prepare_history_guard(
    bootstrap_database, candidate_database, *, native_stopped, uid, gid
):
    """Clone a stopped official empty bootstrap and install temporary scheduling guards.

    A failed attempt retains any exclusively created candidate for its owning
    migration to inspect; this helper never removes an unproven filesystem item.
    """
    _stopped(native_stopped, uid, gid)
    try:
        target = _path(candidate_database)
        with _database(bootstrap_database, uid=uid, gid=gid) as (source, _):
            _fresh(source, generated_config=True)
            descriptor = os.open(
                target, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            try:
                if os.geteuid() == 0:
                    os.fchown(descriptor, uid, gid)
                identity = _identity(os.fstat(descriptor), uid, gid)
            finally:
                os.close(descriptor)
            with _database(
                target, uid=uid, gid=gid, writable=True, expected=identity
            ) as (db, _):
                source.backup(db)
                _fresh(db, generated_config=True)
                with db:
                    db.execute("DELETE FROM config")
                    _fresh(db, generated_config=False)
                    for definition in _TRIGGERS.values():
                        db.execute(definition)
                _guard(db)
                if db.execute("SELECT 1 FROM config LIMIT 1").fetchone() is not None:
                    raise HistoryGuardError(
                        "The guarded native candidate is not fresh for import."
                    )
        return {
            "version": 1,
            "database_identity": identity,
            "guard_sha256": _GUARD_SHA256,
        }
    except (OSError, sqlite3.Error):
        raise HistoryGuardError(
            "The native history protection bootstrap could not be completed safely."
        ) from None


def remove_history_guard(candidate_database, receipt, *, native_stopped, uid, gid):
    """Remove exact owned scheduling guards from the stopped, completed candidate."""
    _stopped(native_stopped, uid, gid)
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"version", "database_identity", "guard_sha256"}
        or type(receipt.get("version")) is not int
        or receipt.get("version") != 1
        or receipt.get("guard_sha256") != _GUARD_SHA256
        or not isinstance(receipt.get("database_identity"), dict)
        or set(receipt["database_identity"]) != {"device", "inode", "uid", "gid"}
        or any(
            type(value) is not int for value in receipt["database_identity"].values()
        )
    ):
        raise HistoryGuardError("The native history protection receipt is invalid.")
    try:
        with _database(
            candidate_database,
            uid=uid,
            gid=gid,
            writable=True,
            expected=receipt["database_identity"],
        ) as (db, _):
            with db:
                db.execute("BEGIN IMMEDIATE")
                _guard(db)
                done = db.execute(
                    "SELECT value FROM config WHERE key=?", (_STATE_KEY,)
                ).fetchall()
                last = db.execute(
                    "SELECT value FROM config WHERE key=?", (_LAST_KEY,)
                ).fetchall()
                if (
                    done != [('"done"',)]
                    or len(last) != 1
                    or not isinstance(last[0][0], str)
                    or not 1 <= len(last[0][0]) <= 19
                    or not last[0][0].isascii()
                    or not last[0][0].isdigit()
                    or not 0 < int(last[0][0]) < 2**63
                ):
                    raise HistoryGuardError(
                        "The native import completion maintenance boundary is unverified."
                    )
                for name in _TRIGGERS:
                    db.execute(f'DROP TRIGGER "{name}"')
                db.execute("DELETE FROM config WHERE key=?", (_LAST_KEY,))
                if (
                    db.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger'"
                    ).fetchone()
                    is not None
                ):
                    raise HistoryGuardError(
                        "Temporary native history protection remains installed."
                    )
                if (
                    db.execute(
                        "SELECT 1 FROM config WHERE key=?", (_LAST_KEY,)
                    ).fetchone()
                    is not None
                ):
                    raise HistoryGuardError(
                        "The temporary native cleanup schedule remains installed."
                    )
        return {"history_guard_applied": True, "history_guard_removed": True}
    except (OSError, sqlite3.Error):
        raise HistoryGuardError(
            "The stopped native history protection could not be removed safely."
        ) from None
