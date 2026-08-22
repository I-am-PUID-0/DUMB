import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils import service_postgres


class InfiniDyskPostgresGuardTests(unittest.TestCase):
    def setUp(self):
        self.postgres = {
            "host": "127.0.0.1",
            "port": 5432,
            "user": "DUMB",
            "password": "secret",
        }
        self.physical_identity = {
            "system_identifier": "7418529630741852963",
            "database_oid": "16384",
        }
        self.physical_identity_patcher = patch.object(
            service_postgres,
            "infinidysk_postgres_physical_identity",
            return_value=self.physical_identity,
        )
        self.physical_identity_patcher.start()
        self.addCleanup(self.physical_identity_patcher.stop)

    def _service(self, config_path):
        return {
            "process_name": "InfiniDysk",
            "config_dir": str(config_path),
            "repo_owner": "infinidysk",
            "repo_name": "infinidysk",
            "postgres_enabled": True,
            "postgres_database": "infinidysk",
            "env": {
                "CONFIG_PATH": str(config_path),
                "DATABASE_PROVIDER": "postgres",
                "DATABASE_CONNECTION_STRING": (
                    service_postgres.postgres_npgsql_connection_string(
                        self.postgres, "infinidysk"
                    )
                ),
            },
        }

    def _completed_payload(self, config_path="/infinidysk"):
        source_schema = "source-schema-fingerprint"
        binding = {
            "process_name": "InfiniDysk",
            "service_key": "infinidysk",
            "instance_name": None,
            "service_version": "v1.2.0-8c960ffc",
            "source_schema_fingerprint": source_schema,
            "source_path_fingerprint": (
                service_postgres.infinidysk_sqlite_source_path_fingerprint(
                    Path(config_path) / "db.sqlite"
                )
            ),
            "launch_config_fingerprint": (
                service_postgres.infinidysk_launch_config_fingerprint(
                    self._service(config_path)
                )
            ),
            "source_migration_history_fingerprint": (
                service_postgres._INFINIDYSK_SQLITE_MIGRATION_HISTORY_FINGERPRINT
            ),
            "postgres_database": "infinidysk",
            "postgres_target_fingerprint": (
                service_postgres._postgres_target_fingerprint(
                    self.postgres, "infinidysk"
                )
            ),
        }
        main_import = {
            "validated": True,
            "tables": 23,
            "primary_key_digests_validated": 23,
            "full_row_digests_validated": 23,
            "foreign_keys_validated": 4,
            "sequences_validated": 2,
            "postgres_schema_fingerprint": (
                service_postgres._INFINIDYSK_POSTGRES_SCHEMA_FINGERPRINT
            ),
        }
        return {
            "job_id": "a" * 32,
            "rehearsal_job_id": "b" * 32,
            "process_name": "InfiniDysk",
            "service_key": "infinidysk",
            "mode": "cutover",
            "status": "completed",
            "binding": binding,
            "result": {
                "mode": "cutover",
                "validated": True,
                "binding": binding,
                "adapter_schema": (
                    service_postgres._INFINIDYSK_POSTGRES_ADAPTER_SCHEMA
                ),
                "postgres_schema_fingerprint": (
                    service_postgres._INFINIDYSK_POSTGRES_SCHEMA_FINGERPRINT
                ),
                "rehearsal_job_id": "b" * 32,
                "application_health_verified": True,
                "imports": {"main": main_import},
                "cutover_performed": True,
                "postgres_databases": ["infinidysk"],
                "postgres_physical_identity": dict(self.physical_identity),
            },
        }

    def test_environment_alone_cannot_bypass_existing_sqlite_guard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "db.sqlite").write_bytes(b"existing sqlite")
            safe, error = service_postgres.validate_infinidysk_postgres_fresh_install(
                temp_dir,
                True,
                service=self._service(temp_dir),
                postgres_config=self.postgres,
                migration_root=Path(temp_dir) / "migration",
            )

        self.assertFalse(safe)
        self.assertIn("guarded SQLite-to-PostgreSQL migration", error)

    def test_source_fingerprint_ignores_only_sqlite_statistics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "db.sqlite"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    'CREATE TABLE "__EFMigrationsHistory" ('
                    '"MigrationId" TEXT NOT NULL PRIMARY KEY, '
                    '"ProductVersion" TEXT NOT NULL)'
                )
                connection.execute(
                    'INSERT INTO "__EFMigrationsHistory" VALUES (?, ?)',
                    ("migration-1", "10.0.0"),
                )
                connection.execute(
                    "CREATE TABLE Items ("
                    "Id INTEGER PRIMARY KEY, Name TEXT NOT NULL UNIQUE)"
                )
                connection.commit()

                before_analyze = service_postgres._sqlite_source_fingerprints(database)
                expected_rows = connection.execute(
                    "SELECT type, name, tbl_name, COALESCE(sql, '') "
                    "FROM sqlite_master WHERE name <> '__EFMigrationsLock' "
                    "AND name NOT GLOB 'sqlite_stat*' "
                    "ORDER BY type, name, tbl_name"
                ).fetchall()
                self.assertTrue(
                    any(row[1].startswith("sqlite_autoindex_") for row in expected_rows)
                )
                expected_payload = json.dumps(
                    expected_rows, separators=(",", ":"), ensure_ascii=False
                )
                self.assertEqual(
                    before_analyze[0],
                    hashlib.sha256(expected_payload.encode("utf-8")).hexdigest(),
                )

                connection.execute("ANALYZE")
                connection.commit()
                self.assertEqual(
                    service_postgres._sqlite_source_fingerprints(database),
                    before_analyze,
                )

                connection.execute("CREATE TABLE FutureItems (Id INTEGER PRIMARY KEY)")
                connection.commit()
                self.assertNotEqual(
                    service_postgres._sqlite_source_fingerprints(database)[0],
                    before_analyze[0],
                )
            finally:
                connection.close()

    def test_nonempty_shm_sidecar_is_initialized_and_refused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "db.sqlite-shm").write_bytes(b"sqlite shared memory")
            safe, _ = service_postgres.validate_infinidysk_postgres_fresh_install(
                temp_dir,
                True,
                service=self._service(temp_dir),
                postgres_config=self.postgres,
                migration_root=Path(temp_dir) / "migration",
            )

        self.assertFalse(safe)

    def test_fresh_postgres_install_cannot_disable_into_empty_sqlite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            safe, error = service_postgres.validate_infinidysk_postgres_fresh_install(
                temp_dir,
                False,
                service=self._service(temp_dir),
                postgres_config=self.postgres,
                migration_root=Path(temp_dir) / "migration",
            )

        self.assertFalse(safe)
        self.assertIn("cannot switch from PostgreSQL", error)

    def test_symlink_and_hardlink_sources_are_refused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.sqlite"
            target.write_bytes(b"sqlite")
            (root / "db.sqlite").symlink_to(target)
            safe, _ = service_postgres.validate_infinidysk_postgres_fresh_install(
                temp_dir,
                True,
                service=self._service(temp_dir),
                postgres_config=self.postgres,
                migration_root=root / "migration",
            )
            self.assertFalse(safe)
            (root / "db.sqlite").unlink()
            os.link(target, root / "db.sqlite")
            safe, _ = service_postgres.validate_infinidysk_postgres_fresh_install(
                temp_dir,
                True,
                service=self._service(temp_dir),
                postgres_config=self.postgres,
                migration_root=root / "migration",
            )
            self.assertFalse(safe)

    def test_private_completed_marker_authorizes_only_matching_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = self._completed_payload(temp_dir)
            source_fingerprints = (
                payload["binding"]["source_schema_fingerprint"],
                payload["binding"]["source_migration_history_fingerprint"],
            )
            root = Path(temp_dir) / "migration"
            service_postgres.record_infinidysk_postgres_migration_completion(
                payload, migration_root=root
            )
            marker = root / "authorizations" / "infinidysk.json"
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(marker.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
            stored = json.loads(marker.read_text(encoding="utf-8"))
            self.assertNotIn("secret", json.dumps(stored))
            self.assertEqual(
                stored["minimum_runtime_commit"],
                service_postgres._INFINIDYSK_POSTGRES_BASELINE_COMMIT,
            )

            service = self._service(temp_dir)
            with patch.object(
                service_postgres,
                "_sqlite_source_fingerprints",
                return_value=source_fingerprints,
            ):
                self.assertTrue(
                    service_postgres.infinidysk_postgres_runtime_configured(
                        service, self.postgres, migration_root=root
                    )
                )
                service["command"] = ["/tmp/untrusted-infinidysk"]
                self.assertFalse(
                    service_postgres.infinidysk_postgres_runtime_configured(
                        service, self.postgres, migration_root=root
                    )
                )
                service.pop("command")
                changed_target = {**self.postgres, "port": 5433}
                self.assertFalse(
                    service_postgres.infinidysk_postgres_runtime_configured(
                        service, changed_target, migration_root=root
                    )
                )
                service["process_name"] = "Different InfiniDysk"
                self.assertFalse(
                    service_postgres.infinidysk_postgres_runtime_configured(
                        service, self.postgres, migration_root=root
                    )
                )

                service["process_name"] = "InfiniDysk"
                service["env"]["DATABASE_CONNECTION_STRING"] = (
                    'Host="example.invalid";Port="5432";Database="infinidysk";'
                    'Username="DUMB";Password="forged"'
                )
                self.assertFalse(
                    service_postgres.infinidysk_postgres_runtime_configured(
                        service, self.postgres, migration_root=root
                    )
                )

            self.assertTrue(
                service_postgres.clear_infinidysk_postgres_migration_completion(
                    migration_root=root
                )
            )
            self.assertFalse(marker.exists())

    def test_completion_without_verified_application_health_is_rejected(self):
        payload = self._completed_payload()
        payload["result"]["application_health_verified"] = False
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(RuntimeError, "authorization contract"):
                service_postgres.record_infinidysk_postgres_migration_completion(
                    payload, migration_root=Path(temp_dir) / "migration"
                )

    def test_password_rotation_preserves_same_target_authorization(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = self._completed_payload(temp_dir)
            source_fingerprints = (
                payload["binding"]["source_schema_fingerprint"],
                payload["binding"]["source_migration_history_fingerprint"],
            )
            root = Path(temp_dir) / "migration"
            service_postgres.record_infinidysk_postgres_migration_completion(
                payload, migration_root=root
            )
            service = self._service(temp_dir)
            rotated_postgres = {**self.postgres, "password": "rotated-secret"}
            with patch.object(
                service_postgres,
                "_sqlite_source_fingerprints",
                return_value=source_fingerprints,
            ):
                self.assertTrue(
                    service_postgres.infinidysk_postgres_runtime_configured(
                        service, rotated_postgres, migration_root=root
                    )
                )

    def test_replacement_cluster_or_database_invalidates_authorization(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = self._completed_payload(temp_dir)
            source_fingerprints = (
                payload["binding"]["source_schema_fingerprint"],
                payload["binding"]["source_migration_history_fingerprint"],
            )
            root = Path(temp_dir) / "migration"
            service_postgres.record_infinidysk_postgres_migration_completion(
                payload, migration_root=root
            )
            service = self._service(temp_dir)
            replacements = (
                {**self.physical_identity, "system_identifier": "9999999999999999999"},
                {**self.physical_identity, "database_oid": "32768"},
            )
            for replacement in replacements:
                with (
                    self.subTest(replacement=replacement),
                    patch.object(
                        service_postgres,
                        "_sqlite_source_fingerprints",
                        return_value=source_fingerprints,
                    ),
                    patch.object(
                        service_postgres,
                        "infinidysk_postgres_physical_identity",
                        return_value=replacement,
                    ),
                ):
                    self.assertFalse(
                        service_postgres.infinidysk_postgres_runtime_configured(
                            service, self.postgres, migration_root=root
                        )
                    )

    def test_install_only_can_use_static_authorization_while_runtime_requires_live_target(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = self._completed_payload(temp_dir)
            source_fingerprints = (
                payload["binding"]["source_schema_fingerprint"],
                payload["binding"]["source_migration_history_fingerprint"],
            )
            root = Path(temp_dir) / "migration"
            Path(temp_dir, "db.sqlite").write_bytes(b"preserved rollback source")
            service_postgres.record_infinidysk_postgres_migration_completion(
                payload, migration_root=root
            )
            service = self._service(temp_dir)
            with (
                patch.object(
                    service_postgres,
                    "_sqlite_source_fingerprints",
                    return_value=source_fingerprints,
                ),
                patch.object(
                    service_postgres,
                    "infinidysk_postgres_physical_identity",
                    return_value=None,
                ),
            ):
                live_safe, _ = (
                    service_postgres.validate_infinidysk_postgres_fresh_install(
                        temp_dir,
                        True,
                        service=service,
                        postgres_config=self.postgres,
                        migration_root=root,
                    )
                )
                install_safe, install_error = (
                    service_postgres.validate_infinidysk_postgres_fresh_install(
                        temp_dir,
                        True,
                        service=service,
                        postgres_config=self.postgres,
                        migration_root=root,
                        allow_offline_authorization=True,
                    )
                )

            self.assertFalse(live_safe)
            self.assertTrue(install_safe)
            self.assertIsNone(install_error)

    def test_completed_job_is_safe_fallback_when_marker_is_absent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = self._completed_payload(temp_dir)
            source_fingerprints = (
                payload["binding"]["source_schema_fingerprint"],
                payload["binding"]["source_migration_history_fingerprint"],
            )
            root = Path(temp_dir) / "migration"
            jobs = root / "jobs"
            jobs.mkdir(parents=True, mode=0o700)
            root.chmod(0o700)
            jobs.chmod(0o700)
            job_path = jobs / f"{payload['job_id']}.json"
            job_path.write_text(json.dumps(payload), encoding="utf-8")
            job_path.chmod(0o600)
            with patch.object(
                service_postgres,
                "_sqlite_source_fingerprints",
                return_value=source_fingerprints,
            ):
                self.assertTrue(
                    service_postgres.infinidysk_postgres_runtime_configured(
                        self._service(temp_dir),
                        self.postgres,
                        migration_root=root,
                    )
                )

    def test_direct_postgres_to_sqlite_reversal_requires_explicit_rollback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = self._completed_payload(temp_dir)
            source_fingerprints = (
                payload["binding"]["source_schema_fingerprint"],
                payload["binding"]["source_migration_history_fingerprint"],
            )
            Path(temp_dir, "db.sqlite").write_bytes(b"preserved rollback source")
            root = Path(temp_dir) / "migration"
            service_postgres.record_infinidysk_postgres_migration_completion(
                payload, migration_root=root
            )
            with patch.object(
                service_postgres,
                "_sqlite_source_fingerprints",
                return_value=source_fingerprints,
            ):
                safe, error = (
                    service_postgres.validate_infinidysk_postgres_fresh_install(
                        temp_dir,
                        False,
                        service=self._service(temp_dir),
                        postgres_config=self.postgres,
                        migration_root=root,
                    )
                )
            self.assertFalse(safe)
            self.assertIn("migration rollback action", error)

    def test_migration_context_is_cleared_after_failure(self):
        self.assertFalse(service_postgres.infinidysk_postgres_migration_authorized())
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            with service_postgres.authorize_infinidysk_postgres_migration():
                self.assertTrue(
                    service_postgres.infinidysk_postgres_migration_authorized()
                )
                raise RuntimeError("synthetic")
        self.assertFalse(service_postgres.infinidysk_postgres_migration_authorized())

    def test_unsafe_evidence_owner_or_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = self._completed_payload(temp_dir)
            source_fingerprints = (
                payload["binding"]["source_schema_fingerprint"],
                payload["binding"]["source_migration_history_fingerprint"],
            )
            root = Path(temp_dir) / "migration"
            service_postgres.record_infinidysk_postgres_migration_completion(
                payload, migration_root=root
            )
            root.chmod(0o755)
            with patch.object(
                service_postgres,
                "_sqlite_source_fingerprints",
                return_value=source_fingerprints,
            ):
                self.assertFalse(
                    service_postgres.infinidysk_postgres_runtime_configured(
                        self._service(temp_dir), self.postgres, migration_root=root
                    )
                )

            root.chmod(0o700)
            with (
                patch.object(
                    service_postgres,
                    "_sqlite_source_fingerprints",
                    return_value=source_fingerprints,
                ),
                patch.object(
                    service_postgres.os,
                    "geteuid",
                    return_value=os.geteuid() + 1,
                ),
            ):
                self.assertFalse(
                    service_postgres.infinidysk_postgres_runtime_configured(
                        self._service(temp_dir), self.postgres, migration_root=root
                    )
                )

    def test_clear_refuses_symlinked_authorization_parent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "migration"
            outside = base / "outside"
            root.mkdir(mode=0o700)
            outside.mkdir(mode=0o700)
            marker = outside / "infinidysk.json"
            marker.write_text("{}", encoding="utf-8")
            marker.chmod(0o600)
            (root / "authorizations").symlink_to(outside, target_is_directory=True)

            self.assertFalse(
                service_postgres.clear_infinidysk_postgres_migration_completion(
                    migration_root=root
                )
            )

            self.assertTrue(marker.exists())

    def test_clear_proves_missing_authorization_is_absent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "migration"
            self.assertTrue(
                service_postgres.clear_infinidysk_postgres_migration_completion(
                    migration_root=root
                )
            )

            root.mkdir(mode=0o700)
            self.assertTrue(
                service_postgres.clear_infinidysk_postgres_migration_completion(
                    migration_root=root
                )
            )

            root.chmod(0o755)
            self.assertFalse(
                service_postgres.clear_infinidysk_postgres_migration_completion(
                    migration_root=root
                )
            )

    def test_clear_refuses_unsafe_authorization_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "migration"
            authorization_dir = root / "authorizations"
            authorization_dir.mkdir(parents=True, mode=0o700)
            root.chmod(0o700)
            authorization_dir.chmod(0o700)
            marker = authorization_dir / "infinidysk.json"
            marker.write_text("{}", encoding="utf-8")
            marker.chmod(0o644)

            self.assertFalse(
                service_postgres.clear_infinidysk_postgres_migration_completion(
                    migration_root=root
                )
            )
            self.assertTrue(marker.exists())

    def test_clear_reports_uninspectable_storage_as_failure(self):
        with patch.object(Path, "lstat", side_effect=PermissionError("denied")):
            self.assertFalse(
                service_postgres.clear_infinidysk_postgres_migration_completion(
                    migration_root="/config/arr-postgres-migration"
                )
            )

    def test_completed_payload_must_cross_check_rehearsal_job_id(self):
        payload = self._completed_payload()
        payload["rehearsal_job_id"] = "c" * 32
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(RuntimeError, "authorization contract"):
                service_postgres.record_infinidysk_postgres_migration_completion(
                    payload, migration_root=Path(temp_dir) / "migration"
                )

    def test_candidate_rejects_existing_sqlite_enable_and_allows_stuck_flag_clear(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "db.sqlite").write_bytes(b"existing sqlite")
            current = self._service(temp_dir)
            current["postgres_enabled"] = False
            current["env"] = {"CONFIG_PATH": temp_dir, "DATABASE_PROVIDER": "sqlite"}
            candidate = {**current, "postgres_enabled": True}

            safe, error = (
                service_postgres.validate_infinidysk_postgres_candidate_update(
                    current,
                    candidate,
                    self.postgres,
                    migration_root=Path(temp_dir) / "migration",
                )
            )
            self.assertFalse(safe)
            self.assertIn("guarded SQLite-to-PostgreSQL migration", error)

            stuck = {**current, "postgres_enabled": True}
            safe, error = (
                service_postgres.validate_infinidysk_postgres_candidate_update(
                    stuck,
                    {**stuck, "postgres_enabled": False},
                    self.postgres,
                    migration_root=Path(temp_dir) / "migration",
                )
            )
            self.assertTrue(safe)
            self.assertIsNone(error)

    def test_candidate_checks_current_sqlite_before_new_config_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current_path = root / "current"
            candidate_path = root / "candidate"
            current_path.mkdir()
            candidate_path.mkdir()
            (current_path / "db.sqlite").write_bytes(b"existing sqlite")
            current = self._service(current_path)
            current["postgres_enabled"] = False
            current["env"] = {
                "CONFIG_PATH": str(current_path),
                "DATABASE_PROVIDER": "sqlite",
            }
            candidate = {
                **current,
                "config_dir": str(candidate_path),
                "postgres_enabled": True,
                "env": {
                    **current["env"],
                    "CONFIG_PATH": str(candidate_path),
                },
            }

            safe, error = (
                service_postgres.validate_infinidysk_postgres_candidate_update(
                    current,
                    candidate,
                    self.postgres,
                    migration_root=root / "migration",
                )
            )

            self.assertFalse(safe)
            self.assertIn(str(current_path / "db.sqlite"), error)
            self.assertIn("guarded SQLite-to-PostgreSQL migration", error)

    def test_candidate_rejects_direct_managed_provider_environment_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            current = self._service(temp_dir)
            current["postgres_enabled"] = False
            current["env"] = {
                "CONFIG_PATH": temp_dir,
                "DATABASE_PROVIDER": "sqlite",
            }
            candidate = {
                **current,
                "env": {
                    **current["env"],
                    "DATABASE_PROVIDER": "postgres",
                    "DATABASE_CONNECTION_STRING": (
                        service_postgres.postgres_npgsql_connection_string(
                            self.postgres, "infinidysk"
                        )
                    ),
                },
            }

            safe, error = (
                service_postgres.validate_infinidysk_postgres_candidate_update(
                    current,
                    candidate,
                    self.postgres,
                    migration_root=Path(temp_dir) / "migration",
                )
            )

        self.assertFalse(safe)
        self.assertIn("DATABASE_PROVIDER", error)
        self.assertIn("DUMB-managed", error)

    def test_candidate_binding_rejects_source_and_cluster_changes_but_allows_password(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = self._completed_payload(temp_dir)
            source_fingerprints = (
                payload["binding"]["source_schema_fingerprint"],
                payload["binding"]["source_migration_history_fingerprint"],
            )
            root = Path(temp_dir) / "migration"
            service_postgres.record_infinidysk_postgres_migration_completion(
                payload, migration_root=root
            )
            current = self._service(temp_dir)
            with patch.object(
                service_postgres,
                "_sqlite_source_fingerprints",
                return_value=source_fingerprints,
            ):
                changed_path = self._service(Path(temp_dir) / "other")
                safe, _ = (
                    service_postgres.validate_infinidysk_postgres_candidate_update(
                        current,
                        changed_path,
                        self.postgres,
                        migration_root=root,
                    )
                )
                self.assertFalse(safe)

                changed_cluster = {**self.postgres, "config_dir": "/other-cluster"}
                safe, _ = (
                    service_postgres.validate_infinidysk_postgres_candidate_update(
                        current,
                        current,
                        self.postgres,
                        changed_cluster,
                        migration_root=root,
                    )
                )
                self.assertFalse(safe)

                rotated = {**self.postgres, "password": "rotated-secret"}
                safe, error = (
                    service_postgres.validate_infinidysk_postgres_candidate_update(
                        current,
                        current,
                        self.postgres,
                        rotated,
                        migration_root=root,
                    )
                )
                self.assertTrue(safe)
                self.assertIsNone(error)

    def test_offline_postgres_provider_still_blocks_binding_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            current = self._service(temp_dir)
            candidates = (
                (
                    self._service(Path(temp_dir) / "other"),
                    self.postgres,
                    "source",
                ),
                (
                    current,
                    {**self.postgres, "port": 5544},
                    "target",
                ),
                (
                    {**current, "postgres_database": "other_database"},
                    self.postgres,
                    "database",
                ),
                (
                    current,
                    {**self.postgres, "config_dir": "/other-cluster"},
                    "cluster",
                ),
            )
            with patch.object(
                service_postgres,
                "infinidysk_postgres_physical_identity",
                return_value=None,
            ):
                for candidate, candidate_postgres, label in candidates:
                    with self.subTest(label=label):
                        safe, error = (
                            service_postgres.validate_infinidysk_postgres_candidate_update(
                                current,
                                candidate,
                                self.postgres,
                                candidate_postgres,
                                migration_root=Path(temp_dir) / "migration",
                            )
                        )
                        self.assertFalse(safe)
                        self.assertIn("persisted PostgreSQL provider", error)

                rotated = {**self.postgres, "password": "rotated-secret"}
                safe, error = (
                    service_postgres.validate_infinidysk_postgres_candidate_update(
                        current,
                        current,
                        self.postgres,
                        rotated,
                        migration_root=Path(temp_dir) / "migration",
                    )
                )
                self.assertTrue(safe)
                self.assertIsNone(error)

    def test_postgres_source_selection_requires_a_proven_v12_runtime(self):
        service = self._service("/infinidysk")
        cases = (
            ({"release_version_enabled": True, "release_version": "v1.1.9"}, False),
            ({"release_version_enabled": True, "release_version": "v1.2.0"}, True),
            ({"release_version_enabled": True, "release_version": "v1.3.0"}, True),
            ({"release_version_enabled": False, "release_version": "v1.1.0"}, True),
            ({"branch_enabled": True, "branch": "main"}, False),
            ({"release_version_enabled": True, "release_version": "rc"}, False),
            (
                {"commit_sha": service_postgres._INFINIDYSK_POSTGRES_BASELINE_COMMIT},
                True,
            ),
            ({"commit_sha": "f" * 40}, False),
        )
        for update, expected_safe in cases:
            with self.subTest(update=update):
                candidate = {**service, **update}
                safe, error = (
                    service_postgres.validate_infinidysk_postgres_source_selection(
                        candidate
                    )
                )
                self.assertEqual(expected_safe, safe)
                if expected_safe:
                    self.assertIsNone(error)
                else:
                    self.assertIn("v1.2.0-or-newer", error)

    def test_post_cutover_branch_and_release_targets_use_commit_ancestry(self):
        minimum = "1" * 40
        newer = "2" * 40
        older = "0" * 40

        def downloader_for(target, status):
            downloader = Mock()
            downloader.get_ref_commit_sha.return_value = (target, None)
            downloader.get_headers.return_value = {}
            response = Mock(status_code=200)
            response.json.return_value = {"status": status}
            downloader.fetch_with_retries.return_value = response
            return downloader

        branch = {
            **self._service("/infinidysk"),
            "branch_enabled": True,
            "branch": "main",
        }
        safe, error = service_postgres.validate_infinidysk_postgres_source_selection(
            branch,
            minimum_commit=minimum,
            downloader=downloader_for(newer, "ahead"),
        )
        self.assertTrue(safe)
        self.assertIsNone(error)

        release = {
            **self._service("/infinidysk"),
            "release_version_enabled": True,
            "release_version": "v1.2.0",
        }
        safe, error = service_postgres.validate_infinidysk_postgres_source_selection(
            release,
            minimum_commit=minimum,
            downloader=downloader_for(older, "behind"),
        )
        self.assertFalse(safe)
        self.assertIn("target is behind", error)

    def test_post_cutover_exact_commit_accepts_the_recorded_floor(self):
        minimum = "1" * 40
        downloader = Mock()
        service = {
            **self._service("/infinidysk"),
            "commit_sha": minimum,
        }

        safe, error = service_postgres.validate_infinidysk_postgres_source_selection(
            service,
            minimum_commit=minimum,
            downloader=downloader,
        )

        self.assertTrue(safe)
        self.assertIsNone(error)
        downloader.fetch_with_retries.assert_not_called()

    def test_installed_branch_runtime_is_checked_against_cutover_commit(self):
        minimum = "1" * 40
        installed = "2" * 40
        downloader = Mock()
        downloader.get_headers.return_value = {}
        response = Mock(status_code=200)
        response.json.return_value = {"status": "ahead"}
        downloader.fetch_with_retries.return_value = response
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "version.txt").write_text(
                f"main-{installed[:8]}", encoding="utf-8"
            )
            state = Path(temp_dir, ".dumb_infinidysk_install.json")
            state.write_text(
                json.dumps({"format": 1, "source_commit": installed}),
                encoding="utf-8",
            )
            state.chmod(0o600)

            safe, error = (
                service_postgres.validate_infinidysk_postgres_installed_version(
                    temp_dir,
                    minimum_commit=minimum,
                    downloader=downloader,
                )
            )

        self.assertTrue(safe)
        self.assertIsNone(error)

    def test_postgres_release_selection_requires_the_official_repository(self):
        service = {
            **self._service("/infinidysk"),
            "repo_owner": "example",
            "repo_name": "fork",
            "release_version_enabled": True,
            "release_version": "v1.2.0",
        }

        safe, error = service_postgres.validate_infinidysk_postgres_source_selection(
            service
        )

        self.assertFalse(safe)
        self.assertIn("official infinidysk/infinidysk", error)

    def test_candidate_rejects_postgres_runtime_downgrade_before_save(self):
        current = self._service("/infinidysk")
        candidate = {
            **current,
            "release_version_enabled": True,
            "release_version": "v1.1.0",
        }

        safe, error = service_postgres.validate_infinidysk_postgres_candidate_update(
            current,
            candidate,
            self.postgres,
            migration_root="/config/arr-postgres-migration",
        )

        self.assertFalse(safe)
        self.assertIn("v1.2.0-or-newer", error)

    def test_installed_postgres_runtime_marker_enforces_v12_floor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir, "version.txt")
            for value, expected_safe in (
                ("v1.1.9-deadbeef", False),
                ("v1.2.0-8c960ffc", True),
                ("v1.3.0", True),
                (
                    "commit-"
                    + service_postgres._INFINIDYSK_POSTGRES_BASELINE_COMMIT[:12],
                    True,
                ),
                ("commit-ffffffffffff", False),
                ("rc-deadbeef", False),
            ):
                with self.subTest(value=value):
                    marker.write_text(value, encoding="utf-8")
                    safe, error = (
                        service_postgres.validate_infinidysk_postgres_installed_version(
                            temp_dir
                        )
                    )
                    self.assertEqual(expected_safe, safe)
                    if expected_safe:
                        self.assertIsNone(error)
                    else:
                        self.assertIn("v1.2.0-or-newer", error)

    def test_postgres_release_validation_rejects_prerelease_and_marker_tags(self):
        for value, expected_safe in (
            ("v1.1.9", False),
            ("v1.2.0", True),
            ("1.2.1", True),
            ("v1.3.0-rc.1", False),
            ("v1.2.0-8c960ffc", False),
            ("rc", False),
        ):
            with self.subTest(value=value):
                safe, error = (
                    service_postgres.validate_infinidysk_postgres_release_version(value)
                )
                self.assertEqual(expected_safe, safe)
                if expected_safe:
                    self.assertIsNone(error)
                else:
                    self.assertIn("v1.2.0-or-newer", error)

    def test_inspection_error_still_returns_a_two_item_result(self):
        with patch.object(Path, "lstat", side_effect=OSError("denied")):
            result = service_postgres.validate_infinidysk_postgres_fresh_install(
                "/infinidysk",
                True,
                service=self._service("/infinidysk"),
                postgres_config=self.postgres,
            )
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
