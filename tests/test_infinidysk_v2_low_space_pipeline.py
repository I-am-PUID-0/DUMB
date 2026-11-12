"""Low-space migration runs real manager/executor/archive recovery callbacks."""

import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from utils.backup_storage import BackupStorage
from utils.infinidysk_v2_execution import InfiniDyskV2Executor
from utils.infinidysk_v2_inventory import tree_fingerprint


class LowSpacePipelineTests(unittest.TestCase):
    def setUp(self):
        # Compose the established supported-schema fixture without inheriting
        # its tests or replacing the manager/executor orchestration under test.
        from tests import test_infinidysk_v2_migration as fixtures

        self.fixture = fixtures.InfiniDyskV2MigrationTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.source = self.fixture.source
        self.config = self.fixture.config
        self.config.config.update(puid=os.geteuid() or 1000, pgid=os.getegid() or 1000)
        self.config.config["infinidysk"]["enabled"] = True
        self.config.config["backup_storage"] = {
            "profiles": {},
            "default_destination_id": "",
        }
        self.file_id = "11111111-1111-4111-8111-111111111111"
        self.history_id = "22222222-2222-4222-8222-222222222222"
        with sqlite3.connect(self.source / "db.sqlite") as database:
            database.executescript(
                "CREATE TABLE DavItems(Id TEXT,Name TEXT,Type INTEGER,Path TEXT);"
                "CREATE TABLE HistoryItems(Id TEXT);"
            )
            database.execute(
                "INSERT INTO DavItems VALUES(?,?,2,?)",
                (self.file_id, "movie.mkv", "/content/example/movie.mkv"),
            )
            database.execute("INSERT INTO HistoryItems VALUES(?)", (self.history_id,))
        self.archive = self.fixture.root / "mounted-archives"
        self.archive.mkdir(mode=0o700)
        self.storage = BackupStorage(
            self.config, state_root=self.fixture.root / "archive-control"
        )
        self.storage.save_profile(
            {
                "id": "nas",
                "name": "Migration archive",
                "path": str(self.archive),
                "enabled": True,
            }
        )
        self.handler = self.Handler()
        self.handler.start_process("InfiniDysk")
        self.handler.start_process("rclone InfiniDysk")
        self.fixture.handler = self.handler
        self.executor = InfiniDyskV2Executor(self.handler, self.config)
        self.executor.gate = None  # Existing fixture cleanup only joins owned workers.
        self.fixture.executor = self.executor
        self.fixture.manager = self.fixture.new_manager()
        self.fixture.managers.append(self.fixture.manager)
        self.manager = self.fixture.manager
        self.bad_report = False
        self.native_candidates = []
        self.patches = []
        for target, value in (
            ("utils.backup_storage.get_backup_storage", self.storage),
            ("utils.infinidysk_v2_space_control.get_backup_storage", self.storage),
            ("utils.infinidysk_v2_space_control.preflight_reuse", None),
            ("utils.infinidysk_v2_execution.install_v2_runtime", (True, None)),
            ("utils.infinidysk_v2_execution.native_auth", {}),
            ("utils.infinidysk_v2_execution.configure_v2_runtime", (True, None)),
            ("utils.infinidysk_v2_execution.native_api_request", {"status": 200}),
            ("utils.infinidysk_v2_execution.delivery_mount_ready", True),
            ("utils.infinidysk_v2_execution.mount_records", []),
        ):
            self.start_patch(patch(target, return_value=value))
        self.start_patch(
            patch.object(self.executor, "preflight", return_value={"blockers": []})
        )
        self.start_patch(
            patch(
                "utils.infinidysk_v2_execution.NativeCandidate", side_effect=self.native
            )
        )
        self.config.save_config()

    def start_patch(self, item):
        result = item.start()
        self.patches.append(item)
        self.addCleanup(item.stop)
        return result

    class Handler:
        def __init__(self):
            self.process_names, self.processes = {}, {}
            self.shutting_down = False
            self.calls = []
            self.next_pid = 500000

        def _prefixed_name(self, name):
            return name

        def start_process(self, name):
            self.next_pid += 1
            process = SimpleNamespace(pid=self.next_pid, poll=lambda: None)
            self.process_names[name] = process
            self.processes[name] = {"name": name, "process_obj": process}
            self.calls.append(("start", name))
            return True, None

        def stop_process(self, name):
            self.process_names.pop(name, None)
            self.processes.pop(name, None)
            self.calls.append(("stop", name))

    def native(
        self, service, candidate, private, uid, gid, timezone, legacy_env, **options
    ):
        owner = self

        class Native:
            stopped = False

            def __enter__(self):
                self.path = Path(candidate)
                owner.native_candidates.append(self.path)
                return self

            def wait_import(self, migrate, progress):
                with sqlite3.connect(self.path / "infinidysk.sqlite") as database:
                    database.executescript(
                        "CREATE TABLE items(id BLOB,name TEXT,kind INTEGER);"
                        "CREATE TABLE history(id BLOB);"
                    )
                    if not owner.bad_report:
                        database.execute(
                            "INSERT INTO items VALUES(?,?,2)",
                            (uuid.UUID(owner.file_id).bytes, "movie.mkv"),
                        )
                    database.execute(
                        "INSERT INTO history VALUES(?)",
                        (uuid.UUID(owner.history_id).bytes,),
                    )
                if owner.bad_report:
                    return (
                        "Imported 0 directories, 0 files, 0 NZBs, 1 history rows, 0 queued jobs\n"
                        "  fetched 0 articles to learn segment sizes\n"
                        "Files not carried over: 1\n"
                        "  1: segment sizes unknown: connection refused\n"
                        "    /content/example/movie.mkv\n"
                    )
                return "Imported 0 directories, 1 files, 0 NZBs, 1 history rows, 0 queued jobs"

            def __exit__(self, *unused):
                self.stopped = True

        return Native()

    def preview(self):
        preview = self.manager.preview(
            {
                "mode": "migrate",
                "mount_backend": "rclone",
                "import_strategy": "preserve",
                "backup_destination_id": "nas",
                "reclaim_source_space": True,
            }
        )
        self.assertTrue(preview["ready"], preview["blockers"])
        self.assertIn("archive_recovery", preview["required_acknowledgements"])
        return preview, {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": dict.fromkeys(
                preview["required_acknowledgements"], True
            ),
        }

    def wait(self, response):
        self.manager._thread.join(timeout=10)
        self.assertFalse(
            self.manager._thread.is_alive(), "owned migration worker did not finish"
        )
        return self.manager.get_job(response["job"]["job_id"])["job"]

    def test_rehearsal_rebinds_preview_then_cutover_and_rollback_restore_real_archive(
        self,
    ):
        original = tree_fingerprint(self.source, scratch_root=self.fixture.root)
        preview, request = self.preview()
        rehearsal = self.wait(self.manager.rehearse(request))
        self.assertEqual("rehearsed", rehearsal["status"], rehearsal)
        self.assertEqual(
            original, tree_fingerprint(self.source, scratch_root=self.fixture.root)
        )
        self.assertFalse(self.native_candidates[-1].exists())
        completed = self.wait(
            self.manager.start({**request, "rehearsal_job_id": rehearsal["job_id"]})
        )
        self.assertEqual("completed", completed["status"], completed)
        self.assertFalse(self.source.exists())
        self.assertEqual("2", self.config.config["infinidysk"]["runtime_generation"])
        self.assertTrue(
            Path(self.config.config["infinidysk"]["v2_data_dir"])
            .joinpath("infinidysk.sqlite")
            .exists()
        )
        private = self.manager._read(self.manager._job_path(completed["job_id"]))
        self.assertTrue(
            private["snapshot"]["space_recovery"]["native_mutations_authorized"]
        )
        self.assertTrue(private["accepted_import"]["validated"])
        result = self.manager.rollback(
            {
                "job_id": completed["job_id"],
                "confirmation": completed["rollback_confirmation_text"],
            }
        )["job"]
        self.assertEqual("rolled_back", result["status"], result)
        self.assertEqual("1", self.config.config["infinidysk"]["runtime_generation"])
        self.assertEqual(
            original, tree_fingerprint(self.source, scratch_root=self.fixture.root)
        )
        self.assertTrue(list(self.archive.rglob("*.tar")))

    def test_provider_required_skip_keeps_diagnostics_and_restores_v1_archive(self):
        self.bad_report = True
        original = tree_fingerprint(self.source, scratch_root=self.fixture.root)
        preview, request = self.preview()
        result = self.wait(self.manager.rehearse(request))
        self.assertEqual("failed_restored", result["status"], result)
        self.assertEqual(
            original, tree_fingerprint(self.source, scratch_root=self.fixture.root)
        )
        self.assertEqual(1, result["import_report"]["skipped_files"])
        self.assertTrue(result["import_report"]["blocking_reasons"])
        raw = self.manager.get_report(result["job_id"])
        self.assertIn("segment sizes unknown", str(raw))
        self.assertFalse(self.native_candidates[-1].exists())

    def test_failure_after_target_promotion_restores_original_data_and_configuration(
        self,
    ):
        original = tree_fingerprint(self.source, scratch_root=self.fixture.root)
        preview, request = self.preview()
        rehearsal = self.wait(self.manager.rehearse(request))
        self.assertEqual("rehearsed", rehearsal["status"], rehearsal)
        with patch(
            "utils.infinidysk_v2_execution.configure_v2_runtime",
            return_value=(False, "fixture failure"),
        ):
            result = self.wait(
                self.manager.start({**request, "rehearsal_job_id": rehearsal["job_id"]})
            )
        self.assertEqual("failed_restored", result["status"], result)
        self.assertEqual(
            original, tree_fingerprint(self.source, scratch_root=self.fixture.root)
        )
        self.assertEqual("1", self.config.config["infinidysk"]["runtime_generation"])
        self.assertFalse(Path(self.config.config["infinidysk"]["v2_data_dir"]).exists())


if __name__ == "__main__":
    unittest.main()
