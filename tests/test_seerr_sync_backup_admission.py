"""Real bidirectional admission between Seerr's daemon and cold installation work."""

import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests import test_installation_backup as fixtures
from utils import installation_backup as backup
from utils import seerr_sync as sync
from utils.infinidysk_migration_admission import infinidysk_external_mutation_active


class SeerrBackupAdmissionTests(unittest.TestCase):
    join_worker = fixtures.ManagerTests.join_worker

    def setUp(self):
        fixtures.ManagerTests.setUp(self)
        self.state_file = Path(self.config.file_path).parent / "seerr_sync_state.json"
        self.state_file.write_text(
            json.dumps(
                {"last_poll_ts": "reserved original poll", "requests": {}, "failed": {}}
            )
        )
        self.config.config["seerr_sync"] = {
            "enabled": True,
            "external_primary": {
                "enabled": True,
                "url": "http://primary.example.invalid",
                "api_key": "reserved-test-key",
            },
            "external_subordinates": [
                {
                    "url": "http://subordinate.example.invalid",
                    "api_key": "reserved-test-key",
                }
            ],
        }
        self.config.config["seerr"] = {"instances": {}}
        self.config.save_config()
        self.get_config = Mock(
            side_effect=lambda key, default=None: self.config.config.get(key, default)
        )
        self.stack.enter_context(
            patch.object(sync, "CONFIG_MANAGER", SimpleNamespace(get=self.get_config))
        )
        self.stack.enter_context(
            patch.object(sync, "_SYNC_STATE_FILE", str(self.state_file))
        )
        self.stack.enter_context(patch.object(backup, "_BOOT_HOLD", None))
        self.wait_for_seerr = self.stack.enter_context(
            patch.object(sync, "_wait_for_seerr", return_value=True)
        )
        self.poll = self.stack.enter_context(
            patch.object(sync, "_poll_requests", return_value=[])
        )
        self.assertFalse(infinidysk_external_mutation_active())

    def request(self):
        preview = self.manager.preview({"destination_id": "fixture"})
        self.assertTrue(preview["ready"], preview["blockers"])
        return {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": {
                "downtime": True,
                "secrets": True,
                "external_state": True,
            },
        }

    def state_identity(self):
        stamp = self.state_file.stat()
        return (
            self.state_file.read_bytes(),
            stamp.st_ino,
            stamp.st_mtime_ns,
            stamp.st_ctime_ns,
        )

    def test_admitted_backup_defers_all_sync_io_then_next_cycle_writes(self):
        entered, release = threading.Event(), threading.Event()
        original = backup.write_archive
        before = self.state_identity()
        self.addCleanup(release.set)

        def held_archive(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise AssertionError("Fixture backup did not release")
            return original(*args, **kwargs)

        with patch.object(backup, "write_archive", held_archive):
            self.manager.start(self.request())
            try:
                self.assertTrue(entered.wait(5))
                self.assertTrue(self.manager.has_blocking_job())
                with patch.object(
                    sync, "_load_sync_state", wraps=sync._load_sync_state
                ) as load:
                    sync.run_sync_cycle()
                    load.assert_not_called()
                self.get_config.assert_not_called()
                self.wait_for_seerr.assert_not_called()
                self.poll.assert_not_called()
                self.assertEqual(self.state_identity(), before)
            finally:
                release.set()
                self.join_worker()
        self.assertEqual(self.manager.status()["job"]["status"], "completed")
        self.assertFalse(self.manager.has_blocking_job())
        sync.run_sync_cycle()
        self.assertNotEqual(self.state_file.read_bytes(), before[0])
        self.assertNotEqual(
            json.loads(self.state_file.read_text())["last_poll_ts"],
            "reserved original poll",
        )
        self.assertFalse(infinidysk_external_mutation_active())

    def assert_inflight_cycle_blocks_backup(self, boundary):
        request = self.request()
        entered, release = threading.Event(), threading.Event()
        errors = []
        original = getattr(sync, boundary)
        self.addCleanup(release.set)

        def paused(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise AssertionError("Fixture sync did not release")
            return original(*args, **kwargs)

        def run():
            try:
                sync.run_sync_cycle()
            except BaseException as error:
                errors.append(error)

        with patch.object(sync, boundary, paused):
            worker = threading.Thread(target=run, name="fixture-seerr-cycle")
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertTrue(infinidysk_external_mutation_active())
                with self.assertRaisesRegex(
                    backup.InstallationBackupError, "managed mutation"
                ):
                    self.manager.start(request)
                self.assertEqual(self.handler.events, [])
                self.assertFalse(self.manager.has_blocking_job())
            finally:
                release.set()
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(infinidysk_external_mutation_active())
        self.assertNotEqual(
            json.loads(self.state_file.read_text())["last_poll_ts"],
            "reserved original poll",
        )
        self.manager.start(request)
        self.join_worker()
        self.assertEqual(self.manager.status()["job"]["status"], "completed")
        self.assertEqual(self.handler.events, [("stop", "App"), ("start", "App")])

    def test_inflight_network_cycle_drains_before_real_backup_admission(self):
        self.assert_inflight_cycle_blocks_backup("_poll_requests")

    def test_inflight_state_checkpoint_drains_before_real_backup_admission(self):
        self.assert_inflight_cycle_blocks_backup("_save_sync_state")

    def test_cycle_exception_releases_reservation_and_next_cycle_writes(self):
        before = self.state_identity()
        self.poll.side_effect = RuntimeError("reserved fixture request failure")
        with self.assertRaisesRegex(RuntimeError, "reserved fixture request failure"):
            sync.run_sync_cycle()
        self.assertFalse(infinidysk_external_mutation_active())
        self.assertEqual(self.state_identity(), before)
        self.poll.side_effect = None
        sync.run_sync_cycle()
        self.assertFalse(infinidysk_external_mutation_active())
        self.assertNotEqual(self.state_file.read_bytes(), before[0])

    def test_recovery_hold_preserves_state_without_loading_or_network_work(self):
        before = self.state_identity()
        with patch.object(backup, "_BOOT_HOLD", "Reserved installation recovery hold"):
            with patch.object(
                sync, "_load_sync_state", wraps=sync._load_sync_state
            ) as load:
                sync.run_sync_cycle()
                load.assert_not_called()
        self.get_config.assert_not_called()
        self.wait_for_seerr.assert_not_called()
        self.poll.assert_not_called()
        self.assertEqual(self.state_identity(), before)
        self.assertFalse(infinidysk_external_mutation_active())


if __name__ == "__main__":
    unittest.main()
