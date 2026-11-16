"""Reconnectable migration previews never start migration or duplicate scans."""

from copy import deepcopy
import threading
import unittest
from unittest.mock import patch

from tests import test_infinidysk_v2_migration as fixtures
from utils import infinidysk_v2_migration as migration
from utils.infinidysk_v2_inventory import InventoryError
from utils.infinidysk_v2_preview import PreviewTasks


class PreviewTaskTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.InfiniDyskV2MigrationTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.manager = self.fixture.manager
        self.release = threading.Event()
        self.entered = threading.Event()
        self.original = self.manager._plan
        self.options = {
            "mode": "migrate",
            "mount_backend": "rclone",
            "request_id": "a" * 32,
        }
        self.addCleanup(self.stop_worker)

    def stop_worker(self):
        self.release.set()
        controller = self.manager._preview_tasks
        if controller and controller.thread:
            controller.thread.join(timeout=5)
            self.assertFalse(controller.thread.is_alive())

    def delayed_plan(self, options, *, progress=None):
        self.entered.set()
        if not self.release.wait(timeout=4):
            raise RuntimeError("fixture was not released")
        if progress:
            progress("checking_capacity", "Measuring source files.")
        return self.original(options, progress=progress)

    def start_blocked(self):
        replacement = patch.object(self.manager, "_plan", side_effect=self.delayed_plan)
        replacement.start()
        self.addCleanup(replacement.stop)
        result = self.manager.preview_start(self.options)
        self.assertTrue(self.entered.wait(timeout=2))
        return result["task"]

    def finish(self):
        self.release.set()
        self.manager._preview_tasks.thread.join(timeout=5)
        self.assertFalse(self.manager._preview_tasks.thread.is_alive())
        return self.manager.preview_task()["task"]

    def test_start_returns_before_scan_finishes_and_status_has_no_partial_token(self):
        original_config = deepcopy(self.fixture.config.config)
        task = self.start_blocked()
        status = self.manager.preview_task({"task_id": task["task_id"]})
        self.assertEqual(status["protocol"], 1)
        self.assertIn(status["task"]["status"], {"queued", "running"})
        self.assertIsNone(status["task"]["preview"])
        self.assertFalse(status["task"]["preview_available"])
        self.assertEqual(
            task["options"], {"mode": "migrate", "mount_backend": "rclone"}
        )
        self.assertEqual(len(task["options_fingerprint"]), 64)
        self.assertEqual(self.fixture.executor.calls, [])
        self.assertEqual(self.fixture.config.config, original_config)
        self.assertFalse((self.manager.state_root / "previews").exists())
        self.assertEqual(self.finish()["status"], "completed")

    def test_active_equal_options_and_lost_completed_response_are_deduplicated(self):
        first = self.start_blocked()
        self.assertEqual(
            self.manager.preview_start(self.options)["task"]["task_id"],
            first["task_id"],
        )
        self.assertEqual(
            self.manager.preview_start({**self.options, "request_id": "b" * 32})[
                "task"
            ]["task_id"],
            first["task_id"],
        )
        self.finish()
        again = self.manager.preview_start(self.options)["task"]
        self.assertEqual(again["task_id"], first["task_id"])
        self.assertTrue(again["preview_available"])
        self.assertEqual(self.manager._plan.call_count, 1)

    def _assert_alias_retry_survives_completion(self, primary):
        if primary is None:
            self.options.pop("request_id")
        else:
            self.options["request_id"] = primary
        first = self.start_blocked()
        retry = {**self.options, "request_id": "b" * 32}
        acknowledged = self.manager.preview_start(retry)["task"]
        self.assertEqual(acknowledged["task_id"], first["task_id"])
        self.assertEqual(acknowledged["request_id"], primary)
        self.assertNotIn("request_aliases", acknowledged)
        self.assertIn("b" * 32, self.manager._preview_tasks._read()["request_aliases"])
        # The positive acknowledgement is lost. The worker completes and
        # writes progress/result using its original in-memory task object.
        self.finish()
        repeated = self.manager.preview_start(retry)["task"]
        self.assertEqual(repeated["task_id"], first["task_id"])
        self.assertEqual(repeated["status"], "completed")
        self.assertEqual(repeated["request_id"], primary)
        self.assertTrue(repeated["preview_available"])
        self.assertNotIn("request_aliases", repeated)
        # The private retry binding also survives controller reconstruction.
        reloaded = PreviewTasks(self.manager).start(retry)["task"]
        self.assertEqual(reloaded["task_id"], first["task_id"])
        with self.assertRaisesRegex(
            migration.InfiniDyskV2MigrationError, "different selections"
        ):
            self.manager.preview_start({**retry, "mount_backend": "fuse"})
        self.assertEqual(self.manager._plan.call_count, 1)
        self.assertEqual(self.fixture.executor.calls, [])

    def test_lost_deduped_acknowledgement_retries_primary_request_without_new_scan(
        self,
    ):
        self._assert_alias_retry_survives_completion("a" * 32)

    def test_lost_deduped_acknowledgement_retries_unidentified_primary_without_new_scan(
        self,
    ):
        self._assert_alias_retry_survives_completion(None)

    def test_retry_identity_capacity_refuses_new_identity_without_forgetting_old_one(
        self,
    ):
        first = self.start_blocked()
        retry = {**self.options, "request_id": "b" * 32}
        with patch("utils.infinidysk_v2_preview.MAX_REQUEST_ALIASES", 1):
            self.manager.preview_start(retry)
            with self.assertRaisesRegex(
                migration.InfiniDyskV2MigrationError, "too many pending requests"
            ):
                self.manager.preview_start({**self.options, "request_id": "c" * 32})
            accepted = self.manager.preview_start(retry)["task"]
        self.assertEqual(accepted["task_id"], first["task_id"])
        self.assertEqual(
            self.manager._preview_tasks._read()["request_aliases"], ["b" * 32]
        )
        self.finish()
        self.assertEqual(
            self.manager.preview_start(retry)["task"]["task_id"], first["task_id"]
        )
        self.assertEqual(self.manager._plan.call_count, 1)

    def test_changed_options_or_sync_requests_cannot_overlap_active_scan(self):
        self.start_blocked()
        with self.assertRaises(migration.InfiniDyskV2MigrationError):
            self.manager.preview_start({**self.options, "mount_backend": "fuse"})
        with self.assertRaises(migration.InfiniDyskV2MigrationError):
            self.manager.preview({"mode": "migrate"})
        self.finish()
        with self.assertRaisesRegex(
            migration.InfiniDyskV2MigrationError, "different selections"
        ):
            self.manager.preview_start({**self.options, "mount_backend": "fuse"})

    def test_synchronous_scan_also_refuses_async_start_until_finished(self):
        with patch.object(self.manager, "_plan", side_effect=self.delayed_plan):
            result = []
            worker = threading.Thread(
                target=lambda: result.append(self.manager.preview({"mode": "migrate"}))
            )
            worker.start()
            try:
                self.assertTrue(self.entered.wait(timeout=2))
                with self.assertRaises(migration.InfiniDyskV2MigrationError):
                    self.manager.preview_start(self.options)
            finally:
                self.release.set()
                worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertTrue(result[0]["ready"])

    def test_migration_rechecks_preview_lane_atomically_before_job_admission(self):
        preview = self.manager.preview({"mode": "migrate"})
        request = {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": dict.fromkeys(
                preview["required_acknowledgements"], True
            ),
        }
        original_request = self.manager._request_plan
        validated, admit = threading.Event(), threading.Event()
        failures = []
        source_bytes = (self.fixture.source / "db.sqlite").read_bytes()
        config = deepcopy(self.fixture.config.config)

        def pause_after_validation(request, **kwargs):
            result = original_request(request, **kwargs)
            validated.set()
            if not admit.wait(timeout=4):
                raise RuntimeError("fixture admission was not released")
            return result

        def start_migration():
            try:
                self.manager._start(request, "rehearsal")
            except Exception as error:
                failures.append(error)

        with patch.object(
            self.manager, "_request_plan", side_effect=pause_after_validation
        ):
            worker = threading.Thread(target=start_migration)
            worker.start()
            try:
                self.assertTrue(validated.wait(timeout=2))
                self.start_blocked()
                admit.set()
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(failures), 1)
                self.assertIsInstance(failures[0], migration.InfiniDyskV2MigrationError)
                self.assertIn("already scanning", str(failures[0]))
                self.assertEqual(self.manager._jobs(), [])
                self.assertEqual(self.fixture.executor.calls, [])
                self.assertEqual(
                    (self.fixture.source / "db.sqlite").read_bytes(), source_bytes
                )
                self.assertEqual(self.fixture.config.config, config)
            finally:
                admit.set()
                worker.join(timeout=5)
                self.finish()

    def test_sync_and_async_preview_admission_refuse_active_migration_before_scanning(
        self,
    ):
        with patch.object(self.manager, "has_blocking_job", return_value=True):
            with patch.object(self.manager, "_compute_preview") as compute:
                with self.assertRaises(migration.InfiniDyskV2MigrationError):
                    self.manager.preview({"mode": "migrate"})
                with self.assertRaises(migration.InfiniDyskV2MigrationError):
                    self.manager.preview_start(self.options)
        compute.assert_not_called()
        self.assertIsNone(self.manager._preview_tasks.busy)
        self.assertFalse(self.manager._preview_tasks.path.exists())

    def test_cancel_only_interrupts_metadata_preview_and_does_not_publish_result(self):
        task = self.start_blocked()
        cancel = self.manager.preview_cancel({"task_id": task["task_id"]})["task"]
        self.assertTrue(cancel["cancel_requested"])
        self.assertFalse(cancel["can_cancel"])
        terminal = self.finish()
        self.assertEqual(terminal["status"], "cancelled")
        self.assertIsNone(terminal["preview"])
        self.assertEqual(self.fixture.executor.calls, [])
        self.assertTrue(self.fixture.source.exists())
        self.assertFalse((self.manager.state_root / "previews").exists())

    def test_restart_reports_interruption_without_resurrecting_or_scanning(self):
        first = self.start_blocked()
        second = PreviewTasks(self.manager)
        state = second.status()["task"]
        self.assertEqual(state["task_id"], first["task_id"])
        self.assertEqual(state["status"], "interrupted")
        self.assertFalse(state["can_cancel"])
        self.assertIsNone(second.thread)
        self.assertIsNone(state["preview"])
        self.finish()
        reloaded = PreviewTasks(self.manager).status()["task"]
        self.assertEqual(reloaded["status"], "completed")
        self.assertTrue(reloaded["preview_available"])

    def test_expiry_begins_after_scan_and_consumed_or_expired_tokens_are_not_reoffered(
        self,
    ):
        with patch.object(migration.time, "time", return_value=100):
            self.start_blocked()
        with patch.object(migration.time, "time", return_value=200):
            completed = self.finish()
            preview = completed["preview"]
            self.assertEqual(preview["expires_at"], 200 + migration.PREVIEW_TTL)
            token = preview["preview_token"]
            path = (
                self.manager.state_root
                / "previews"
                / (migration.hashlib.sha256(token.encode()).hexdigest() + ".json")
            )
            saved = self.manager._read(path)
            saved["used_for_start"] = True
            self.manager._write(path, saved)
            consumed = self.manager.preview_task()["task"]
            self.assertEqual(consumed["preview_unavailable_reason"], "consumed")
            self.assertIsNone(consumed["preview"])
        with patch.object(
            migration.time, "time", return_value=preview["expires_at"] + 1
        ):
            expired = self.manager.preview_task()["task"]
            self.assertEqual(expired["preview_unavailable_reason"], "expired")
            self.assertIsNone(expired["preview"])

    def test_unexpected_worker_error_is_sanitized_and_invalid_identity_is_rejected(
        self,
    ):
        with patch.object(
            self.manager,
            "_compute_preview",
            side_effect=RuntimeError("private connection credential"),
        ):
            with self.assertLogs("utils.infinidysk_v2_preview", level="ERROR") as logs:
                self.manager.preview_start(self.options)
                failed = self.finish()
        self.assertIn("RuntimeError", " ".join(logs.output))
        self.assertNotIn("credential", " ".join(logs.output))
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("credential", failed["error"])
        self.assertIsNone(failed["preview"])
        for identifier in ("../state", "x", "f" * 33):
            with self.assertRaises(migration.InfiniDyskV2MigrationError):
                self.manager.preview_task({"task_id": identifier})

    def test_task_start_failure_retains_failure_and_releases_scan_lane(self):
        with patch(
            "utils.infinidysk_v2_preview.threading.Thread.start",
            side_effect=RuntimeError("private failure"),
        ):
            with self.assertRaises(migration.InfiniDyskV2MigrationError):
                self.manager.preview_start(self.options)
        self.assertEqual(self.manager.preview_task()["task"]["status"], "failed")
        self.assertTrue(self.manager.preview({"mode": "migrate"})["ready"])
        self.manager._preview_tasks.thread = None

    def test_inventory_errors_become_reviewable_blockers_and_blobs_are_measured_once(
        self,
    ):
        from utils import infinidysk_v2_blob_staging as blobs

        self.fixture.archive_profile()
        self.fixture.executor.supports_external_blob_staging = True
        options = {
            "mode": "migrate",
            "backup_destination_id": "nas",
            "reclaim_source_space": True,
            "offload_legacy_blobs": True,
        }
        with patch(
            "utils.infinidysk_v2_space_control.preflight_reuse",
            side_effect=InventoryError("Unsafe source inventory"),
        ):
            with patch.object(
                blobs,
                "blob_summary",
                return_value={"entries": 1, "logical_bytes": 20, "allocated_bytes": 20},
            ) as scan:
                preview = self.manager.preview(options)
        self.assertFalse(preview["ready"])
        self.assertIn("Unsafe source inventory", preview["blockers"])
        self.assertEqual(scan.call_count, 1)
        with patch("utils.infinidysk_v2_space_control.preflight_reuse"):
            with patch.object(
                blobs,
                "blob_summary",
                side_effect=InventoryError("Source changed while scanned"),
            ):
                preview = self.manager.preview(options)
        self.assertFalse(preview["ready"])
        self.assertIn("Source changed while scanned", preview["blockers"])


if __name__ == "__main__":
    unittest.main()
