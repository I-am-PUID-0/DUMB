"""Verified archive acceptance survives independent service and boot recovery."""

from copy import deepcopy
import io
import unittest
from unittest.mock import patch

from tests import test_installation_backup as fixtures
from utils import installation_backup as module
from utils.installation_backup import InstallationBackupError
from utils.installation_backup_archive import InstallationArchiveError, inspect_archive


class BackupAcceptanceTests(unittest.TestCase):
    setUp = fixtures.ManagerTests.setUp
    join_worker = fixtures.ManagerTests.join_worker
    start_backup = fixtures.ManagerTests.start_backup

    def new_controller(self, label):
        with patch.object(
            module,
            "_boot_identity",
            return_value=[label, "reserved-process", "reserved-boot"],
        ):
            manager = module.InstallationBackupManager(
                self.config, self.handler, storage=self.storage
            )
        manager.workspace_root = self.manager.workspace_root
        return manager

    def accepted_but_resume_failed(self):
        with patch.object(
            self.manager,
            "_resume",
            side_effect=InstallationBackupError("Fixture service cannot resume."),
        ) as resume:
            job = self.start_backup()
        # The exception handler must not restart this same failing service a
        # second time or repeat archive work after the archive already exists.
        self.assertEqual(resume.call_count, 1)
        self.assertEqual(job["status"], "recovery_required", job)
        self.assertEqual(job["stage"], "resume_required")
        self.assertTrue(job["archive_verified"])
        self.assertTrue(job["result"]["complete"])
        self.assertFalse(job["archive_publication_uncertain"])
        self.assertIn("verified and retained", job["message"])
        self.assertNotIn("not accepted", job["message"])
        self.assertTrue(self.manager.has_blocking_job())
        self.assertTrue(job["can_rollback"])
        return job

    def test_real_verified_archive_survives_resume_failure_finish_and_new_boot(self):
        original_payload = self.payload.read_bytes()
        job = self.accepted_but_resume_failed()
        receipt = deepcopy(job["result"]["archive"])
        self.assertEqual(self.manager.catalog("fixture")["artifacts"], [receipt])
        with self.storage.open_artifact(receipt["reference"]) as stream:
            raw = stream.read()
        self.assertGreater(len(raw), 0)
        scratch = self.root / "readback"
        scratch.mkdir()
        proof = inspect_archive(io.BytesIO(raw), scratch)
        self.assertTrue(proof["verified"])

        recovery = self.new_controller("recovery")
        recovery.initialize_controller()
        with patch.object(recovery, "_resume") as resume:
            finished = recovery.rollback(
                job["job_id"], {"confirmation": job["rollback_confirmation_text"]}
            )["job"]
        resume.assert_not_called()
        self.assertEqual(finished["status"], "rolled_back")
        self.assertTrue(finished["restart_required"])
        self.assertTrue(finished["archive_verified"])
        self.assertEqual(finished["result"]["archive"], receipt)
        self.assertTrue(finished["result"]["complete"])
        self.assertTrue(recovery.has_blocking_job())

        restarted = self.new_controller("ordinary-startup")
        restarted.initialize_controller()
        final = restarted.get_job(job["job_id"])["job"]
        self.assertFalse(final["restart_required"])
        self.assertFalse(final["result"]["restart_required"])
        self.assertTrue(final["result"]["complete"])
        self.assertTrue(final["archive_verified"])
        self.assertEqual(final["result"]["archive"], receipt)
        self.assertFalse(restarted.has_blocking_job())
        self.assertEqual(
            self.storage.list_artifacts("fixture", "installation"), [receipt]
        )
        with self.storage.open_artifact(receipt["reference"]) as stream:
            self.assertEqual(stream.read(), raw)
        self.assertEqual(self.payload.read_bytes(), original_payload)

    def test_scheduled_archive_success_keeps_normal_interval_and_skips_retention_on_resume_failure(
        self,
    ):
        self.manager.clock = lambda: 1000.0
        self.config.config["installation_backup"] = {
            "enabled": True,
            "destination_id": "fixture",
            "interval_hours": 8,
            "keep_last": 1,
        }
        self.config.save_config()
        preview = self.manager.preview({"destination_id": "fixture"})
        request = {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": {
                "downtime": True,
                "secrets": True,
                "external_state": True,
            },
        }
        record = self.manager._validated_request("backup", request)
        with (
            patch.object(
                self.manager,
                "_resume",
                side_effect=InstallationBackupError("Fixture service cannot resume."),
            ),
            patch.object(self.manager, "_retention") as retention,
        ):
            accepted = self.manager._admit("backup", record, scheduled=True)
            self.join_worker()
        job = self.manager.get_job(accepted["job"]["job_id"])["job"]
        self.assertTrue(job["archive_verified"])
        self.assertEqual(job["status"], "recovery_required")
        runtime = self.manager._schedule_runtime()
        self.assertEqual(runtime["status"], "recovery_required")
        self.assertEqual(runtime["last_success_at"], 1000)
        self.assertEqual(runtime["next_due_at"], 1000 + 8 * 3600)
        self.assertEqual(runtime["error"], "Fixture service cannot resume.")
        retention.assert_not_called()

    def test_prepublication_failure_never_claims_a_verified_archive(self):
        with patch.object(
            module,
            "write_archive",
            side_effect=InstallationArchiveError("Fixture source consistency failed."),
        ):
            job = self.start_backup()
        self.assertEqual(job["status"], "failed")
        self.assertFalse(job["archive_verified"])
        self.assertFalse(job["archive_publication_uncertain"])
        self.assertNotIn("result", job)
        self.assertIn("not accepted", job["message"])
        self.assertEqual(self.storage.list_artifacts("fixture", "installation"), [])

    def test_completed_transfer_or_incomplete_receipt_is_not_acceptance_proof(self):
        job = {
            "job_type": "installation_backup",
            "progress": {"bytes_processed": 6 * 1024**4, "percent": 100},
            "stage": "resuming",
            "result": {"complete": True, "archive": {"verified": True}},
        }
        self.assertIsNone(self.manager._verified_backup_archive(job))
        job["job_type"] = "installation_restore"
        self.assertIsNone(self.manager._verified_backup_archive(job))

    def test_published_artifact_without_job_receipt_is_uncertain_and_survives_recovery(
        self,
    ):
        original = self.manager._progress

        def interrupted(identifier, stage, message, **fields):
            if fields.get("result", {}).get("archive"):
                raise InstallationBackupError(
                    "Fixture interrupted recording the receipt."
                )
            return original(identifier, stage, message, **fields)

        with patch.object(self.manager, "_progress", interrupted):
            job = self.start_backup()
        self.assertEqual(job["status"], "failed")
        self.assertFalse(job["archive_verified"])
        self.assertTrue(job["archive_publication_uncertain"])
        self.assertNotIn("not accepted", job["message"])
        self.assertIn("could not be confirmed", job["message"])
        artifacts = self.storage.list_artifacts("fixture", "installation")
        self.assertEqual(len(artifacts), 1)
        self.assertTrue(artifacts[0]["verified"])
        self.assertNotIn("result", job)

    def test_crash_during_publication_keeps_uncertainty_after_status_changes_stage(
        self,
    ):
        job = self.accepted_but_resume_failed()
        stored = self.manager._job(job["job_id"])
        stored.pop("result")
        stored.update(
            status="running", stage="publishing", archive_publication_started=True
        )
        self.manager._save(stored)
        recovery = self.new_controller("publication-interrupted")
        public = recovery.status()["job"]
        self.assertEqual(public["stage"], "interrupted")
        self.assertFalse(public["archive_verified"])
        self.assertTrue(public["archive_publication_uncertain"])
        finished = recovery.rollback(
            job["job_id"], {"confirmation": public["rollback_confirmation_text"]}
        )["job"]
        self.assertTrue(finished["archive_publication_uncertain"])
        restarted = self.new_controller("ordinary-startup")
        restarted.initialize_controller()
        final = restarted.get_job(job["job_id"])["job"]
        self.assertTrue(final["archive_publication_uncertain"])
        self.assertFalse(final["archive_verified"])

    def test_uncertain_scheduled_publication_does_not_claim_success_or_repeat_immediately(
        self,
    ):
        self.manager.clock = lambda: 1000.0
        self.config.config["installation_backup"] = {
            "enabled": True,
            "destination_id": "fixture",
            "interval_hours": 8,
        }
        self.config.save_config()
        preview = self.manager.preview({"destination_id": "fixture"})
        request = {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": {
                "downtime": True,
                "secrets": True,
                "external_state": True,
            },
        }
        record = self.manager._validated_request("backup", request)
        original = self.manager._progress

        def interrupted(identifier, stage, message, **fields):
            if fields.get("result", {}).get("archive"):
                raise InstallationBackupError(
                    "Fixture interrupted recording the receipt."
                )
            return original(identifier, stage, message, **fields)

        with patch.object(self.manager, "_progress", interrupted):
            accepted = self.manager._admit("backup", record, scheduled=True)
            self.join_worker()
        job = self.manager.get_job(accepted["job"]["job_id"])["job"]
        self.assertEqual(job["status"], "failed")
        self.assertFalse(job["archive_verified"])
        self.assertTrue(job["archive_publication_uncertain"])
        runtime = self.manager._schedule_runtime()
        self.assertEqual(runtime["status"], "error")
        self.assertIsNone(runtime["last_success_at"])
        self.assertEqual(runtime["next_due_at"], 1000 + 8 * 3600)
        self.assertFalse(self.manager.has_blocking_job())
        self.assertEqual(len(self.storage.list_artifacts("fixture", "installation")), 1)

    def test_postcompletion_followup_error_keeps_backup_complete_and_services_available(
        self,
    ):
        self.config.config["installation_backup"] = {
            "enabled": True,
            "destination_id": "fixture",
            "interval_hours": 8,
        }
        self.config.save_config()
        preview = self.manager.preview({"destination_id": "fixture"})
        record = self.manager._validated_request(
            "backup",
            {
                "preview_token": preview["preview_token"],
                "confirmation": preview["confirmation_text"],
                "acknowledgements": {
                    "downtime": True,
                    "secrets": True,
                    "external_state": True,
                },
            },
        )
        with patch.object(
            self.manager, "_retention", side_effect=ValueError("fixture")
        ):
            accepted = self.manager._admit("backup", record, scheduled=True)
            self.join_worker()
        job = self.manager.get_job(accepted["job"]["job_id"])["job"]
        self.assertEqual(job["status"], "completed")
        self.assertTrue(job["archive_verified"])
        self.assertFalse(job["can_rollback"])
        self.assertFalse(self.manager.has_blocking_job())
        self.assertEqual(self.handler.events, [("stop", "App"), ("start", "App")])
