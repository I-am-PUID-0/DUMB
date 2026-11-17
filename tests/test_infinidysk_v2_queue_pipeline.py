"""Real capture/recovery keeps queued work out of v2 and intact in v1."""

from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock, patch

from tests import test_infinidysk_v2_migration as fixtures
from utils.backup_storage import BackupStorage
from utils import backup_storage as storage_module
from utils import infinidysk_v2_space_control as reuse
from utils import infinidysk_v2_blob_staging as staging
from utils.infinidysk_v2_queue import QueueInputError, _private_sqlite_copy
from utils.infinidysk_v2_inventory import tree_fingerprint
from utils.infinidysk_v2_credentials import source_sab_key


class QueueCapturePipelineTests(unittest.TestCase):
    new_manager = fixtures.InfiniDyskV2MigrationTests.new_manager
    tearDown = fixtures.InfiniDyskV2MigrationTests.tearDown

    def setUp(self):
        fixtures.InfiniDyskV2MigrationTests.setUp(self)
        self.executor.supports_external_blob_staging = True
        self.executor._execution_ids = lambda: (
            os.geteuid(),
            os.getegid(),
        )
        with closing(sqlite3.connect(self.source / "db.sqlite")) as db:
            db.executescript(
                "CREATE TABLE Accounts(Id TEXT,Username TEXT);"
                "CREATE TABLE DavItems(Id TEXT,Type INTEGER,Path TEXT);"
                "INSERT INTO DavItems VALUES('media',2,'/content/film.mkv');"
                "CREATE TABLE HistoryItems(Id TEXT,DownloadStatus INTEGER,FailMessage TEXT);"
                "INSERT INTO HistoryItems VALUES('completed',1,NULL);"
                "INSERT INTO HistoryItems VALUES('failed',2,'retained failure');"
                "CREATE TABLE QueueNzbContents(Id INTEGER,NzbContents BLOB);"
                "CREATE TRIGGER source_delete_is_destructive AFTER DELETE ON QueueItems BEGIN "
                "DELETE FROM DavItems; UPDATE OriginalData SET Value='BAD'; END;"
            )
            db.executemany(
                "INSERT INTO QueueItems VALUES(?)", [(value,) for value in range(13)]
            )
            db.executemany(
                "INSERT INTO QueueNzbContents VALUES(?,?)",
                [(value, b"retained NZB input") for value in range(13)],
            )
            db.commit()
        self.original_database = (self.source / "db.sqlite").read_bytes()
        self.expected_hash = hashlib.sha256(self.original_database).hexdigest()
        self.external = self.root / "external"
        self.external.mkdir(mode=0o700)
        self.storage = BackupStorage(
            self.config, state_root=self.root / "storage-control"
        )
        self.storage.save_profile(
            {
                "id": "nas",
                "name": "Recovery archive",
                "path": str(self.external),
                "enabled": True,
            }
        )
        for module in (storage_module, reuse, staging):
            current = patch.object(
                module, "get_backup_storage", return_value=self.storage
            )
            current.start()
            self.addCleanup(current.stop)
        # Device separation alone is supplied by this fixture. All archive,
        # database, input hashes, identity checks and restore work use real files.
        for module, name in (
            (reuse, "preflight_reuse"),
            (staging, "require_external_filesystem"),
        ):
            current = patch.object(module, name)
            current.start()
            self.addCleanup(current.stop)

    def capture(
        self, *, archive=False, reclaim=False, offload=False, preserved_sidecars=None
    ):
        options = {
            "mode": "migrate",
            "mount_backend": "rclone",
            "omit_queued_downloads": True,
        }
        if archive:
            options["backup_destination_id"] = "nas"
        if reclaim:
            options["reclaim_source_space"] = True
        if offload:
            options["offload_legacy_blobs"] = True
        plan = self.manager._plan(options)
        self.assertEqual([], plan["blockers"])
        job = {
            "job_id": "d" * 32,
            "mode": "rehearsal",
            "status": "running",
            "worker_id": self.manager._worker_id,
            "plan": plan,
            "plan_fingerprint": plan["plan_fingerprint"],
        }
        self.manager._active_id = job["job_id"]
        self.manager._write(self.manager._job_path(job["job_id"]), job)
        workspace = self.manager._workspace(job)
        workspace["check_source_queue"]()
        snapshot = workspace["capture_source"]()
        self.assertEqual(13, snapshot["source_schema"]["queue_count"])
        source = Path(snapshot["source_database"])
        retained_tree = snapshot.get("data_snapshot")
        retained_fingerprint = snapshot["original_fingerprints"][
            plan["source"]["data_dir"]
        ]
        if retained_tree:
            # Every later credential/inventory reader receives this closed
            # standalone database, never the raw rollback tree's SQLite/WAL.
            self.assertNotIn(Path(retained_tree), source.parents)
            self.assertEqual(
                retained_fingerprint,
                tree_fingerprint(Path(retained_tree), scratch_root=workspace["root"]),
            )
        self.assertEqual(
            "private-example-key",
            source_sab_key(source, plan["source"]["service"].get("env") or {}),
        )
        # The test must not open the retained rollback copy itself: even an
        # SQLite SELECT may update its shared-memory reader marks.
        with _private_sqlite_copy(
            source, check=Mock(), scratch_root=workspace["root"]
        ) as private:
            with closing(sqlite3.connect(private)) as db:
                self.assertEqual(
                    13, db.execute("SELECT COUNT(*) FROM QueueItems").fetchone()[0]
                )
                self.assertEqual(
                    13,
                    db.execute("SELECT COUNT(*) FROM QueueNzbContents").fetchone()[0],
                )
        original_retained = source.read_bytes()
        workspace["prepare_import_database"]()
        workspace["verify_queue_input"]()
        candidate = Path(workspace["candidate_data"])
        with closing(sqlite3.connect(candidate / "db.sqlite")) as db:
            self.assertEqual(
                0, db.execute("SELECT COUNT(*) FROM QueueItems").fetchone()[0]
            )
            self.assertEqual(
                0, db.execute("SELECT COUNT(*) FROM QueueNzbContents").fetchone()[0]
            )
            self.assertEqual(
                [("media", 2, "/content/film.mkv")],
                db.execute("SELECT * FROM DavItems").fetchall(),
            )
            self.assertEqual(
                [("completed", 1, None), ("failed", 2, "retained failure")],
                db.execute("SELECT * FROM HistoryItems ORDER BY Id").fetchall(),
            )
            self.assertEqual(
                [],
                db.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall(),
            )
        self.assertEqual(original_retained, source.read_bytes())
        if retained_tree:
            self.assertEqual(
                retained_fingerprint,
                tree_fingerprint(Path(retained_tree), scratch_root=workspace["root"]),
            )
        self.assertTrue(job["queue_input_verified"])
        self.assertEqual(13, job["source_queue"]["omitted_count"])
        self.assertTrue(job["source_queue"]["source_queue_preserved"])
        if archive:
            self.assertTrue(job["snapshot"]["cold_archive"]["verified"])
        # Simulate native mutation only in its derived working database.
        with closing(sqlite3.connect(candidate / "db.sqlite")) as db:
            db.execute("DELETE FROM DavItems")
            db.commit()
        if reclaim:
            self.assertFalse(self.source.exists())
            workspace["restore_source"](Mock())
            self.assertEqual("restored", job["snapshot"]["space_recovery"]["state"])
        workspace["restore_config"]()
        self.assertEqual(
            self.expected_hash,
            hashlib.sha256((self.source / "db.sqlite").read_bytes()).hexdigest(),
        )
        # Compare bytes before the test itself opens SQLite to inspect restored
        # rows: that independent read may update this test-owned SHM's reader marks.
        for suffix, original in (preserved_sidecars or {}).items():
            self.assertEqual(
                original, Path(str(self.source / "db.sqlite") + suffix).read_bytes()
            )
        with closing(sqlite3.connect(self.source / "db.sqlite")) as db:
            self.assertEqual(
                13, db.execute("SELECT COUNT(*) FROM QueueItems").fetchone()[0]
            )
            self.assertEqual(
                13, db.execute("SELECT COUNT(*) FROM QueueNzbContents").fetchone()[0]
            )
            self.assertEqual(
                ("original",), db.execute("SELECT Value FROM OriginalData").fetchone()
            )
            self.assertEqual(
                1, db.execute("SELECT COUNT(*) FROM DavItems").fetchone()[0]
            )
            self.assertEqual(
                2, db.execute("SELECT COUNT(*) FROM HistoryItems").fetchone()[0]
            )
        if offload:
            self.assertEqual(
                "cleaned", job["snapshot"]["external_blob_staging"]["state"]
            )
        self.assertEqual(
            "original metadata", (self.source / "blobs/original").read_text()
        )
        self.manager._active_id = None
        return job, workspace

    def test_local_tree_capture_preserves_full_queue_and_history(self):
        self.capture()

    def test_wal_capture_preserves_uncheckpointed_queue_and_original_sidecars(self):
        connection = sqlite3.connect(self.source / "db.sqlite")
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("UPDATE QueueItems SET Id=Id+100")
        connection.execute("UPDATE QueueNzbContents SET Id=Id+100")
        connection.commit()
        database = self.source / "db.sqlite"
        self.expected_hash = hashlib.sha256(database.read_bytes()).hexdigest()
        sidecars = {
            suffix: Path(str(database) + suffix).read_bytes()
            for suffix in ("-wal", "-shm")
        }
        self.capture(preserved_sidecars=sidecars)
        self.assertEqual(
            (100, 112),
            connection.execute("SELECT MIN(Id),MAX(Id) FROM QueueItems").fetchone(),
        )

    def test_archive_capture_preserves_full_queue_and_history(self):
        self.capture(archive=True)

    def test_reused_directory_restores_original_queue_after_candidate_mutation(self):
        self.capture(archive=True, reclaim=True)

    def test_offloaded_blobs_restore_original_queue_from_verified_archive(self):
        self.capture(archive=True, reclaim=True, offload=True)

    def test_derived_candidate_change_fails_its_native_startup_receipt(self):
        job, workspace = self.capture()
        self.manager._active_id = job["job_id"]
        with self.assertRaisesRegex(QueueInputError, "changed"):
            workspace["verify_queue_input"]()
        self.manager._active_id = None
