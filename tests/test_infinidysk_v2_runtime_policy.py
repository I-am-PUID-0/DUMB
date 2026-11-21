"""Completed migration selectors may move once; recovery ownership stays strict."""

import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from utils.infinidysk_v2_migration import (
    InfiniDyskV2MigrationError,
    InfiniDyskV2MigrationManager,
    _digest,
)
from utils.infinidysk_v2_postgres import (
    InfiniDyskV2PostgresManager,
    NativePostgresError,
    _fingerprint,
)
from utils.infinidysk_v2_runtime import RELEASE
from utils.infinidysk_v2_runtime_policy import ROLLBACK_RETIRED


class Config:
    def __init__(self, root):
        self.config = {
            "data_root": str(root),
            "infinidysk": {
                "runtime_generation": "2",
                "process_name": "InfiniDysk",
                "v2_runtime_dir": str(root / "runtime"),
                "v2_data_dir": str(root / "data"),
                "v2_release_version": RELEASE,
            },
        }
        self.saved = []

    def get(self, key):
        return self.config.get(key)

    def save_config(self, name):
        self.saved.append(name)


class MajorHarness:
    runtime_update_blocker = InfiniDyskV2MigrationManager.runtime_update_blocker
    unlock_runtime_updates = InfiniDyskV2MigrationManager.unlock_runtime_updates
    retire_runtime_rollback = InfiniDyskV2MigrationManager.retire_runtime_rollback

    def __init__(self, config):
        self.config_manager = config
        self._lock = threading.RLock()
        self._worker_id = "controller"
        self.process_handler = SimpleNamespace(startup_phase="ready")
        self.blocked = False
        service = config.get("infinidysk")
        fingerprint = _digest(self._owned_for_job(config.config, None))
        self.job = {
            "job_id": "a" * 32,
            "created_at": 1,
            "status": "completed",
            "activation_exposed": True,
            "activation_committed": True,
            "activation_fingerprint": fingerprint,
            "accepted_import": {"immutable": "receipt"},
            "result": {"restart_required": False},
            "plan": {
                "target": {
                    "runtime_dir": service["v2_runtime_dir"],
                    "data_dir": service["v2_data_dir"],
                }
            },
            "consumer_recovery": {
                "schema": 1,
                "ready": True,
                "pending": [],
                "failed": [],
                "target_binding": {"configuration": fingerprint},
                "scope_bindings": [
                    {
                        "name": "InfiniDysk",
                        "sha256": _digest({"key": "infinidysk", "service": service}),
                    }
                ],
            },
        }

    def _config(self):
        return copy.deepcopy(self.config_manager.config)

    def _jobs(self):
        return [copy.deepcopy(self.job)] if self.job else []

    @staticmethod
    def _owned_for_job(configuration, job):
        return configuration["infinidysk"]

    def _job_path(self, identifier):
        return identifier

    def _write(self, path, job):
        self.job = copy.deepcopy(job)

    def has_blocking_job(self):
        return self.blocked


class RuntimePolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = Config(self.root)
        self.major = MajorHarness(self.config)
        self.pg = InfiniDyskV2PostgresManager(
            config_manager=self.config, state_root=self.root / "pg"
        )
        self.pg._boot = Mock(return_value={})
        self.patch = patch(
            "utils.infinidysk_v2_runtime_policy._pg", return_value=self.pg
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.installed = patch(
            "utils.infinidysk_v2_runtime_policy.installed_v2_version",
            return_value=RELEASE,
        )
        self.installed.start()
        self.addCleanup(self.installed.stop)
        backup = patch(
            "utils.installation_backup.installation_backup_blocker", return_value=None
        )
        backup.start()
        self.addCleanup(backup.stop)

    def pg_job(self, **changes):
        job = {
            "job_id": "b" * 32,
            "status": "completed",
            "can_rollback": True,
            "service_fingerprint": _fingerprint(self.config.get("infinidysk")),
        }
        job.update(changes)
        self.pg._save(job)
        return job

    def receipt(self):
        return {
            "format": "native-runtime-update-v1",
            "id": "c" * 32,
            "runtime": self.config.get("infinidysk")["v2_runtime_dir"],
            "launched": False,
            "state": "active",
            "stage": "prepare_launch",
            "release_metadata": {"release": "v2.0.0-beta.7"},
        }

    def test_unlock_once_rebinds_only_live_selector_and_retains_original_evidence(self):
        before = copy.deepcopy(self.major.job)
        self.pg_job()
        self.assertTrue(self.major.unlock_runtime_updates())
        self.assertEqual(self.config.get("infinidysk")["v2_release_version"], "latest")
        self.assertEqual(self.major.job["plan"], before["plan"])
        self.assertEqual(self.major.job["accepted_import"], before["accepted_import"])
        self.assertNotIn("runtime_rollback_retired", self.major.job)
        self.assertTrue(self.pg._job()["can_rollback"])
        self.assertEqual(
            self.pg._job()["service_fingerprint"],
            _fingerprint(self.config.get("infinidysk")),
        )
        self.assertEqual(
            self.major.job["activation_fingerprint"],
            _digest(self.config.get("infinidysk")),
        )
        self.config.get("infinidysk")["v2_release_version"] = RELEASE
        self.assertFalse(self.major.unlock_runtime_updates())
        self.assertEqual(self.config.get("infinidysk")["v2_release_version"], RELEASE)

    def test_failed_configuration_save_leaves_resumable_exact_intent(self):
        with patch.object(
            self.config, "save_config", side_effect=OSError("fixture save failure")
        ):
            with self.assertRaises(OSError):
                self.major.unlock_runtime_updates()
        self.assertEqual(self.config.get("infinidysk")["v2_release_version"], RELEASE)
        self.assertEqual(self.major.job["runtime_updates_unlock"]["state"], "pending")
        self.assertIsNotNone(self.major.runtime_update_blocker())
        self.assertTrue(self.major.unlock_runtime_updates())
        self.assertIsNone(self.major.runtime_update_blocker())

    def test_failed_provider_rebind_resumes_after_configuration_was_saved(self):
        self.pg_job()
        with patch.object(
            self.pg,
            "rebind_runtime_selector",
            side_effect=OSError("fixture provider journal failure"),
        ):
            with self.assertRaises(OSError):
                self.major.unlock_runtime_updates()
        self.assertEqual(self.config.get("infinidysk")["v2_release_version"], "latest")
        self.assertEqual(self.major.job["runtime_updates_unlock"]["state"], "pending")
        self.assertTrue(self.major.unlock_runtime_updates())
        self.assertEqual(
            self.pg._job()["service_fingerprint"],
            _fingerprint(self.config.get("infinidysk")),
        )

    def test_unrelated_change_cannot_reuse_pending_selector_intent(self):
        with patch.object(
            self.config, "save_config", side_effect=OSError("fixture save failure")
        ):
            with self.assertRaises(OSError):
                self.major.unlock_runtime_updates()
        self.config.get("infinidysk")["mount_path"] = "/example/changed"
        with self.assertRaises(InfiniDyskV2MigrationError):
            self.major.unlock_runtime_updates()
        self.assertEqual(self.config.get("infinidysk")["v2_release_version"], RELEASE)

    def test_unsettled_consumers_restart_cleanup_and_uncommitted_jobs_do_not_unlock(
        self,
    ):
        for field in ("consumers", "restart", "cleanup", "committed"):
            with self.subTest(field=field):
                self.major = MajorHarness(self.config)
                if field == "consumers":
                    self.major.job["consumer_recovery"]["pending"] = ["Example Arr"]
                elif field == "restart":
                    self.major.job["result"]["restart_required"] = True
                    self.major.job["worker_id"] = self.major._worker_id
                elif field == "cleanup":
                    self.major.job["staging_cleanup_required"] = True
                else:
                    self.major.job["activation_committed"] = False
                self.assertFalse(self.major.unlock_runtime_updates())
                self.assertIsNotNone(self.major.runtime_update_blocker())
                self.assertEqual(
                    self.config.get("infinidysk")["v2_release_version"], RELEASE
                )

    def test_database_operation_always_prevents_unlock(self):
        self.pg_job(status="copying")
        self.assertFalse(self.major.unlock_runtime_updates())
        self.assertIsNotNone(self.major.runtime_update_blocker())

    def test_active_installation_backup_keeps_selector_and_private_job_unchanged(self):
        configuration = copy.deepcopy(self.config.config)
        job = copy.deepcopy(self.major.job)
        with patch(
            "utils.installation_backup.installation_backup_blocker",
            return_value="Installation backup owns configuration persistence.",
        ):
            self.assertFalse(self.major.unlock_runtime_updates())
        self.assertEqual(self.config.config, configuration)
        self.assertEqual(self.major.job, job)
        self.assertEqual(self.config.saved, [])
        self.assertTrue(self.major.unlock_runtime_updates())
        self.assertEqual(self.config.get("infinidysk")["v2_release_version"], "latest")

    def test_fresh_native_install_needs_no_old_major_job(self):
        self.major.job = None
        self.config.get("infinidysk")["v2_release_version"] = "latest"
        self.assertFalse(self.major.unlock_runtime_updates())
        self.assertIsNone(self.major.runtime_update_blocker())

    def test_actual_update_retires_both_rollback_actions_without_removing_backups(self):
        self.pg_job()
        backup = self.root / "retained-backup"
        backup.write_bytes(b"retained original")
        self.assertTrue(self.major.retire_runtime_rollback(self.receipt()))
        self.assertTrue(self.pg.retire_runtime_rollback(self.receipt()))
        self.assertIn("runtime_rollback_retired", self.major.job)
        self.assertFalse(self.pg._public(self.pg._job())["can_rollback"])
        self.assertEqual(
            self.pg._public(self.pg._job())["rollback_unavailable_reason"],
            ROLLBACK_RETIRED,
        )
        with self.assertRaisesRegex(NativePostgresError, "later native runtime"):
            self.pg.rollback({})
        self.assertEqual(backup.read_bytes(), b"retained original")

    def test_provider_rebind_refuses_other_configuration_changes(self):
        self.pg_job()
        before = copy.deepcopy(self.config.get("infinidysk"))
        after = {
            **before,
            "v2_release_version": "latest",
            "mount_path": "/example/changed",
        }
        with self.assertRaises(NativePostgresError):
            self.pg.rebind_runtime_selector(before, after)
        self.assertEqual(self.pg._job()["service_fingerprint"], _fingerprint(before))

    def test_busy_native_provider_lock_defers_without_waiting(self):
        entered, release = threading.Event(), threading.Event()

        def hold():
            with self.pg._lock:
                entered.set()
                release.wait(timeout=5)

        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(entered.wait(timeout=2))
        try:
            self.assertIsNotNone(self.pg.runtime_update_blocker())
            self.assertFalse(self.major.unlock_runtime_updates())
        finally:
            release.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    def test_activation_from_sorted_private_plan_preserves_visible_service_field_order(
        self,
    ):
        original = copy.deepcopy(self.config.config)
        before = list(original["infinidysk"])
        target = copy.deepcopy(original["infinidysk"])
        target.update(
            runtime_generation="2",
            mount_backend="rclone",
            mount_path="/example/mount",
            enabled=True,
        )
        target = json.loads(json.dumps(target, sort_keys=True))
        job = copy.deepcopy(self.major.job)
        job["plan"].update(
            config_snapshot=original,
            source={"process_name": "InfiniDysk"},
            target={"service": target, "mount_backend": "rclone"},
        )
        self.assertNotEqual(list(target), before)
        InfiniDyskV2MigrationManager._activate_config(self.major, job)
        actual = self.config.get("infinidysk")
        self.assertEqual(actual, target)
        self.assertEqual(list(actual)[: len(before)], before)
        self.assertEqual(actual["runtime_generation"], "2")
        self.assertEqual(actual["mount_path"], "/example/mount")


if __name__ == "__main__":
    unittest.main()
