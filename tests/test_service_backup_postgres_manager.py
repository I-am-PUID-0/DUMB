"""Opt-in real-PG plus real-archive selected-service coordinator qualification."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from tests import test_service_backup as manager_fixture
from tests import test_service_backup_postgres as pg_fixture
from utils import installation_backup as backup_module
from utils.installation_backup_archive import InstallationArchiveError
from utils.service_backup_postgres import PostgresServiceBackup, service_databases


@unittest.skipUnless(
    os.environ.get("DUMB_SERVICE_BACKUP_PG_TESTS") == "1",
    "opt-in disposable PostgreSQL coordinator round trip",
)
class ServicePostgresCoordinatorTests(pg_fixture.ServiceDatabaseRoundTripTests):
    # Reuse its isolated server lifecycle without re-running adapter cases here.
    test_active_database_client_is_not_terminated = None
    test_apply_then_rollback_restores_original_current_data = None
    test_changed_scratch_comment_blocks_cleanup_without_deletion = None
    test_lost_apply_acknowledgement_reconciles_actual_oids = None
    test_restore_sql_cannot_create_global_role = None
    test_round_trip_finish_and_repeat_finish_preserve_unrelated = None
    test_foreign_object_owner_rejected_without_changing_owner_or_rows = None
    test_default_type_and_large_object_acl_rejected_before_dump = None

    backup = manager_fixture.ServiceBackupTests.backup
    restore_preview = manager_fixture.ServiceBackupTests.restore_preview
    start_restore = manager_fixture.ServiceBackupTests.start_restore
    change_selected = manager_fixture.ServiceBackupTests.change_selected
    assert_unselected = manager_fixture.ServiceBackupTests.assert_unselected
    query = manager_fixture.ServiceBackupTests.query

    def setUp(self):
        pg_fixture.ServiceDatabaseRoundTripTests.setUp(self)
        managed_postgres = deepcopy(type(self).config["postgres"])
        specs = deepcopy(self.specs)
        manager_fixture.ServiceBackupTests.setUp(self)
        self.config.config["postgres"] = managed_postgres
        self.config.save_config()

        def databases(configuration, key, instance_name=None):
            if key == "fixture":
                return deepcopy(specs)
            return service_databases(configuration, key, instance_name)

        self.stack.enter_context(
            patch("utils.service_backup.service_databases", side_effect=databases)
        )

    def join_worker(self):
        if self.manager._worker:
            self.manager._worker.join(timeout=120)
            self.assertFalse(self.manager._worker.is_alive())

    def test_manager_round_trip_preserves_shared_pg_and_unselected_state(self):
        self._sql("unrelated", "UPDATE example SET value='unselected-current'")
        backup = self.backup()
        current, auth = self.change_selected()
        self._sql("selected_main", "UPDATE example SET value='current-after-backup'")
        self._sql("selected_log", "INSERT INTO example(value) VALUES ('current')")
        preview = self.restore_preview(backup)
        result = self.start_restore(preview)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(
            self._sql("selected_main", "SELECT value FROM example", fetch=True),
            [("saved",)],
        )
        self.assertEqual(
            self._sql("selected_log", "SELECT count(*) FROM example", fetch=True),
            [(1,)],
        )
        self.assertEqual(
            self._sql("unrelated", "SELECT value FROM example", fetch=True),
            [("unselected-current",)],
        )
        self.assertEqual(self.query(), "before")
        self.assert_unselected(current, auth)
        self.assertFalse(self.manager.has_blocking_job())

    def test_committed_cleanup_failure_requires_finish_without_old_database_replay(
        self,
    ):
        backup = self.backup()
        current, auth = self.change_selected()
        self._sql("selected_main", "UPDATE example SET value='current-after-backup'")
        preview = self.restore_preview(backup)
        with patch.object(
            PostgresServiceBackup,
            "finish_restore",
            side_effect=InstallationArchiveError(
                "fixture committed cleanup interruption"
            ),
        ):
            result = self.start_restore(preview)
        self.assertEqual(result["status"], "recovery_required", result)
        retained = self.manager._job(result["job_id"])
        self.assertTrue(retained["service_restore_committed"])
        self.assertIn("FINISH SERVICE RESTORE", result["rollback_confirmation_text"])
        self.assert_unselected(current, auth)
        self.manager.rollback(
            result["job_id"], {"confirmation": result["rollback_confirmation_text"]}
        )
        self.join_worker()
        finished = self.manager.get_job(result["job_id"])["job"]
        self.assertEqual(finished["status"], "completed", finished)
        self.assertEqual(
            self._sql("selected_main", "SELECT value FROM example", fetch=True),
            [("saved",)],
        )
        self.assert_unselected(current, auth)
        self.assertFalse(self.manager.has_blocking_job())

    def test_new_controller_finishes_pg_recovery_then_requires_normal_restart(self):
        backup = self.backup()
        current, auth = self.change_selected()
        preview = self.restore_preview(backup)
        with patch.object(
            PostgresServiceBackup,
            "finish_restore",
            side_effect=InstallationArchiveError("fixture interrupted after commit"),
        ):
            interrupted = self.start_restore(preview)
        self.assertEqual(interrupted["status"], "recovery_required", interrupted)
        subprocess.run(
            [
                str(self.tools / "pg_ctl"),
                "-D",
                str(self.data),
                "-m",
                "fast",
                "-w",
                "stop",
            ],
            check=True,
            timeout=30,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        handler = manager_fixture.fixtures.Handler()
        handler.process_names = {}

        def prepared_start(name):
            handler.events.append(("prepared", name))
            if name != "PostgreSQL":
                raise AssertionError(
                    "cross-boot recovery must not start application services"
                )
            subprocess.run(
                [
                    str(self.tools / "pg_ctl"),
                    "-D",
                    str(self.data),
                    "-l",
                    str(type(self).root / "server.log"),
                    "-o",
                    f"-h 127.0.0.1 -p {self.port} -k {self.socket}",
                    "-w",
                    "start",
                ],
                check=True,
                timeout=30,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            handler.process_names[name] = manager_fixture.fixtures.Process()
            return True, None

        handler.start_prepared_process = prepared_start
        with patch.object(
            backup_module,
            "_boot_identity",
            return_value=["different-controller", "reserved-process", "reserved-boot"],
        ):
            recovery = backup_module.InstallationBackupManager(
                self.config, handler, storage=self.storage
            )
        recovery.workspace_root = self.manager.workspace_root
        recovery.boot_error = (
            "fixture API/frontend-only recovery mode; restart required"
        )
        self.manager = recovery
        self.manager.initialize_controller()
        retained = self.manager.get_job(interrupted["job_id"])["job"]
        finished = self.manager.rollback(
            interrupted["job_id"],
            {"confirmation": retained["rollback_confirmation_text"]},
        )["job"]
        self.assertEqual(finished["status"], "completed", finished)
        self.assertTrue(finished["restart_required"], finished)
        self.assertTrue(finished["result"]["restart_required"])
        self.assertEqual(handler.events, [("prepared", "PostgreSQL")])
        self.assertEqual(
            self._sql("selected_main", "SELECT value FROM example", fetch=True),
            [("saved",)],
        )
        self.assertEqual(
            json.loads(Path(self.config.file_path).read_text()), self.config.config
        )
        self.assertEqual(self.auth_path.read_bytes(), auth)
        for key in ("postgres", "other", "dumb", "tz"):
            self.assertEqual(self.config.config[key], current[key])


if __name__ == "__main__":
    unittest.main()
