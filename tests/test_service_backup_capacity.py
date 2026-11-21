"""Service dumps cannot consume space before stopped-state admission succeeds."""

from copy import deepcopy
from collections import namedtuple
import unittest
from unittest.mock import Mock, patch

from tests import test_service_backup as fixtures
from utils.service_backup import ServiceBackupOperations

GIB = 1024**3
MIB = 1024**2
DiskUsage = namedtuple("DiskUsage", "total used free")


class ServiceBackupCapacityTests(unittest.TestCase):
    join_worker = fixtures.ServiceBackupTests.join_worker

    def setUp(self):
        fixtures.ServiceBackupTests.setUp(self)
        self.specs = [{"database": "fixture_selected", "role": "main"}]
        self.adapter = Mock()
        self.adapter.estimate.return_value = GIB
        self.stack.enter_context(
            patch(
                "utils.service_backup.service_databases",
                side_effect=lambda config, key, instance_name=None: (
                    deepcopy(self.specs) if key == "fixture" else []
                ),
            )
        )
        self.stack.enter_context(
            patch(
                "utils.service_backup.PostgresServiceBackup",
                return_value=self.adapter,
            )
        )

    @staticmethod
    def request():
        return {
            "request_id": "9" * 32,
            "destination_id": "fixture",
            "scope": "services",
            "service_ids": ["fixture"],
            "confirmation": "BACK UP DUMB INSTALLATION",
            "acknowledgements": {
                "downtime": True,
                "secrets": True,
                "external_state": True,
            },
        }

    def assert_space_failure_preserves_running_set(self, job):
        self.assertEqual(job["status"], "failed", job)
        self.assertIn("space", " ".join(job["errors"]).lower())
        self.assertEqual(job["capacity"]["status"], "insufficient")
        self.adapter.backup.assert_not_called()
        self.assertEqual(self.handler.events, [("stop", "App"), ("start", "App")])
        self.assertIsNone(self.handler.process_names["App"].poll())
        self.assertIs(self.handler.process_names["Other App"], self.other_process)
        self.assertIs(self.handler.process_names["PostgreSQL"], self.postgres_process)
        self.assertIsNone(self.other_process.poll())
        self.assertIsNone(self.postgres_process.poll())
        self.assertFalse(self.manager.has_blocking_job())
        self.assertEqual(self.manager.catalog("fixture")["artifacts"], [])

    def test_direct_backup_rechecks_database_space_before_dump_or_archive_payload(self):
        # The file/archive-only footprint fits; the database export allowance does not.
        usage = DiskUsage(4 * GIB, 2 * GIB, 2 * GIB)
        with (
            patch("utils.service_backup.shutil.disk_usage", return_value=usage),
            patch.object(
                self.storage, "write_file", wraps=self.storage.write_file
            ) as write,
        ):
            admitted = self.manager.start_direct(self.request())
            self.join_worker()
        job = self.manager.get_job(admitted["job"]["job_id"])["job"]
        write.assert_not_called()
        self.assert_space_failure_preserves_running_set(job)

    def test_scheduled_backup_obeys_the_same_predump_capacity_boundary(self):
        self.manager.update_schedule(
            {
                "enabled": True,
                "destination_id": "fixture",
                "scope": "services",
                "service_ids": ["fixture"],
            }
        )
        self.manager._write(
            self.manager._state("schedule.json"),
            {"next_due_at": self.manager.clock() - 1},
        )
        usage = DiskUsage(4 * GIB, 2 * GIB, 2 * GIB)
        with (
            patch("utils.service_backup.shutil.disk_usage", return_value=usage),
            patch.object(
                self.storage, "write_file", wraps=self.storage.write_file
            ) as write,
        ):
            self.manager.tick()
            self.join_worker()
        job = self.manager.status()["job"]
        self.assertTrue(self.manager._job(job["job_id"])["scheduled"])
        write.assert_not_called()
        self.assert_space_failure_preserves_running_set(job)
        self.assertGreater(
            self.manager.status()["schedule_runtime"]["next_due_at"],
            self.manager.clock(),
        )

    def test_database_growth_after_classification_is_remeasured_after_pause(self):
        self.adapter.estimate.side_effect = [16 * MIB, GIB]
        usage = DiskUsage(4 * GIB, 2 * GIB, 2 * GIB)
        with patch("utils.service_backup.shutil.disk_usage", return_value=usage):
            admitted = self.manager.start_direct(self.request())
            self.join_worker()
        job = self.manager.get_job(admitted["job"]["job_id"])["job"]
        self.assertEqual(self.adapter.estimate.call_count, 2)
        self.assert_space_failure_preserves_running_set(job)

    def restore_plan(self, *, estimate=256 * MIB, dump_bytes=32 * MIB):
        # All disposable targets, including PostgreSQL, share the same free-space pool.
        return {
            "roots": [
                {
                    "id": "root_000",
                    "path": str(self.application),
                    "kind": "directory",
                }
            ],
            "capacity_proof": {
                "logical_bytes": 256 * MIB,
                "ledger_bytes": 4096,
                "entries": 1,
                "root_sizes": {"root_000": 256 * MIB},
            },
            "metadata": {
                "database_estimated_bytes": estimate,
                "service_databases": [
                    {"manifest": {"databases": [{"bytes": dump_bytes}]}}
                ],
            },
        }

    def test_restore_combines_file_staging_and_pg_needs_on_a_shared_free_pool(self):
        usage = DiskUsage(4 * GIB, 2 * GIB, 2 * GIB)
        plan = self.restore_plan()
        with patch("utils.service_backup.shutil.disk_usage", return_value=usage):
            file_report = self.manager._restore_capacity(
                plan["capacity_proof"], plan["roots"], self.manager.workspace_root
            )
            self.assertEqual(file_report["status"], "sufficient")
            report = ServiceBackupOperations(self.manager).restore_capacity(
                plan, self.manager.workspace_root
            )
        self.assertEqual(report["status"], "insufficient")
        self.assertTrue(
            any(row["shortfall_bytes"] > 0 for row in report["filesystems"])
        )

    def test_zero_header_estimate_cannot_hide_the_verified_dump_footprint(self):
        usage = DiskUsage(4 * GIB, 2 * GIB, 2 * GIB)
        plan = self.restore_plan(estimate=0, dump_bytes=512 * MIB)
        with patch("utils.service_backup.shutil.disk_usage", return_value=usage):
            report = ServiceBackupOperations(self.manager).restore_capacity(
                plan, self.manager.workspace_root
            )
        self.assertEqual(report["status"], "insufficient")
        self.assertTrue(
            any(row["shortfall_bytes"] > 0 for row in report["filesystems"])
        )


if __name__ == "__main__":
    unittest.main()
