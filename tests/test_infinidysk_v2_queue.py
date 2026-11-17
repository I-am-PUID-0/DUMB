from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from utils.infinidysk_v2_queue import (
    QueueInputError,
    create_import_input,
    inspect_queue,
    observe_queue,
    public_queue_state,
    sqlite_queue,
    verify_import_input,
)
from utils.infinidysk_v2_migration import InfiniDyskV2Cancelled
from tests import test_infinidysk_v2_migration as migration_fixture


def populate(database):
    with sqlite3.connect(database) as db:
        db.executescript(
            "CREATE TABLE __EFMigrationsHistory(MigrationId TEXT);"
            "INSERT INTO __EFMigrationsHistory VALUES('20260824160000_Ready');"
            "CREATE TABLE Accounts(Id TEXT, Username TEXT);"
            "INSERT INTO Accounts VALUES('account','retained');"
            "CREATE TABLE ConfigItems(ConfigName TEXT, ConfigValue TEXT);"
            "INSERT INTO ConfigItems VALUES('api.key','fixture-key');"
            "CREATE TABLE DavItems(Id TEXT, Type INTEGER, Path TEXT);"
            "INSERT INTO DavItems VALUES('media',2,'/content/example.mkv');"
            "CREATE TABLE HistoryItems(Id TEXT, DownloadStatus INTEGER);"
            "INSERT INTO HistoryItems VALUES('completed',1),('failed',2);"
            "CREATE TABLE QueueItems(Id TEXT, Priority INTEGER);"
            "INSERT INTO QueueItems VALUES('11111111-1111-1111-1111-111111111111',0),('22222222-2222-2222-2222-222222222222',1);"
            "CREATE TABLE QueueNzbContents(Id TEXT, Content BLOB);"
            "INSERT INTO QueueNzbContents VALUES('content',x'0001ff');"
            "CREATE TABLE NzbBlobCleanupItems(Id TEXT);"
            "CREATE TRIGGER source_cleanup AFTER DELETE ON QueueItems BEGIN INSERT INTO NzbBlobCleanupItems VALUES(old.Id); END;"
        )


class QueueInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.sqlite"
        self.output = self.root / "input.sqlite"
        populate(self.source)
        with closing(sqlite3.connect(self.source)) as db:
            self.queue = sqlite_queue(db, identities=True)

    def prepare(self, **kwargs):
        return create_import_input(self.source, self.output, self.queue, **kwargs)

    def test_projection_has_empty_queue_and_exact_media_history_without_triggers(self):
        before = self.source.read_bytes()
        receipt = self.prepare()
        self.assertEqual(2, receipt["omitted_count"])
        self.assertEqual(before, self.source.read_bytes())
        with closing(sqlite3.connect(self.output)) as db:
            self.assertEqual(
                0, db.execute("SELECT COUNT(*) FROM QueueItems").fetchone()[0]
            )
            self.assertEqual(
                0, db.execute("SELECT COUNT(*) FROM QueueNzbContents").fetchone()[0]
            )
            self.assertEqual(
                [("completed", 1), ("failed", 2)],
                db.execute("SELECT * FROM HistoryItems").fetchall(),
            )
            self.assertEqual(
                [("media", 2, "/content/example.mkv")],
                db.execute("SELECT * FROM DavItems").fetchall(),
            )
            self.assertEqual(
                [],
                db.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall(),
            )
            self.assertIsNone(
                db.execute(
                    "SELECT name FROM sqlite_master WHERE name='NzbBlobCleanupItems'"
                ).fetchone()
            )
        with closing(sqlite3.connect(self.source)) as db:
            self.assertEqual(
                2, db.execute("SELECT COUNT(*) FROM QueueItems").fetchone()[0]
            )
            self.assertEqual(
                0, db.execute("SELECT COUNT(*) FROM NzbBlobCleanupItems").fetchone()[0]
            )
        self.assertTrue(verify_import_input(self.source, self.output, receipt))

    def test_changed_cold_queue_refuses_projection_and_preserves_source_tasks(self):
        with sqlite3.connect(self.source) as db:
            db.execute(
                "INSERT INTO QueueItems VALUES('33333333-3333-3333-3333-333333333333',0)"
            )
        with self.assertRaisesRegex(QueueInputError, "changed after"):
            self.prepare()
        self.assertFalse(self.output.exists())
        with closing(sqlite3.connect(self.source)) as db:
            self.assertEqual(
                3, db.execute("SELECT COUNT(*) FROM QueueItems").fetchone()[0]
            )

    def test_native_start_verification_refuses_derived_input_tampering(self):
        receipt = self.prepare()
        with sqlite3.connect(self.output) as db:
            db.execute("UPDATE DavItems SET Path='/content/replaced.mkv'")
        with self.assertRaisesRegex(QueueInputError, "changed before native"):
            verify_import_input(self.source, self.output, receipt)

    def test_native_start_verification_refuses_changed_original_snapshot(self):
        receipt = self.prepare()
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE HistoryItems SET DownloadStatus=2 WHERE Id='completed'")
        with self.assertRaisesRegex(QueueInputError, "original retained"):
            verify_import_input(self.source, self.output, receipt)

    def test_native_start_verification_refuses_queue_reinsertion_even_updated_digest(
        self,
    ):
        receipt = self.prepare()
        with sqlite3.connect(self.output) as db:
            db.execute("INSERT INTO QueueItems VALUES('new',0)")
        receipt["derived"]["sha256"] = hashlib.sha256(
            self.output.read_bytes()
        ).hexdigest()
        with self.assertRaisesRegex(QueueInputError, "reappeared"):
            verify_import_input(self.source, self.output, receipt)

    def test_partial_projection_cancellation_keeps_typed_cancel_and_original(self):
        before = self.source.read_bytes()
        count = 0
        cancelled = InfiniDyskV2Cancelled("fixture cancellation")

        def check():
            nonlocal count
            count += 1
            if count == 12:
                raise cancelled

        with self.assertRaises(InfiniDyskV2Cancelled) as caught:
            self.prepare(check=check)
        self.assertIs(cancelled, caught.exception)
        self.assertEqual(before, self.source.read_bytes())
        self.assertFalse(self.output.exists())

    def test_unknown_live_database_is_unavailable_and_never_zero(self):
        plan = {
            "source": {"provider": "sqlite", "data_dir": str(self.root / "missing")}
        }
        self.assertIsNone(observe_queue(plan))
        with self.assertRaises(OSError):
            inspect_queue(plan, captured=True)

    def test_live_sqlite_observation_sees_committed_wal(self):
        database = self.root / "db.sqlite"
        with sqlite3.connect(database) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE QueueItems(Id TEXT)")
            db.commit()
            db.execute("INSERT INTO QueueItems VALUES('wal-only')")
            db.commit()
            self.assertTrue(Path(str(database) + "-wal").exists())
            plan = {"source": {"provider": "sqlite", "data_dir": str(self.root)}}
            self.assertEqual(1, inspect_queue(plan)["count"])
            self.assertEqual(
                [("wal-only",)], db.execute("SELECT Id FROM QueueItems").fetchall()
            )

    def test_advisory_cancel_is_not_downgraded_to_unknown(self):
        plan = {"source": {"provider": "sqlite", "data_dir": str(self.root)}}
        cancelled = InfiniDyskV2Cancelled("fixture cancellation")
        with self.assertRaises(InfiniDyskV2Cancelled) as caught:
            observe_queue(plan, check=Mock(side_effect=cancelled))
        self.assertIs(cancelled, caught.exception)

    def test_pg_cleanup_errors_are_private_and_unavailable(self):
        connection = MagicMock()
        connection.rollback.side_effect = RuntimeError("password=PRIVATE")
        connection.close.side_effect = RuntimeError("token=PRIVATE")
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = [3]
        with (
            patch("utils.infinidysk_v2_queue.connection_settings", return_value={}),
            patch("utils.infinidysk_v2_queue._readonly"),
            patch(
                "utils.infinidysk_v2_queue._connect_default", return_value=connection
            ),
        ):
            self.assertIsNone(observe_queue({"source": {"provider": "postgres"}}))
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()

    def test_pg_identity_matches_sqlite_codec_uuid_case(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.__iter__.return_value = iter(
            [
                ("11111111-1111-1111-1111-111111111111",),
                ("22222222-2222-2222-2222-222222222222",),
            ]
        )
        with (
            patch("utils.infinidysk_v2_queue.connection_settings", return_value={}),
            patch("utils.infinidysk_v2_queue._readonly"),
        ):
            result = inspect_queue(
                {"source": {"provider": "postgres"}},
                captured=True,
                connect=Mock(return_value=connection),
            )
        for key in ("count", "ids_xor", "ids_sum"):
            self.assertEqual(self.queue[key], result[key])

    def test_optional_public_metadata_is_strict_and_contains_no_private_fields(self):
        result = public_queue_state(
            {
                "policy": "omit_from_v2",
                "observed_count": True,
                "observed_at": {"bad": "value"},
                "omitted_count": -1,
                "observation": [],
                "source_queue_preserved": True,
                "private": "never publish",
            }
        )
        self.assertIsNone(result["observed_count"])
        self.assertIsNone(result["observed_at"])
        self.assertIsNone(result["omitted_count"])
        self.assertEqual("unavailable", result["observation"])
        self.assertNotIn("private", result)
        self.assertIsNone(
            public_queue_state({"policy": [], "source_queue_preserved": True})
        )


class QueueManagerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = migration_fixture.InfiniDyskV2MigrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.manager = self.fixture.manager

    def test_known_nonempty_queue_stops_preview_before_any_source_walk(self):
        with sqlite3.connect(self.fixture.source / "db.sqlite") as db:
            db.execute("INSERT INTO QueueItems VALUES(1)")
        with (
            patch.object(
                self.manager,
                "_copy_capacity",
                side_effect=AssertionError("capacity walk"),
            ),
            patch(
                "utils.infinidysk_v2_space_control.preflight_reuse",
                side_effect=AssertionError("reuse walk"),
            ),
        ):
            preview = self.manager.preview(
                {
                    "mode": "migrate",
                    "mount_backend": "rclone",
                    "reclaim_source_space": True,
                }
            )
        self.assertFalse(preview["ready"])
        self.assertIn("1 item(s)", " ".join(preview["blockers"]))
        self.assertEqual("require_empty", preview["source_queue"]["policy"])
        self.assertEqual(1, preview["source_queue"]["observed_count"])

    def test_omission_policy_is_bound_but_live_count_is_not_stable_identity(self):
        options = {
            "mode": "migrate",
            "mount_backend": "rclone",
            "omit_queued_downloads": True,
        }
        first = self.manager._plan(options)
        with sqlite3.connect(self.fixture.source / "db.sqlite") as db:
            db.execute("INSERT INTO QueueItems VALUES(1)")
        second = self.manager._plan(options)
        self.assertEqual(first["plan_fingerprint"], second["plan_fingerprint"])
        self.assertEqual(0, first["source_queue"]["observed_count"])
        self.assertEqual(1, second["source_queue"]["observed_count"])
        self.assertNotEqual(
            first["plan_fingerprint"],
            self.manager._plan({"mode": "migrate", "mount_backend": "rclone"})[
                "plan_fingerprint"
            ],
        )

    def test_non_boolean_queue_option_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "boolean"):
            self.manager._plan({"mode": "migrate", "omit_queued_downloads": "true"})

    def test_queue_option_is_rejected_for_fresh_installation(self):
        plan = self.manager._plan({"mode": "fresh", "omit_queued_downloads": True})
        self.assertTrue(
            any("only be left out" in message for message in plan["blockers"])
        )

    def test_omission_estimate_includes_two_local_database_workspaces(self):
        plan = self.manager._plan(
            {
                "mode": "migrate",
                "mount_backend": "rclone",
                "omit_queued_downloads": True,
            }
        )
        roles = [
            role for row in plan["capacity"]["filesystems"] for role in row["roles"]
        ]
        self.assertIn("queue_import_input", roles)

    def test_late_queue_before_capture_is_not_deleted(self):
        _, request = self.fixture.preview_request()
        with sqlite3.connect(self.fixture.source / "db.sqlite") as db:
            db.execute("INSERT INTO QueueItems VALUES(1)")
        job = self.fixture.wait(self.manager.rehearse(request))
        self.assertEqual("failed", job["status"])
        self.assertTrue(job["result"]["source_untouched"])
        self.assertEqual([], self.fixture.executor.calls)
        with closing(sqlite3.connect(self.fixture.source / "db.sqlite")) as db:
            self.assertEqual(
                1, db.execute("SELECT COUNT(*) FROM QueueItems").fetchone()[0]
            )

    def test_normal_postgres_snapshot_uses_verified_staged_original_database(self):
        from utils.infinidysk_v2_queue import sqlite_queue

        fixture = self.fixture
        fixture.config.config["infinidysk"]["postgres_enabled"] = True
        fixture.executor.supported_source_providers = ("sqlite", "postgres")
        binding = {
            "physical_identity": {"system_identifier": "123", "database_oid": "456"},
            "schema_fingerprint": "a" * 64,
            "migration_history_fingerprint": "b" * 64,
            "terminal_migration": "20260824160000_Ready",
            "session_timezone": "UTC",
            "database_size_bytes": 1024,
        }
        fixture.executor.preflight = lambda plan: {
            "blockers": [],
            "source_binding": binding,
        }
        with closing(sqlite3.connect(fixture.source / "db.sqlite")) as db:
            empty = sqlite_queue(db, identities=True)

        def rehearse(plan, workspace, progress):
            root = Path(workspace["root"])
            root.mkdir(parents=True, mode=0o700)
            staged, dump = root / "v1-postgres-import.sqlite", root / "v1-postgres.dump"
            import shutil

            shutil.copyfile(fixture.source / "db.sqlite", staged)
            with sqlite3.connect(staged) as db:
                db.execute("INSERT INTO OriginalData VALUES('actual PG source')")
            dump.write_bytes(b"independently verified fixture dump")
            workspace["capture_source"](
                postgres_snapshot={
                    "staged_sqlite": str(staged),
                    "dump_path": str(dump),
                    "verified_restore": True,
                    "source_read_only": True,
                    "physical_identity": binding["physical_identity"],
                }
            )
            receipt = fixture.executor.receipt(plan)
            workspace["accept_import"](receipt)
            return {**receipt, "source_restored": True}

        fixture.executor.rehearse = rehearse
        with patch("utils.infinidysk_v2_migration.inspect_queue", return_value=empty):
            _, request = fixture.preview_request()
            job = fixture.wait(fixture.manager.rehearse(request))
        self.assertEqual("rehearsed", job["status"], job)
        private = fixture.manager._read(fixture.manager._job_path(job["job_id"]))
        database = Path(private["snapshot"]["source_database"])
        self.assertEqual("v1-postgres-import.sqlite", database.name)
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(
                2, db.execute("SELECT COUNT(*) FROM OriginalData").fetchone()[0]
            )
        with closing(sqlite3.connect(fixture.source / "db.sqlite")) as db:
            self.assertEqual(
                1, db.execute("SELECT COUNT(*) FROM OriginalData").fetchone()[0]
            )
