"""Cooperative backup cancellation, publication fencing and cold-state reporting."""

from contextlib import closing
from copy import deepcopy
import threading
import unittest
from unittest.mock import patch

from tests import test_installation_backup as fixtures
from utils.installation_backup import InstallationBackupError


class BackupCancellationTests(unittest.TestCase):
    join_worker = fixtures.ManagerTests.join_worker

    def setUp(self):
        fixtures.ManagerTests.setUp(self)
        self.release = threading.Event()
        self.addCleanup(self.release_worker)

    def release_worker(self):
        self.release.set()
        self.join_worker()

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

    def assert_cancelled(self, identifier):
        self.join_worker()
        final = self.manager.get_job(identifier)["job"]
        self.assertEqual(final["status"], "cancelled", final)
        self.assertEqual(final["stage"], "cancelled")
        self.assertTrue(final["cancel_requested"])
        self.assertFalse(final["can_cancel"])
        self.assertFalse(final["can_rollback"])
        self.assertFalse(final.get("services_stopped"))
        self.assertFalse(self.manager.has_blocking_job())
        self.assertEqual(self.storage.list_artifacts("fixture", "installation"), [])
        self.assertEqual(list(self.root.joinpath("archives").rglob("*.partial")), [])
        self.assertEqual(list(self.root.joinpath("archives").rglob("*.tar")), [])
        self.assertEqual(final["errors"], [])
        return final

    def test_cancel_during_cold_inventory_keeps_lease_until_worker_and_resume_finish(
        self,
    ):
        from utils import installation_backup_archive as archive

        entered = threading.Event()
        request = self.request()
        original_rows = archive.source_rows
        paused = []

        def held_rows(*args, **kwargs):
            with closing(original_rows(*args, **kwargs)) as rows:
                for row in rows:
                    yield row
                    if not paused:
                        paused.append(True)
                        entered.set()
                        self.assertTrue(self.release.wait(10))

        with patch.object(archive, "source_rows", held_rows):
            result = self.manager.start(request)
            identifier = result["job"]["job_id"]
            self.assertTrue(entered.wait(5))
            current = self.manager.get_job(identifier)["job"]
            self.assertTrue(current["can_cancel"])
            self.assertTrue(current["services_stopped"])
            self.assertEqual(current["stopped_process_names"], ["App"])
            accepted = self.manager.cancel(identifier)["job"]
            self.assertTrue(accepted["cancel_requested"])
            self.assertFalse(accepted["can_cancel"])
            self.assertEqual(accepted["status"], "running")
            self.assertTrue(self.manager.has_blocking_job())
            self.assertTrue(self.manager._state("active.json").exists())
            self.assertEqual(self.handler.events, [("stop", "App")])
            self.assertTrue(self.manager.cancel(identifier)["job"]["cancel_requested"])
            self.release.set()
            self.assert_cancelled(identifier)
        self.assertEqual(self.handler.events, [("stop", "App"), ("start", "App")])

    def test_cancel_during_readback_discards_partial_without_restoring_payload(self):
        request = self.request()
        entered = threading.Event()
        original = self.manager._backup_verifier
        payload = self.payload.read_bytes()

        def held(*args, **kwargs):
            entered.set()
            self.assertTrue(self.release.wait(10))
            return original(*args, **kwargs)

        with patch.object(self.manager, "_backup_verifier", held):
            result = self.manager.start(request)
            identifier = result["job"]["job_id"]
            self.assertTrue(entered.wait(5))
            self.manager.cancel(identifier)
            self.release.set()
            self.assert_cancelled(identifier)
        self.assertEqual(self.payload.read_bytes(), payload)

    def test_queued_cancel_does_not_stop_any_application(self):
        request = self.request()
        entered = threading.Event()
        original = self.manager._run

        def held(identifier):
            entered.set()
            self.assertTrue(self.release.wait(10))
            original(identifier)

        with patch.object(self.manager, "_run", held):
            result = self.manager.start(request)
            identifier = result["job"]["job_id"]
            self.assertTrue(entered.wait(5))
            self.assertEqual(
                self.manager.get_job(identifier)["job"]["status"], "queued"
            )
            self.manager.cancel(identifier)
            self.release.set()
            self.assert_cancelled(identifier)
        self.assertEqual(self.handler.events, [])

    def test_cancel_during_partial_quiesce_resumes_only_captured_stopped_services(self):
        second = self.application.parent / "second"
        second.mkdir()
        self.config.config["second"] = {
            "enabled": True,
            "process_name": "Second",
            "config_dir": str(second),
        }
        self.config.save_config()
        self.handler.process_names["Second"] = fixtures.Process()
        request = self.request()
        entered = threading.Event()
        original = self.handler.stop_process

        def held(name):
            result = original(name)
            entered.set()
            self.assertTrue(self.release.wait(10))
            return result

        with patch.object(self.handler, "stop_process", held):
            result = self.manager.start(request)
            identifier = result["job"]["job_id"]
            self.assertTrue(entered.wait(5))
            current = self.manager.get_job(identifier)["job"]
            self.assertFalse(current["services_stopped"])
            self.assertEqual(current["stopped_process_names"], [])
            self.manager.cancel(identifier)
            self.release.set()
            self.assert_cancelled(identifier)
        stopped = [name for action, name in self.handler.events if action == "stop"]
        started = [name for action, name in self.handler.events if action == "start"]
        self.assertEqual(len(stopped), 1)
        self.assertEqual(started, stopped)

    def test_cancel_at_large_file_chunks_in_each_data_pass(self):
        for stage in ("archiving", "checking_source", "verifying"):
            with self.subTest(stage=stage):
                # Each subcase uses its own retained state and exact worker.
                case = BackupCancellationTests()
                case.setUp()
                try:
                    case.application.joinpath("large.bin").write_bytes(
                        b"reserved-" * 600000
                    )
                    request = case.request()
                    entered = threading.Event()
                    original = case.manager._backup_progress

                    def held_progress(identifier):
                        report = original(identifier)

                        def held(**event):
                            report(**event)
                            value = (
                                event.get("logical_bytes")
                                if stage == "archiving"
                                else event.get("bytes_processed")
                            )
                            if (
                                event["stage"] == stage
                                and value
                                and not entered.is_set()
                            ):
                                entered.set()
                                self.assertTrue(case.release.wait(10))

                        return held

                    with patch.object(case.manager, "_backup_progress", held_progress):
                        result = case.manager.start(request)
                        identifier = result["job"]["job_id"]
                        self.assertTrue(entered.wait(5))
                        case.manager.cancel(identifier)
                        case.release.set()
                        case.assert_cancelled(identifier)
                finally:
                    case.doCleanups()

    def test_publication_fence_rejects_late_cancellation_and_keeps_valid_archive(self):
        request = self.request()
        entered = threading.Event()
        original = self.manager._close_backup_cancel

        def held(identifier):
            original(identifier)
            entered.set()
            self.assertTrue(self.release.wait(10))

        with patch.object(self.manager, "_close_backup_cancel", held):
            result = self.manager.start(request)
            identifier = result["job"]["job_id"]
            self.assertTrue(entered.wait(5))
            self.assertFalse(self.manager.get_job(identifier)["job"]["can_cancel"])
            with self.assertRaisesRegex(InstallationBackupError, "publishing"):
                self.manager.cancel(identifier)
            self.release.set()
            self.join_worker()
        final = self.manager.get_job(identifier)["job"]
        self.assertEqual(final["status"], "completed", final)
        self.assertEqual(len(self.storage.list_artifacts("fixture", "installation")), 1)

    def test_cancelled_backup_with_failed_resume_retains_recovery_and_lease(self):
        request = self.request()
        entered = threading.Event()

        def held(_identifier):
            entered.set()
            self.assertTrue(self.release.wait(10))
            self.manager._check_backup_cancel(_identifier)

        with (
            patch.object(self.manager, "_backup", held),
            patch.object(
                self.manager,
                "_resume",
                side_effect=InstallationBackupError("Fixture service cannot resume."),
            ),
        ):
            result = self.manager.start(request)
            identifier = result["job"]["job_id"]
            self.assertTrue(entered.wait(5))
            self.manager.cancel(identifier)
            self.release.set()
            self.join_worker()
        final = self.manager.get_job(identifier)["job"]
        self.assertEqual(final["status"], "recovery_required")
        self.assertTrue(final["can_rollback"])
        self.assertTrue(self.manager.has_blocking_job())
        self.assertIn("Fixture service cannot resume.", final["errors"])

    def test_cancelled_scheduled_run_waits_for_its_next_normal_interval(self):
        self.manager.clock = lambda: 1000.0
        self.config.config["installation_backup"] = {
            "enabled": True,
            "destination_id": "fixture",
            "interval_hours": 8,
        }
        self.config.save_config()
        request = self.request()
        record = self.manager._validated_request("backup", request)
        entered = threading.Event()

        def held(identifier):
            entered.set()
            self.assertTrue(self.release.wait(10))
            self.manager._check_backup_cancel(identifier)

        with patch.object(self.manager, "_backup", held):
            result = self.manager._admit("backup", record, scheduled=True)
            identifier = result["job"]["job_id"]
            self.assertTrue(entered.wait(5))
            self.assertTrue(self.manager.get_job(identifier)["job"]["scheduled"])
            self.manager.cancel(identifier)
            self.release.set()
            self.assert_cancelled(identifier)
        runtime = self.manager._schedule_runtime()
        self.assertEqual(runtime["status"], "cancelled")
        self.assertIsNone(runtime["error"])
        self.assertEqual(runtime["next_due_at"], 1000 + 8 * 3600)

    def test_restore_or_old_controller_job_cannot_be_cancelled(self):
        request = self.request()
        entered = threading.Event()

        def held(identifier):
            entered.set()
            self.assertTrue(self.release.wait(10))
            self.manager._check_backup_cancel(identifier)

        with patch.object(self.manager, "_backup", held):
            result = self.manager.start(request)
            identifier = result["job"]["job_id"]
            self.assertTrue(entered.wait(5))
            job = self.manager._job(identifier)
            job["job_type"] = "installation_restore"
            self.manager._save(job)
            with self.assertRaisesRegex(
                InstallationBackupError, "Only a running backup"
            ):
                self.manager.cancel(identifier)
            job["job_type"], job["session"] = (
                "installation_backup",
                "another-controller",
            )
            self.manager._save(job)
            with self.assertRaisesRegex(InstallationBackupError, "cannot be cancelled"):
                self.manager.cancel(identifier)
            job["session"] = self.manager._session
            self.manager._save(job)
            self.manager.cancel(identifier)
            self.release.set()
            self.assert_cancelled(identifier)


class BackupPercentageTests(unittest.TestCase):
    def test_phase_percent_uses_exact_totals_and_never_reports_early_verification_success(
        self,
    ):
        fixture = fixtures.ManagerTests()
        fixture.setUp()
        try:
            observed = []
            with patch.object(
                fixture.manager,
                "_progress",
                side_effect=lambda _id, stage, _message, **values: observed.append(
                    {"stage": stage, **deepcopy(values["progress"])}
                ),
            ):
                report = fixture.manager._backup_progress("a" * 32)
                report(stage="inventorying", entries=100, force=True)
                report(
                    stage="archiving",
                    logical_bytes=25,
                    total_logical_bytes=100,
                    force=True,
                )
                report(stage="checking_source", entries=2, total_entries=4, force=True)
                report(
                    stage="verifying",
                    bytes_processed=100,
                    total_bytes=100,
                    complete=True,
                    force=True,
                )
            self.assertNotIn("percent", observed[0])
            self.assertEqual(observed[1]["percent"], 25)
            self.assertEqual(observed[2]["percent"], 50)
            self.assertEqual(observed[3]["percent"], 99.9)
            self.assertFalse(observed[3]["complete"])
            self.assertTrue(all(row["percent_scope"] == "phase" for row in observed))
        finally:
            fixture.doCleanups()
