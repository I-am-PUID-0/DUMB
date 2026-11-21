"""The contained native import must retain old history until DUMB accepts it."""

from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
import uuid

from utils.infinidysk_v2_history import (
    HistoryGuardError,
    prepare_history_guard,
    remove_history_guard,
)


class NativeHistoryGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "bootstrap.sqlite"
        self.target = self.root / "infinidysk.sqlite"
        self.uid, self.gid = os.geteuid(), os.getegid()
        with closing(sqlite3.connect(self.source)) as db, db:
            db.executescript(
                "CREATE TABLE _sqlx_migrations(version INTEGER,success INTEGER,checksum BLOB);"
                "INSERT INTO _sqlx_migrations VALUES(1,1,x'00');"
                "CREATE TABLE items(id BLOB PRIMARY KEY,parent_id BLOB,name TEXT,path TEXT,kind INTEGER);"
                "CREATE TABLE history(id BLOB PRIMARY KEY,created_at INTEGER,status INTEGER);"
                "CREATE TABLE queue(id BLOB PRIMARY KEY);"
                "CREATE TABLE blobs(id BLOB PRIMARY KEY);"
                "CREATE TABLE accounts(kind INTEGER,username TEXT,password_hash TEXT);"
                "CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                "CREATE TABLE quotas(provider BLOB,used_bytes INTEGER);"
            )
            db.executemany(
                "INSERT INTO items VALUES(?,?,?,?,1)",
                [
                    (bytes(16), None, "", "/"),
                    ((1).to_bytes(16, "big"), bytes(16), "content", "/content"),
                ],
            )
            db.executemany(
                "INSERT INTO config VALUES(?,?)",
                [
                    ("auth", '{"api_key":"generated-bootstrap-only"}'),
                    ("cleanup.last_run", "1234"),
                ],
            )

    def prepare(self, **kwargs):
        return prepare_history_guard(
            self.source,
            self.target,
            native_stopped=True,
            uid=self.uid,
            gid=self.gid,
            **kwargs,
        )

    def remove(self, receipt, **kwargs):
        return remove_history_guard(
            self.target,
            receipt,
            native_stopped=True,
            uid=self.uid,
            gid=self.gid,
            **kwargs,
        )

    def state(self, path):
        with closing(sqlite3.connect(path)) as db:
            return tuple(db.iterdump())

    def completed(self, db):
        # Match pinned Db::write_config's upsert, including the two-stage update.
        db.execute(
            "INSERT INTO config(key,value) VALUES('upgrade.state','\"started\"') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        self.assertIsNone(
            db.execute(
                "SELECT value FROM config WHERE key='cleanup.last_run'"
            ).fetchone()
        )
        db.execute(
            "INSERT INTO config(key,value) VALUES('upgrade.state','\"done\"') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )

    def test_actual_completion_delays_native_500_row_cleanup_without_losing_history(
        self,
    ):
        source_before = self.state(self.source)
        receipt = self.prepare()
        self.assertEqual(self.state(self.source), source_before)
        old = int(time.time() * 1000) - 60 * 86400 * 1000
        ids = [uuid.UUID(int=value).bytes for value in range(100, 1101)]
        with closing(sqlite3.connect(self.target)) as db, db:
            self.assertEqual(db.execute("SELECT * FROM config").fetchall(), [])
            db.executemany(
                "INSERT INTO history VALUES(?,?,2)", [(value, old) for value in ids]
            )
            db.execute(
                "INSERT INTO config VALUES('sab','{\"api_key\":\"real-imported-key\"}')"
            )
            self.completed(db)
            last = int(
                db.execute(
                    "SELECT value FROM config WHERE key='cleanup.last_run'"
                ).fetchone()[0]
            )
            # The native worker only calls prune_history if this daily due check is <=0.
            wait = last + 86400 * 1000 - int(time.time() * 1000)
            self.assertGreater(wait, 86399 * 1000)
            if wait <= 0:
                db.execute(
                    "DELETE FROM history WHERE id IN (SELECT id FROM history "
                    "WHERE created_at < ? ORDER BY created_at LIMIT 500)",
                    (int(time.time() * 1000) - 30 * 86400 * 1000,),
                )
            self.assertEqual(
                {row[0] for row in db.execute("SELECT id FROM history")}, set(ids)
            )
        self.assertEqual(
            self.remove(receipt),
            {"history_guard_applied": True, "history_guard_removed": True},
        )
        with closing(sqlite3.connect(self.target)) as db:
            self.assertEqual(
                {row[0] for row in db.execute("SELECT id FROM history")}, set(ids)
            )
            self.assertEqual(
                dict(db.execute("SELECT key,value FROM config")),
                {"upgrade.state": '"done"', "sab": '{"api_key":"real-imported-key"}'},
            )
            self.assertEqual(
                db.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall(),
                [],
            )

    def test_done_insert_is_also_atomic_and_initial_config_remains_empty(self):
        receipt = self.prepare()
        with closing(sqlite3.connect(self.target)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM config").fetchone())
            db.execute("BEGIN")
            db.execute("INSERT INTO config VALUES('upgrade.state','\"done\"')")
            self.assertIsNotNone(
                db.execute(
                    "SELECT value FROM config WHERE key='cleanup.last_run'"
                ).fetchone()
            )
            db.rollback()
            self.assertIsNone(db.execute("SELECT 1 FROM config").fetchone())
            with db:
                db.execute("INSERT INTO config VALUES('upgrade.state','\"done\"')")
        self.remove(receipt)

    def test_wal_backup_includes_committed_schema_and_generated_state(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                db.execute("CREATE TABLE empty_wal_table(id INTEGER)")
            self.assertTrue(Path(str(self.source) + "-wal").exists())
            self.prepare()
            with closing(sqlite3.connect(self.target)) as copied:
                self.assertIsNotNone(
                    copied.execute(
                        "SELECT name FROM sqlite_master WHERE name='empty_wal_table'"
                    ).fetchone()
                )

    def test_official_startup_planner_statistics_are_not_user_records(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("ANALYZE")
            self.assertGreater(
                db.execute("SELECT count(*) FROM sqlite_stat1").fetchone()[0], 0
            )
        self.prepare()
        with closing(sqlite3.connect(self.target)) as db:
            self.assertGreater(
                db.execute("SELECT count(*) FROM sqlite_stat1").fetchone()[0], 0
            )
            self.assertEqual(db.execute("SELECT count(*) FROM items").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT * FROM config").fetchall(), [])

    def test_existing_target_is_preserved(self):
        self.target.write_bytes(b"existing user data")
        with self.assertRaises(HistoryGuardError):
            self.prepare()
        self.assertEqual(self.target.read_bytes(), b"existing user data")

    def test_live_bootstrap_and_candidate_are_never_modified(self):
        before = self.state(self.source)
        with self.assertRaisesRegex(HistoryGuardError, "shutdown"):
            prepare_history_guard(
                self.source,
                self.target,
                native_stopped=False,
                uid=self.uid,
                gid=self.gid,
            )
        self.assertFalse(self.target.exists())
        self.assertEqual(self.state(self.source), before)
        receipt = self.prepare()
        with closing(sqlite3.connect(self.target)) as db, db:
            self.completed(db)
        before = self.state(self.target)
        with self.assertRaisesRegex(HistoryGuardError, "shutdown"):
            remove_history_guard(
                self.target, receipt, native_stopped=False, uid=self.uid, gid=self.gid
            )
        self.assertEqual(self.state(self.target), before)

    def test_nonfresh_bootstrap_is_refused_before_any_candidate_is_created(self):
        for mutation in (
            "INSERT INTO history VALUES(randomblob(16),1,2)",
            "INSERT INTO queue VALUES(randomblob(16))",
            "INSERT INTO accounts VALUES(1,'existing-user','existing-hash')",
            "INSERT INTO blobs VALUES(randomblob(16))",
            "INSERT INTO items VALUES(randomblob(16),NULL,'file','/content/file',2)",
            "INSERT INTO quotas VALUES(randomblob(16),1234)",
            "INSERT INTO config VALUES('upgrade.state','\"done\"')",
        ):
            with self.subTest(mutation=mutation):
                other = self.root / "nonfresh.sqlite"
                shutil.copyfile(self.source, other)
                with closing(sqlite3.connect(other)) as db, db:
                    db.execute(mutation)
                with self.assertRaises(HistoryGuardError):
                    prepare_history_guard(
                        other,
                        self.target,
                        native_stopped=True,
                        uid=self.uid,
                        gid=self.gid,
                    )
                self.assertFalse(self.target.exists())
                other.unlink()

    def test_invalid_schema_and_seed_shapes_are_refused(self):
        for mutation in (
            "DROP TABLE accounts",
            "ALTER TABLE history RENAME COLUMN status TO unexpected",
            "UPDATE items SET name='wrong' WHERE path='/content'",
            "CREATE VIEW unexpected AS SELECT * FROM history",
            "CREATE TRIGGER unexpected AFTER INSERT ON history BEGIN SELECT 1; END",
        ):
            with self.subTest(mutation=mutation):
                altered = self.root / "altered.sqlite"
                shutil.copyfile(self.source, altered)
                with closing(sqlite3.connect(altered)) as db, db:
                    db.execute(mutation)
                with self.assertRaises(HistoryGuardError):
                    prepare_history_guard(
                        altered,
                        self.target,
                        native_stopped=True,
                        uid=self.uid,
                        gid=self.gid,
                    )
                self.assertFalse(self.target.exists())
                altered.unlink()

    def test_database_links_and_wrong_owner_are_refused(self):
        alias = self.root / "alias"
        alias.symlink_to(self.source)
        with self.assertRaisesRegex(HistoryGuardError, "links"):
            prepare_history_guard(
                alias, self.target, native_stopped=True, uid=self.uid, gid=self.gid
            )
        alias.unlink()
        os.link(self.source, alias)
        with self.assertRaisesRegex(HistoryGuardError, "owned regular"):
            self.prepare()
        alias.unlink()
        parent_alias = self.root / "parent-alias"
        parent_alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(HistoryGuardError, "links"):
            prepare_history_guard(
                parent_alias / self.source.name,
                self.target,
                native_stopped=True,
                uid=self.uid,
                gid=self.gid,
            )
        with self.assertRaisesRegex(HistoryGuardError, "owned regular"):
            prepare_history_guard(
                self.source,
                self.target,
                native_stopped=True,
                uid=self.uid + 1,
                gid=self.gid,
            )

    def test_changed_guard_or_database_identity_is_retained_without_cleanup(self):
        receipt = self.prepare()
        with closing(sqlite3.connect(self.target)) as db, db:
            self.completed(db)
            (name,) = db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name LIMIT 1"
            ).fetchone()
            db.execute(f'DROP TRIGGER "{name}"')
            db.execute(
                f'CREATE TRIGGER "{name}" AFTER INSERT ON config BEGIN SELECT 1; END'
            )
        before = self.state(self.target)
        with self.assertRaisesRegex(HistoryGuardError, "definitions changed"):
            self.remove(receipt)
        self.assertEqual(self.state(self.target), before)
        replaced = self.root / "replacement.sqlite"
        shutil.copyfile(self.target, replaced)
        os.replace(replaced, self.target)
        with self.assertRaisesRegex(HistoryGuardError, "identity changed"):
            self.remove(receipt)
        self.assertEqual(self.state(self.target), before)

    def test_unfinished_or_missing_maintenance_marker_does_not_remove_guards(self):
        receipt = self.prepare()
        before = self.state(self.target)
        with self.assertRaisesRegex(HistoryGuardError, "completion"):
            self.remove(receipt)
        self.assertEqual(self.state(self.target), before)
        with closing(sqlite3.connect(self.target)) as db, db:
            self.completed(db)
            db.execute("DELETE FROM config WHERE key='cleanup.last_run'")
        before = self.state(self.target)
        with self.assertRaisesRegex(HistoryGuardError, "completion"):
            self.remove(receipt)
        self.assertEqual(self.state(self.target), before)

    def test_invalid_receipt_or_timestamp_is_not_used_to_remove_protection(self):
        receipt = self.prepare()
        with closing(sqlite3.connect(self.target)) as db, db:
            self.completed(db)
        before = self.state(self.target)
        for altered in (
            {**receipt, "version": True},
            {**receipt, "guard_sha256": "changed"},
            {**receipt, "unexpected": "field"},
        ):
            with self.subTest(receipt=altered):
                with self.assertRaisesRegex(HistoryGuardError, "receipt"):
                    self.remove(altered)
                self.assertEqual(self.state(self.target), before)
        for value in ("0", "-1", "9" * 30, "１２３", "not-a-timestamp"):
            with self.subTest(timestamp=value):
                with closing(sqlite3.connect(self.target)) as db, db:
                    db.execute(
                        "UPDATE config SET value=? WHERE key='cleanup.last_run'",
                        (value,),
                    )
                before = self.state(self.target)
                with self.assertRaisesRegex(HistoryGuardError, "completion"):
                    self.remove(receipt)
                self.assertEqual(self.state(self.target), before)
