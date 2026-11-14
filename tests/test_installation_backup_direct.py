"""Backup admission must be durable before any expensive source census."""

from copy import deepcopy
import threading
import unittest
from unittest.mock import patch
from tests import test_installation_backup as fixtures
from utils.installation_backup import InstallationBackupError
from utils.installation_backup_archive import inspect_archive


class DirectBackupTests(unittest.TestCase):
    setUp = fixtures.ManagerTests.setUp
    join_worker = fixtures.ManagerTests.join_worker

    def request(self, identifier="a" * 32):
        return {
            "request_id": identifier,
            "destination_id": "fixture",
            "compression": "none",
            "confirmation": "BACK UP DUMB INSTALLATION",
            "acknowledgements": dict.fromkeys(
                ("downtime", "secrets", "external_state"), True
            ),
        }

    def test_direct_start_returns_durable_job_before_classification_and_never_live_scans(
        self,
    ):
        entered, release = threading.Event(), threading.Event()
        original = self.manager._backup_plan

        def held(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(*args, **kwargs)

        with (
            patch.object(self.manager, "_backup_plan", held),
            patch(
                "utils.installation_backup.capacity",
                side_effect=AssertionError("No live census"),
            ),
        ):
            try:
                accepted = self.manager.start_direct(self.request())
                self.assertTrue(entered.wait(5))
                self.assertEqual(accepted["job"]["request_id"], "a" * 32)
                current = self.manager.status()["job"]
                self.assertEqual(current["job_id"], accepted["job"]["job_id"])
                self.assertEqual(current["stage"], "classifying")
                self.assertTrue(
                    self.manager.state_root.joinpath("active.json").exists()
                )
                self.assertEqual(self.handler.events, [])
            finally:
                release.set()
                self.join_worker()
        current = self.manager.status()["job"]
        self.assertEqual(current["status"], "completed", current)
        self.assertTrue(current["archive_verified"])
        private = self.manager._job(current["job_id"])
        self.assertTrue(private["capacity"]["from_census"])
        with self.storage.open_artifact(
            private["result"]["archive"]["reference"]
        ) as stream:
            proof = inspect_archive(stream, self.manager._workspace("e" * 32))
        self.assertTrue(proof["verified"])

    def test_direct_retries_return_same_job_and_changed_options_are_rejected(self):
        request = self.request()
        result = self.manager.start_direct(request)
        self.join_worker()
        self.assertEqual(
            self.manager.start_direct(request)["job"]["job_id"], result["job"]["job_id"]
        )
        changed = deepcopy(request)
        changed["destination_id"] = "different"
        with self.assertRaisesRegex(InstallationBackupError, "different options"):
            self.manager.start_direct(changed)
        self.assertEqual(self.handler.events, [("stop", "App"), ("start", "App")])

    def test_direct_requires_consent_before_any_admission(self):
        for field in ("confirmation", "acknowledgements"):
            request = self.request()
            request[field] = "" if field == "confirmation" else {}
            with self.assertRaises(InstallationBackupError):
                self.manager.start_direct(request)
        self.assertFalse(self.manager.has_blocking_job())
        self.assertIsNone(self.manager.status()["job"])
        self.assertEqual(self.handler.events, [])

    def test_direct_unavailable_destination_fails_observably_without_stopping_services(
        self,
    ):
        from utils.backup_storage import BackupStorageError

        with patch.object(
            self.storage,
            "bound_identity",
            side_effect=BackupStorageError("Mounted destination unavailable"),
        ):
            result = self.manager.start_direct(self.request())
            self.join_worker()
        current = self.manager.get_job(result["job"]["job_id"])["job"]
        self.assertEqual(current["status"], "failed", current)
        self.assertFalse(current["can_rollback"])
        self.assertFalse(self.manager.has_blocking_job())
        self.assertEqual(self.handler.events, [])
        # A failed early job must not corrupt destination-management usage.
        self.assertEqual(
            self.manager._job(current["job_id"])["plan"]["destination"][
                "destination_id"
            ],
            "fixture",
        )
        self.storage._profile_usage("fixture")

    def test_default_selection_is_pinned_in_pending_job_and_replay(self):
        entered, release = threading.Event(), threading.Event()
        original = self.manager._backup_plan
        request = self.request()
        request["destination_id"] = "@default"

        def held(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(*args, **kwargs)

        with patch.object(self.manager, "_backup_plan", held):
            try:
                accepted = self.manager.start_direct(request)
                self.assertTrue(entered.wait(5))
                plan = self.manager._job(accepted["job"]["job_id"])["plan"]
                self.assertEqual(plan["destination"]["destination_id"], "fixture")
                self.assertEqual(plan["options"]["destination_id"], "fixture")
                self.storage._profile_usage("fixture")
                self.assertEqual(
                    self.manager.start_direct(request)["job"]["job_id"],
                    accepted["job"]["job_id"],
                )
            finally:
                release.set()
                self.join_worker()

    def test_admitted_scheduled_capacity_failure_waits_normal_interval(self):
        self.manager.update_schedule(
            {"enabled": True, "destination_id": "fixture", "interval_hours": 1}
        )
        self.manager._write(self.manager._state("schedule.json"), {"next_due_at": 0})
        with (
            patch.object(self.manager, "clock", return_value=100),
            patch(
                "utils.installation_backup.capacity_from_census",
                return_value={"status": "insufficient", "from_census": True},
            ),
        ):
            self.manager.tick()
            self.join_worker()
            runtime = self.manager._schedule_runtime()
        self.assertEqual(runtime["next_due_at"], 3700)
        self.assertEqual(self.handler.events, [("stop", "App"), ("start", "App")])
        current = self.manager.status()["job"]
        self.assertEqual(current["status"], "failed", current)
        self.assertEqual(current["capacity"]["status"], "insufficient")

    def test_cold_capacity_failure_emits_no_archive_and_resumes_without_restore(self):
        with patch(
            "utils.installation_backup.capacity_from_census",
            return_value={"status": "insufficient", "from_census": True},
        ):
            result = self.manager.start_direct(self.request())
            self.join_worker()
        current = self.manager.get_job(result["job"]["job_id"])["job"]
        self.assertEqual(current["status"], "failed", current)
        self.assertIn("Insufficient space", current["errors"][0])
        self.assertEqual(self.handler.events, [("stop", "App"), ("start", "App")])
        self.assertFalse(current["can_rollback"])
        self.assertEqual(self.manager.catalog("fixture")["artifacts"], [])

    def test_scheduled_backup_has_no_live_preview_or_second_capacity_scan(self):
        self.manager.update_schedule(
            {"enabled": True, "destination_id": "fixture", "interval_hours": 1}
        )
        self.manager._write(self.manager._state("schedule.json"), {"next_due_at": 0})
        with (
            patch.object(
                self.manager,
                "preview",
                side_effect=AssertionError("No scheduled preview"),
            ),
            patch(
                "utils.installation_backup.capacity",
                side_effect=AssertionError("No live census"),
            ),
        ):
            self.manager.tick()
            self.join_worker()
        current = self.manager.status()["job"]
        self.assertTrue(current["scheduled"])
        self.assertEqual(current["status"], "completed", current)

    def test_reviewed_start_does_not_recheck_and_remains_replayable_after_expiry(self):
        preview = self.manager.preview({"destination_id": "fixture"})
        request = {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": self.request()["acknowledgements"],
        }
        with patch(
            "utils.installation_backup.capacity",
            side_effect=AssertionError("No second live census"),
        ):
            result = self.manager.start(request)
            self.join_worker()
        with patch.object(
            self.manager, "clock", return_value=preview["expires_at"] + 1
        ):
            replay = self.manager.start(request)
        self.assertEqual(result["job"]["job_id"], replay["job"]["job_id"])
