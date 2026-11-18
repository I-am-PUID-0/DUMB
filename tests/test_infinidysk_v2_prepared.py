"""Prepared review survives restart and activates one accepted conversion."""

from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from tests import test_infinidysk_v2_migration as fixtures
from utils.infinidysk_v2_migration import InfiniDyskV2MigrationError


class PreparedExecutor(fixtures.Executor):
    def __init__(self):
        super().__init__()
        self._assert_unmounted = Mock()

    def prepare(self, plan, workspace, progress):
        self.calls.append("native_conversion")
        workspace["capture_source"]()
        (Path(workspace["candidate_data"]) / "infinidysk.sqlite").write_bytes(
            b"converted database"
        )
        receipt = self.receipt(plan)
        workspace["accept_import"](receipt)
        return {**receipt, "candidate_prepared": True}

    def activate(self, plan, workspace, progress, receipt):
        self.calls.append("activate")
        target = Path(plan["target"]["data_dir"])
        if target.exists():
            target.rmdir()
        Path(workspace["candidate_data"]).rename(target)
        workspace["activate_config"](plan["target"]["service"])
        return {**receipt, "generation_2_started": True}


class PreparedMigrationTests(unittest.TestCase):
    setUp = fixtures.InfiniDyskV2MigrationTests.setUp
    tearDown = fixtures.InfiniDyskV2MigrationTests.tearDown
    new_manager = fixtures.InfiniDyskV2MigrationTests.new_manager
    preview_request = fixtures.InfiniDyskV2MigrationTests.preview_request
    wait = fixtures.InfiniDyskV2MigrationTests.wait

    def prepare(self):
        self.executor = PreparedExecutor()
        self.manager.executor = self.executor
        runtime = Path(self.config.config["infinidysk"]["v2_runtime_dir"])
        runtime.mkdir()
        (runtime / "infinidysk").write_bytes(b"pinned native executable")
        mocked = patch(
            "utils.infinidysk_v2_prepared.installed_v2_version",
            return_value="v2.0.0-beta.6",
        )
        mocked.start()
        self.addCleanup(mocked.stop)
        _, request = self.preview_request()
        job = self.wait(self.manager.prepare(request))
        self.assertEqual("prepared", job["status"], job)
        self.assertTrue(job["can_activate"])
        self.assertTrue(job["can_rollback"])
        return job

    def activation(self, job):
        return {"job_id": job["job_id"], "confirmation": job["activate_confirmation"]}

    def test_same_conversion_is_activated_once_after_controller_restart(self):
        job = self.prepare()
        self.assertTrue(self.manager.has_blocking_job())
        self.assertFalse(self.manager.has_recovery_pending_job())
        self.assertTrue(self.manager.has_prepared_job())
        restarted = self.new_manager()
        self.managers.append(restarted)
        self.manager = restarted
        durable = restarted.get_job(job["job_id"])["job"]
        self.assertEqual("prepared", durable["status"])
        self.assertTrue(durable["can_activate"])
        completed = self.wait(restarted.activate(self.activation(durable)))
        self.assertEqual("completed", completed["status"], completed)
        self.assertEqual(["native_conversion", "activate"], self.executor.calls)
        self.assertEqual("2", self.config.config["infinidysk"]["runtime_generation"])
        self.assertFalse(restarted.has_blocking_job())
        with self.assertRaises(InfiniDyskV2MigrationError):
            restarted.activate(self.activation(durable))

    def test_old_prepare_worker_cannot_overwrite_its_same_job_activation(self):
        self.executor = PreparedExecutor()
        self.manager.executor = self.executor
        runtime = Path(self.config.config["infinidysk"]["v2_runtime_dir"])
        runtime.mkdir()
        (runtime / "infinidysk").write_bytes(b"pinned native executable")
        _, request = self.preview_request()
        published, finish_prepare, activating, finish_activation = (
            threading.Event() for _ in range(4)
        )
        original_lock = self.manager._lock
        manager = self.manager
        old_worker = []

        class ReviewBarrier:
            def __enter__(self):
                original_lock.acquire()
                return self

            def __exit__(self, *unused):
                pause = (
                    threading.current_thread().name.startswith("infinidysk-v2-")
                    and manager._active_id is None
                    and manager._thread is threading.current_thread()
                    and not published.is_set()
                )
                original_lock.release()
                if pause:
                    old_worker.append(threading.current_thread())
                    published.set()
                    if not finish_prepare.wait(5):
                        raise TimeoutError("prepare finalization barrier")

        manager._lock = ReviewBarrier()
        original_activate = self.executor.activate

        def activate(*args):
            activating.set()
            if not finish_activation.wait(5):
                raise TimeoutError("activation barrier")
            return original_activate(*args)

        self.executor.activate = activate
        with patch(
            "utils.infinidysk_v2_prepared.installed_v2_version",
            return_value="v2.0.0-beta.6",
        ):
            try:
                response = manager.prepare(request)
                self.assertTrue(published.wait(5))
                job = manager.get_job(response["job"]["job_id"])["job"]
                self.assertEqual("prepared", job["status"])
                manager.activate(self.activation(job))
                self.assertTrue(activating.wait(5))
                finish_prepare.set()
                old_worker[0].join(3)
                self.assertFalse(old_worker[0].is_alive())
                self.assertEqual(job["job_id"], manager._active_id)
                self.assertEqual(
                    "running", manager.get_job(job["job_id"])["job"]["status"]
                )
                finish_activation.set()
                manager._thread.join(3)
                self.assertFalse(manager._thread.is_alive())
                self.assertEqual(
                    "completed", manager.get_job(job["job_id"])["job"]["status"]
                )
                self.assertEqual(["native_conversion", "activate"], self.executor.calls)
            finally:
                finish_prepare.set()
                finish_activation.set()
                for worker in [*old_worker, manager._thread]:
                    if worker is not None:
                        worker.join(3)

    def test_failed_activation_restored_in_minimal_boot_requests_one_restart(self):
        job = self.prepare()
        self.handler.startup_phase = "recovery_required"
        self.executor.activate = Mock(
            side_effect=ValueError("fixture activation failed before exposure")
        )
        result = self.wait(self.manager.activate(self.activation(job)))
        self.assertEqual("failed_restored", result["status"], result)
        self.assertTrue(result["result"]["source_restored"])
        self.assertTrue(result["result"]["restart_required"])
        restarted = self.new_manager()
        self.managers.append(restarted)
        self.handler.startup_phase = "ready"
        final = restarted.get_job(job["job_id"])["job"]
        self.assertFalse(final["result"]["restart_required"])
        self.assertEqual("failed_restored", final["status"])

    def test_optional_cleanup_scheduling_failure_does_not_revoke_activation(self):
        job = self.prepare()
        private = self.manager._read(self.manager._job_path(job["job_id"]))
        private["snapshot"]["external_blob_staging"] = {"state": "released"}
        self.manager._write(self.manager._job_path(job["job_id"]), private)
        recovery = SimpleNamespace(public=Mock(return_value={}), hold=Mock())
        with (
            patch(
                "utils.infinidysk_v2_prepared.defer_cleanup",
                side_effect=RuntimeError("fixture thread start failure"),
            ),
            patch.object(self.manager, "_possibly_exposed", return_value=True),
            patch.object(self.manager, "_consumer_recovery", return_value=recovery),
        ):
            result = self.wait(self.manager.activate(self.activation(job)))
        self.assertEqual("completed", result["status"], result)
        recovery.hold.assert_not_called()
        self.assertEqual("2", self.config.config["infinidysk"]["runtime_generation"])

    def test_prepared_review_refuses_wrong_confirmation_without_starting_worker(self):
        job = self.prepare()
        request = self.activation(job)
        request["confirmation"] += "wrong"
        with self.assertRaises(InfiniDyskV2MigrationError):
            self.manager.activate(request)
        self.assertEqual(
            "prepared", self.manager.get_job(job["job_id"])["job"]["status"]
        )
        self.assertEqual(["native_conversion"], self.executor.calls)

    def test_modified_candidate_never_activates_and_v1_is_restored(self):
        job = self.prepare()
        private = self.manager._read(self.manager._job_path(job["job_id"]))
        candidate = Path(self.manager._workspace(private)["candidate_data"])
        (candidate / "infinidysk.sqlite").write_bytes(b"different database")
        result = self.wait(self.manager.activate(self.activation(job)))
        self.assertEqual("failed_restored", result["status"], result)
        self.assertNotIn("activate", self.executor.calls)
        self.assertEqual("1", self.config.config["infinidysk"]["runtime_generation"])

    def test_changed_runtime_never_reuses_prepared_proof(self):
        job = self.prepare()
        runtime = Path(self.config.config["infinidysk"]["v2_runtime_dir"])
        (runtime / "infinidysk").write_bytes(b"replacement executable")
        result = self.wait(self.manager.activate(self.activation(job)))
        self.assertEqual("failed_restored", result["status"], result)
        self.assertNotIn("activate", self.executor.calls)

    def test_source_change_is_rejected_before_activation(self):
        job = self.prepare()
        (self.source / "blobs" / "original").write_text("changed source content")
        result = self.wait(self.manager.activate(self.activation(job)))
        self.assertEqual("failed_restored", result["status"], result)
        self.assertNotIn("activate", self.executor.calls)

    def test_prepared_review_can_restore_v1_after_restart(self):
        job = self.prepare()
        restarted = self.new_manager()
        self.managers.append(restarted)
        response = restarted.rollback(
            {"job_id": job["job_id"], "confirmation": job["rollback_confirmation_text"]}
        )
        restarted._thread.join(timeout=3)
        self.assertFalse(restarted._thread.is_alive())
        result = restarted.get_job(job["job_id"])
        self.assertEqual("rolled_back", result["job"]["status"])
        self.assertFalse(restarted.has_blocking_job())
        self.assertEqual(["native_conversion", "rollback"], self.executor.calls)

    def test_activation_requires_exact_current_skipped_item_consent(self):
        job = self.prepare()
        private = self.manager._read(self.manager._job_path(job["job_id"]))
        private["import_report"] = {"review_required": True, "review_digest": "a" * 64}
        self.manager._write(self.manager._job_path(job["job_id"]), private)
        for request in (
            self.activation(job),
            {
                **self.activation(job),
                "acknowledgements": {"skipped_media": True},
                "skip_review_digest": "b" * 64,
            },
        ):
            with self.assertRaises(InfiniDyskV2MigrationError):
                self.manager.activate(request)
        self.assertEqual(["native_conversion"], self.executor.calls)
