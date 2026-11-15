"""Long restoration is observable; dispatch failures retain recovery state."""

from copy import deepcopy
import threading
import unittest
from unittest.mock import Mock, patch

from utils.infinidysk_v2_recovery_worker import start_rollback_worker


class RecoveryWorkerTests(unittest.TestCase):
    def setUp(self):
        self.job = {"job_id": "1" * 32, "status": "rolling_back"}
        self.release = threading.Event()
        self.started = threading.Event()
        self.addCleanup(self.release.set)
        self.update_lock = threading.Lock()
        self.update_lock.acquire()
        self.manager = Mock()
        self.manager._lock = threading.RLock()
        self.manager._thread = None
        self.manager._active_id = self.job["job_id"]
        self.manager._rollback_active = True
        self.manager._public_job.side_effect = deepcopy
        self.manager._job_path.side_effect = lambda job_id: job_id
        self.manager._write.side_effect = lambda _path, job: setattr(
            self.manager, "saved", deepcopy(job)
        )
        self.addCleanup(self.finish_owned_worker)

    def finish_owned_worker(self):
        self.release.set()
        worker = self.manager._thread
        if worker is not None:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "the test recovery worker leaked")
        if self.update_lock.locked():
            self.update_lock.release()

    def operation(self, job, update_lock):
        try:
            self.started.set()
            if not self.release.wait(timeout=5):
                raise AssertionError("the test did not release its recovery worker")
            job["status"] = "rolled_back"
        finally:
            with self.manager._lock:
                self.manager._write(job["job_id"], job)
                self.manager._active_id = None
                self.manager._rollback_active = False
            update_lock.release()

    def test_long_restore_returns_running_job_and_does_not_block_status(self):
        result = start_rollback_worker(
            self.manager, self.job, self.update_lock, self.operation
        )
        self.assertTrue(self.started.wait(timeout=5))
        self.assertEqual(result["job"]["status"], "rolling_back")
        self.assertTrue(self.manager._thread.is_alive())
        self.assertTrue(self.update_lock.locked())
        # Status readers can acquire the same controller lock while the
        # operation is waiting on a slow external archive.
        with self.manager._lock:
            self.assertEqual(
                self.manager._public_job(self.job)["status"], "rolling_back"
            )
        self.release.set()
        self.manager._thread.join(timeout=5)
        self.assertFalse(self.manager._thread.is_alive())
        self.assertEqual(self.manager.saved["status"], "rolled_back")
        self.assertFalse(self.update_lock.locked())

    def test_background_failure_retains_operation_error_without_thread_traceback(self):
        def failure(job, update_lock):
            try:
                job.update(
                    status="recovery_required", error="Reconnect the migration share."
                )
                self.manager._write(job["job_id"], job)
                raise ValueError("private archive detail")
            finally:
                self.manager._active_id = None
                self.manager._rollback_active = False
                update_lock.release()

        with patch("threading.excepthook") as unhandled:
            start_rollback_worker(self.manager, self.job, self.update_lock, failure)
            self.manager._thread.join(timeout=5)
        self.assertFalse(self.manager._thread.is_alive())
        self.assertEqual(self.manager.saved["status"], "recovery_required")
        self.assertEqual(self.manager.saved["error"], "Reconnect the migration share.")
        self.assertFalse(self.update_lock.locked())
        unhandled.assert_not_called()

    def test_worker_start_failure_keeps_recovery_available_and_releases_admission(self):
        operation = Mock()
        with (
            patch("threading.Thread.start", side_effect=RuntimeError("cannot start")),
            self.assertRaisesRegex(RuntimeError, "Recovery could not start"),
        ):
            start_rollback_worker(self.manager, self.job, self.update_lock, operation)
        operation.assert_not_called()
        self.assertEqual(self.manager.saved["status"], "recovery_required")
        self.assertIn("files were kept", self.manager.saved["error"])
        self.assertFalse(self.update_lock.locked())
        self.assertIsNone(self.manager._active_id)
        self.assertFalse(self.manager._rollback_active)
        self.assertIsNone(self.manager._thread)


if __name__ == "__main__":
    unittest.main()
