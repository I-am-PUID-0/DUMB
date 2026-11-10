"""Setup and database migration share one lifecycle admission boundary."""

import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import MagicMock, patch

from utils.arr_postgres_migration import (
    ArrPostgresMigrationError,
    ArrPostgresMigrationManager,
)


class SetupPostgresAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = ArrPostgresMigrationManager(self.temp.name)

    def test_every_service_job_rejects_reserved_setup_before_preflight_or_job_write(
        self,
    ):
        for family, instance in (
            ("sonarr", "TV"),
            ("radarr", "Movies"),
            ("infinidysk", None),
        ):
            with self.subTest(family=family):
                config = MagicMock()
                config.find_key_for_process.return_value = (family, instance)
                with (
                    patch(
                        "utils.infinidysk_migration.INFINIDYSK_MIGRATION_MANAGER"
                    ) as namespace,
                    patch(
                        "utils.arr_postgres_migration.infinidysk_external_mutation_active",
                        return_value=True,
                    ),
                    patch.object(self.manager, "_create_job_admitted") as admitted,
                ):
                    namespace.has_blocking_job.return_value = False
                    with self.assertRaisesRegex(
                        ArrPostgresMigrationError, "Another DUMB operation"
                    ):
                        self.manager.create_job(
                            config,
                            MagicMock(),
                            MagicMock(),
                            MagicMock(),
                            "Fixture service",
                            "rehearsal",
                            False,
                            "MIGRATE Fixture service",
                            True,
                            True,
                            True,
                        )
                    admitted.assert_not_called()
                self.assertFalse(self.manager.jobs_dir.exists())

    def test_all_rollbacks_reject_reserved_setup_before_native_or_job_mutation(self):
        for family in ("sonarr", "radarr", "infinidysk"):
            with (
                self.subTest(family=family),
                patch.object(
                    self.manager, "get_job", return_value={"service_key": family}
                ),
                patch(
                    "utils.arr_postgres_migration.infinidysk_external_mutation_active",
                    return_value=True,
                ),
                patch.object(self.manager, "_reserve_rollback") as reserve,
            ):
                with self.assertRaisesRegex(
                    ArrPostgresMigrationError, "Another DUMB operation"
                ):
                    self.manager.rollback_job(
                        "a" * 32,
                        "ROLLBACK Fixture",
                        MagicMock(),
                        MagicMock(),
                        MagicMock(),
                    )
                reserve.assert_not_called()

    def test_arr_rollback_also_rejects_namespace_migration_before_reservation(self):
        with (
            patch.object(
                self.manager, "get_job", return_value={"service_key": "sonarr"}
            ),
            patch(
                "utils.arr_postgres_migration.infinidysk_external_mutation_active",
                return_value=False,
            ),
            patch(
                "utils.arr_postgres_migration.infinidysk_namespace_migration_active",
                return_value=True,
            ),
            patch.object(self.manager, "_reserve_rollback") as reserve,
        ):
            with self.assertRaisesRegex(
                ArrPostgresMigrationError, "namespace migration"
            ):
                self.manager.rollback_job(
                    "a" * 32, "ROLLBACK Fixture", MagicMock(), MagicMock(), MagicMock()
                )
            reserve.assert_not_called()

    def test_setup_guard_sees_all_services_active_and_recovery_states(self):
        payload = {
            "job_id": "a" * 32,
            "service_key": "sonarr",
            "process_name": "Fixture Sonarr",
            "worker_pid": os.getpid(),
            "worker_id": self.manager._worker_id,
        }
        self.manager._create_job(
            uuid.UUID(hex=payload["job_id"]), {**payload, "status": "queued"}
        )
        for status, rollback, expected in (
            ("queued", False, True),
            ("running", False, True),
            ("rolling_back", False, True),
            ("rollback_failed", False, True),
            ("failed", True, True),
            ("interrupted", True, True),
            ("failed", False, False),
            ("completed", True, False),
            ("rolled_back", False, False),
        ):
            with self.subTest(status=status):
                self.manager._save(
                    {**payload, "status": status, "rollback_available": rollback}
                )
                self.assertEqual(self.manager.has_setup_conflicting_job(), expected)
        self.manager._active_processes.add("Fixture Sonarr")
        self.assertTrue(self.manager.has_setup_conflicting_job())

    def test_unreadable_migration_evidence_blocks_setup_conservatively(self):
        payload = {"job_id": "a" * 32, "service_key": "sonarr", "status": "completed"}
        self.manager._create_job(uuid.UUID(hex=payload["job_id"]), payload)
        path = self.manager.jobs_dir / (payload["job_id"] + ".json")
        for raw in ("{broken", "[]"):
            with self.subTest(raw=raw):
                path.write_text(raw)
                self.assertTrue(self.manager.has_setup_conflicting_job())
