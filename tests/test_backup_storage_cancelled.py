"""Terminal cancellation must release destination use without hiding recovery."""

import unittest

from tests import test_backup_storage as fixtures
from tests import test_backup_storage_management as management
from utils.backup_storage import BackupStorageError


class CancelledInstallationRegistryTests(unittest.TestCase):
    setUp = fixtures.BackupStorageTests.setUp
    register = fixtures.BackupStorageTests.register
    recovery_record = management.BackupStorageManagementTests.recovery_record

    def cancelled(self, **changes):
        job = {
            "job_id": "a" * 32,
            "job_type": "installation_backup",
            "status": "cancelled",
            "can_rollback": False,
            "restart_required": False,
            "plan": {
                "direct_start": True,
                "destination": {"destination_id": "nas"},
                "options": {"destination_id": "nas", "compression": "none"},
            },
        }
        job.update(changes)
        self.recovery_record(
            "installation-backups/jobs/" + job["job_id"] + ".json", job
        )
        return job

    def usage(self):
        return {
            row["kind"] for row in self.storage.profiles()["destinations"][0]["usage"]
        }

    def test_cancelled_history_allows_removal_and_preserves_closed_archives(self):
        self.register()
        receipt = self.storage.write_json("nas", "example", {"history": True})
        artifact = (
            self.destination
            / ".dumb-backups"
            / "nas"
            / "example"
            / (receipt["artifact_id"] + ".json")
        )
        previous = artifact.read_bytes()
        self.cancelled()
        self.assertFalse(self.usage())
        self.assertTrue(self.storage.remove_profile("nas")["removed"])
        self.assertEqual(artifact.read_bytes(), previous)

    def test_cancelled_record_with_active_owner_or_rollback_still_reserves_destination(
        self,
    ):
        self.register()
        for mode in ("active", "rollback", "restart"):
            with self.subTest(mode=mode):
                self.cancelled(
                    can_rollback=mode == "rollback", restart_required=mode == "restart"
                )
                active = self.root / "installation-backups" / "active.json"
                if mode == "active":
                    self.recovery_record(
                        "installation-backups/active.json", {"job_id": "a" * 32}
                    )
                elif active.exists():
                    active.unlink()
                self.assertIn("installation_recovery", self.usage())
                self.assertNotIn("recovery_unavailable", self.usage())
                with self.assertRaisesRegex(BackupStorageError, "retained restore"):
                    self.storage.remove_profile("nas")
                self.assertIn("nas", self.config.config["backup_storage"]["profiles"])

    def test_cancelled_record_still_requires_valid_owned_destination_reference(self):
        self.register()
        self.cancelled(plan={})
        self.assertIn("recovery_unavailable", self.usage())
        with self.assertRaisesRegex(BackupStorageError, "recovery references"):
            self.storage.remove_profile("nas")

    def test_unknown_terminal_state_does_not_hide_a_retained_job(self):
        self.register()
        self.cancelled(status="unverified_cancellation")
        self.assertIn("recovery_unavailable", self.usage())
        with self.assertRaisesRegex(BackupStorageError, "recovery references"):
            self.storage.remove_profile("nas")


if __name__ == "__main__":
    unittest.main()
