import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, Mock, call, patch

from utils.arr_postgres_migration import (
    ACTIVE_NAMESPACE_MIGRATION_BLOCKER,
    ArrPostgresMigrationError,
    ArrPostgresMigrationManager,
    INFINIDYSK_DATABASE_CONTRACTS,
    INFINIDYSK_POSTGRES_ADAPTER_SCHEMA,
    INFINIDYSK_POSTGRES_FOREIGN_KEY_LAYOUTS,
    INFINIDYSK_POSTGRES_SCHEMA_FINGERPRINT,
    INFINIDYSK_POSTGRES_TABLES,
    INFINIDYSK_SQLITE_MIGRATION_COUNT,
    INFINIDYSK_SQLITE_MIGRATION_HISTORY_SHA256,
    INFINIDYSK_SQLITE_SCHEMA_FINGERPRINT,
    INFINIDYSK_SQLITE_TERMINAL_MIGRATION,
    SUPPORTED_SERVICES,
    _backup_sqlite,
    _converted_import_batches,
    _convert_value,
    _digest_rows,
    _clear_infinidysk_rollback_authorization,
    _infinidysk_namespace_migration_resolved,
    _infinidysk_postgres_source_selection,
    _infinidysk_sqlite_contract_matches,
    _prepare_service_schema,
    _read_infinidysk_version_marker,
    _repair_altmount_postgres_migration_010,
    _reset_postgres_sequences,
    _sqlite_schema_fingerprint,
    _source_paths,
    _stop_infinidysk_if_running,
    _stop_tracked_infinidysk_process,
    _validate_full_row_digests,
    _validate_infinidysk_postgres_schema_connection,
    _wait_for_schema_helper,
    _wait_for_schema,
    build_arr_postgres_preflight,
)
from utils.infinidysk_migration import InfiniDyskMigrationManager


class StubConfig:
    def __init__(self, root):
        config_dir = str(Path(root) / "sonarr")
        self.file_path = str(Path(root) / "dumb_config.json")
        self.saved = []
        self.config = {
            "sonarr": {
                "instances": {
                    "TV": {
                        "enabled": True,
                        "postgres_enabled": False,
                        "process_name": "Sonarr TV",
                        "config_dir": config_dir,
                        "config_file": f"{config_dir}/config.xml",
                    }
                }
            },
            "postgres": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 5432,
                "user": "DUMB",
                "password": "secret-value",
                "databases": [],
            },
        }
        Path(self.file_path).write_text(json.dumps(self.config), encoding="utf-8")

    def find_key_for_process(self, process_name):
        if process_name == "Sonarr TV":
            return "sonarr", "TV"
        return None, None

    def get_instance(self, instance_name, key):
        return self.config[key]["instances"][instance_name]

    def get(self, key, default=None):
        return self.config.get(key, default)

    def save_config(self, process_name=None):
        self.saved.append(process_name)


class StubPulsarrConfig:
    def __init__(self, root):
        config_dir = str(Path(root) / "pulsarr")
        migration_dir = Path(config_dir) / "migrations"
        migration_dir.mkdir(parents=True)
        (migration_dir / "migrate.ts").write_text("", encoding="utf-8")
        self.file_path = str(Path(root) / "dumb_config.json")
        self.saved = []
        self.config = {
            "pulsarr": {
                "enabled": True,
                "postgres_enabled": False,
                "postgres_database": "",
                "process_name": "Pulsarr",
                "config_dir": config_dir,
                "env": {"dbType": "sqlite"},
            },
            "postgres": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 5432,
                "user": "DUMB",
                "password": "secret-value",
                "databases": [],
            },
        }
        Path(self.file_path).write_text(json.dumps(self.config), encoding="utf-8")

    def find_key_for_process(self, process_name):
        return ("pulsarr", None) if process_name == "Pulsarr" else (None, None)

    def get_instance(self, instance_name, key):
        del instance_name
        return self.config[key]

    def get(self, key, default=None):
        return self.config.get(key, default)

    def save_config(self, process_name=None):
        self.saved.append(process_name)


def create_sqlite(path, table="Series", rows=2):
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            f'CREATE TABLE "{table}" ("Id" INTEGER PRIMARY KEY, "Title" TEXT)'
        )
        connection.executemany(
            f'INSERT INTO "{table}" ("Id", "Title") VALUES (?, ?)',
            [(index, f"Item {index}") for index in range(1, rows + 1)],
        )
        connection.commit()
    finally:
        connection.close()


class ArrPostgresMigrationTests(unittest.TestCase):
    @staticmethod
    def persist_job(
        manager: ArrPostgresMigrationManager,
        payload: dict,
    ) -> None:
        payload.setdefault("worker_pid", os.getpid())
        payload.setdefault("worker_id", manager._worker_id)
        manager._create_job(uuid.UUID(hex=payload["job_id"]), payload)

    def test_same_pid_new_manager_interrupts_an_active_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = ArrPostgresMigrationManager(temp_dir)
            payload = {
                "job_id": "0" * 32,
                "process_name": "InfiniDysk",
                "service_key": "infinidysk",
                "status": "running",
                "rollback_available": True,
                "worker_pid": os.getpid(),
                "worker_id": first._worker_id,
                "events": [],
            }
            self.persist_job(first, payload)

            restarted = ArrPostgresMigrationManager(temp_dir)
            recovered = restarted.get_job(payload["job_id"])

            self.assertEqual(recovered["status"], "interrupted")
            self.assertEqual(recovered["worker_pid"], os.getpid())
            self.assertNotEqual(first._worker_id, restarted._worker_id)
            self.assertTrue(restarted.has_active_infinidysk_job())

    def test_interrupted_job_without_rollback_does_not_hold_admission(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ArrPostgresMigrationManager(temp_dir)
            self.persist_job(
                manager,
                {
                    "job_id": "1" * 32,
                    "process_name": "InfiniDysk",
                    "service_key": "infinidysk",
                    "status": "interrupted",
                    "rollback_available": False,
                    "events": [],
                },
            )

            self.assertFalse(manager.has_active_infinidysk_job())

    def test_finalizing_cutover_keeps_admission_and_rollback_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ArrPostgresMigrationManager(temp_dir)
            payload = {
                "job_id": "7" * 32,
                "process_name": "InfiniDysk",
                "service_key": "infinidysk",
                "instance_name": None,
                "mode": "cutover",
                "status": "finalizing",
                "rollback_available": True,
                "events": [],
            }
            self.persist_job(manager, payload)

            self.assertTrue(manager.has_active_infinidysk_job())
            with self.assertRaisesRegex(
                ArrPostgresMigrationError,
                "while the job is active",
            ):
                manager.rollback_job(
                    payload["job_id"],
                    "ROLLBACK InfiniDysk",
                    MagicMock(),
                    MagicMock(),
                    MagicMock(),
                )

    def test_recovery_pending_job_blocks_new_infini_job_before_preflight(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ArrPostgresMigrationManager(temp_dir)
            self.persist_job(
                manager,
                {
                    "job_id": "2" * 32,
                    "process_name": "InfiniDysk",
                    "service_key": "infinidysk",
                    "status": "interrupted",
                    "rollback_available": True,
                    "events": [],
                },
            )
            config_manager = MagicMock()
            config_manager.find_key_for_process.return_value = ("infinidysk", None)

            with (
                patch(
                    "utils.infinidysk_migration.INFINIDYSK_MIGRATION_MANAGER"
                ) as namespace_manager,
                patch(
                    "utils.arr_postgres_migration.build_arr_postgres_preflight"
                ) as preflight,
                self.assertRaisesRegex(
                    ArrPostgresMigrationError,
                    "unused guarded rollback",
                ),
            ):
                namespace_manager.has_blocking_job.return_value = False
                manager.create_job(
                    config_manager=config_manager,
                    process_handler=MagicMock(),
                    api_state=MagicMock(),
                    logger=MagicMock(),
                    process_name="InfiniDysk",
                    mode="rehearsal",
                    include_logs=False,
                    confirmation="MIGRATE InfiniDysk",
                    acknowledge_unsupported=True,
                    acknowledge_backup=True,
                    acknowledge_target_reset=True,
                )

            preflight.assert_not_called()

    def test_supported_inventory_includes_every_confirmed_dual_backend_service(self):
        self.assertEqual(
            set(SUPPORTED_SERVICES),
            {
                "altmount",
                "bazarr",
                "infinidysk",
                "lidarr",
                "prowlarr",
                "pulsarr",
                "radarr",
                "seerr",
                "sonarr",
                "whisparr",
            },
        )

    def test_infini_database_migration_requires_resolved_namespace_status(self):
        with patch(
            "utils.infinidysk_migration.INFINIDYSK_MIGRATION_MANAGER.status"
        ) as status:
            status.return_value = {"status": "pending"}
            self.assertEqual(
                _infinidysk_namespace_migration_resolved({}),
                (False, "pending"),
            )
            for resolved_status in (
                "not_needed",
                "compatibility_completed",
                "completed",
            ):
                with self.subTest(status=resolved_status):
                    status.return_value = {"status": resolved_status}
                    self.assertEqual(
                        _infinidysk_namespace_migration_resolved({}),
                        (True, resolved_status),
                    )

    def test_infini_preflight_rejects_a_selector_unsupported_after_cutover(self):
        safe, error = _infinidysk_postgres_source_selection(
            {
                "repo_owner": "infinidysk",
                "repo_name": "infinidysk",
                "branch_enabled": True,
                "branch": "main",
                "postgres_enabled": False,
                "env": {"DATABASE_PROVIDER": "sqlite"},
            }
        )

        self.assertFalse(safe)
        self.assertIn("v1.2.0-or-newer", error)

    def test_infini_preflight_reads_exact_marker_instead_of_lossy_version_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            logs = root / "logs"
            logs.mkdir()
            (logs / "infinidysk.txt").write_text(
                "Starting Infinidysk Version 1.2.0\n", encoding="utf-8"
            )
            (root / "version.txt").write_text("v1.2.0-8c960ffc\n", encoding="utf-8")

            self.assertEqual(
                _read_infinidysk_version_marker(root),
                "v1.2.0-8c960ffc",
            )

            (root / "version.txt").write_text("commit-8c960ffc39fc\n", encoding="utf-8")
            self.assertEqual(
                _read_infinidysk_version_marker(root),
                "commit-8c960ffc39fc",
            )

    def test_infini_contract_accepts_supported_schema_and_rejects_any_drift(self):
        expected_tables = set(INFINIDYSK_POSTGRES_TABLES) | {
            "__EFMigrationsHistory",
            "__EFMigrationsLock",
        }
        expected_foreign_keys = {
            (
                child_table,
                child_columns[0],
                parent_table,
                parent_columns[0],
                "CASCADE",
            )
            for child_table, child_columns, parent_table, parent_columns in (
                INFINIDYSK_POSTGRES_FOREIGN_KEY_LAYOUTS.values()
            )
        }
        for contract in INFINIDYSK_DATABASE_CONTRACTS:
            with self.subTest(contract=contract["id"]):
                contract_history = tuple(
                    [
                        f"migration-{index}"
                        for index in range(contract["sqlite_migration_count"] - 1)
                    ]
                    + [contract["sqlite_terminal_migration"]]
                )
                contract_details = {
                    "tables": tuple(sorted(expected_tables)),
                    "foreign_keys": tuple(sorted(expected_foreign_keys)),
                    "migration_history": contract_history,
                    "migration_history_fingerprint": contract[
                        "sqlite_migration_history_fingerprint"
                    ],
                }
                self.assertTrue(
                    _infinidysk_sqlite_contract_matches(
                        contract_details, contract["sqlite_schema_fingerprint"]
                    )
                )

        migration_history = tuple(
            [
                f"migration-{index}"
                for index in range(INFINIDYSK_SQLITE_MIGRATION_COUNT - 1)
            ]
            + [INFINIDYSK_SQLITE_TERMINAL_MIGRATION]
        )
        details = {
            "tables": tuple(sorted(expected_tables)),
            "foreign_keys": tuple(sorted(expected_foreign_keys)),
            "migration_history": migration_history,
            "migration_history_fingerprint": (
                INFINIDYSK_SQLITE_MIGRATION_HISTORY_SHA256
            ),
        }

        drift_cases = {
            "extra table": {**details, "tables": (*details["tables"], "FutureTable")},
            "missing table": {**details, "tables": details["tables"][:-1]},
            "extra migration": {
                **details,
                "migration_history": (*migration_history, "future-migration"),
            },
            "changed history": {
                **details,
                "migration_history_fingerprint": "0" * 64,
            },
        }
        for label, changed_details in drift_cases.items():
            with self.subTest(label=label):
                self.assertFalse(
                    _infinidysk_sqlite_contract_matches(
                        changed_details, INFINIDYSK_SQLITE_SCHEMA_FINGERPRINT
                    )
                )
        self.assertFalse(
            _infinidysk_sqlite_contract_matches(details, "f" * 64),
            "Column, index, or trigger changes must alter the audited schema fingerprint.",
        )

    def test_infini_schema_fingerprint_ignores_sqlite_internal_statistics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "db.sqlite"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE Items ("
                    "Id INTEGER PRIMARY KEY, Name TEXT NOT NULL UNIQUE)"
                )
                connection.execute("CREATE INDEX IX_Items_Name ON Items (Name)")
                connection.commit()
                before_analyze = _sqlite_schema_fingerprint(database)
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
                    before_analyze,
                    hashlib.sha256(expected_payload.encode("utf-8")).hexdigest(),
                    "Deterministic SQLite auto-indexes remain in the contract.",
                )

                connection.execute("ANALYZE")
                connection.commit()
                self.assertTrue(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'"
                    ).fetchone()
                )
                self.assertEqual(
                    _sqlite_schema_fingerprint(database),
                    before_analyze,
                    "SQLite-owned statistics must not look like application schema drift.",
                )

                connection.execute(
                    "CREATE TABLE TMP_LINKED_FILES (FileName TEXT PRIMARY KEY)"
                )
                connection.execute(
                    "CREATE INDEX TMP_LINKED_FILES_UNIQUE "
                    "ON TMP_LINKED_FILES (FileName)"
                )
                connection.commit()
                self.assertEqual(
                    _sqlite_schema_fingerprint(
                        database,
                        {"TMP_LINKED_FILES", "TMP_LINKED_FILES_UNIQUE"},
                    ),
                    before_analyze,
                    "InfiniDysk's reconstructable linked-file work table is not migrated.",
                )

                connection.execute("CREATE TABLE FutureItems (Id INTEGER PRIMARY KEY)")
                connection.commit()
                self.assertNotEqual(
                    _sqlite_schema_fingerprint(
                        database,
                        {"TMP_LINKED_FILES", "TMP_LINKED_FILES_UNIQUE"},
                    ),
                    before_analyze,
                    "Application-owned schema changes must still alter the fingerprint.",
                )
            finally:
                connection.close()

    def test_infini_migration_uses_minimum_version_not_an_exact_release_pin(self):
        self.assertEqual(SUPPORTED_SERVICES["infinidysk"]["minimum_version"], (1, 2, 0))
        self.assertNotIn("exact_version", SUPPORTED_SERVICES["infinidysk"])

    def test_latest_infini_contract_matches_v125_schema(self):
        latest = INFINIDYSK_DATABASE_CONTRACTS[-1]

        self.assertEqual("v1.2.5", latest["id"])
        self.assertEqual(51, latest["sqlite_migration_count"])
        self.assertEqual(
            "20260824143000_Add-Generated-Symlink-Metadata",
            latest["sqlite_terminal_migration"],
        )
        self.assertEqual(
            latest["sqlite_terminal_migration"],
            latest["postgres_migrations"][-1],
        )
        self.assertEqual(6, len(latest["postgres_migrations"]))
        self.assertEqual(
            "9bce3501afceee53f435834ad703e1083a2d4f51c44bd16b6bb217a8d4d9955b",
            latest["sqlite_migration_history_fingerprint"],
        )
        self.assertEqual(
            "42ade890f0f9394018630a3938c57d67acd0444b91acfaaca27289ed09fe80ae",
            latest["sqlite_schema_fingerprint"],
        )
        self.assertEqual(
            "f9a845c95f4e218a0c3f36ea7eb1e14972f63a2ad6391382e3615d4cd0601902",
            latest["postgres_schema_fingerprint"],
        )

    def test_infini_staged_postgres_contract_must_match_sqlite_source(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        v120_contract = next(
            contract
            for contract in INFINIDYSK_DATABASE_CONTRACTS
            if contract["id"] == "v1.2.0"
        )
        cursor.fetchall.side_effect = [
            [(migration,) for migration in v120_contract["postgres_migrations"]],
            [],
            [],
            [],
            [],
            [],
        ]

        with self.assertRaisesRegex(
            ArrPostgresMigrationError,
            "v1.2.3 source, v1.2.0 target",
        ):
            _validate_infinidysk_postgres_schema_connection(
                connection,
                expected_contract_id="v1.2.3",
            )

    def test_infini_rollback_fails_when_authorization_clear_is_unproven(self):
        with (
            patch(
                "utils.arr_postgres_migration.clear_infinidysk_postgres_migration_completion",
                return_value=False,
            ),
            self.assertRaisesRegex(
                ArrPostgresMigrationError,
                "could not safely clear",
            ),
        ):
            _clear_infinidysk_rollback_authorization(Path("/controlled/root"))

    def test_bazarr_paths_follow_config_root_and_resolve_current_config_layout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "bazarr-data"
            current_config = data_dir / "config" / "config.yaml"
            current_config.parent.mkdir(parents=True)
            current_config.write_text("general: {}\n", encoding="utf-8")

            paths = _source_paths(
                "bazarr",
                {
                    # Persisted configs may still contain Bazarr's former path.
                    "config_file": str(data_dir / "config.yaml"),
                    "command": ["python", "bazarr.py", "--config", str(data_dir)],
                },
            )

            self.assertEqual(paths["config_xml"], current_config)
            self.assertEqual(paths["main"], data_dir / "db" / "bazarr.db")

    def make_runtime(self, temp_dir):
        config = StubConfig(temp_dir)
        config_dir = Path(temp_dir) / "sonarr"
        config_dir.mkdir()
        (config_dir / "config.xml").write_text(
            "<Config><Port>8989</Port></Config>", encoding="utf-8"
        )
        create_sqlite(config_dir / "sonarr.db")
        create_sqlite(config_dir / "logs.db", table="Logs", rows=1)
        process_handler = Mock()
        process_handler.start_process.return_value = (True, None)
        api_state = SimpleNamespace(get_status=lambda _: "running")
        return config, process_handler, api_state

    def test_preflight_reports_sqlite_and_target_state_without_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = StubConfig(temp_dir)
            config_dir = Path(temp_dir) / "sonarr"
            config_dir.mkdir()
            (config_dir / "config.xml").write_text(
                "<Config><Port>8989</Port></Config>", encoding="utf-8"
            )
            create_sqlite(config_dir / "sonarr.db")
            create_sqlite(config_dir / "logs.db", table="Logs", rows=1)

            disk_usage = SimpleNamespace(total=10**12, used=0, free=10**12)
            with (
                patch(
                    "utils.arr_postgres_migration._postgres_role_summary",
                    return_value={"superuser": True, "createdb": True},
                ),
                patch(
                    "utils.arr_postgres_migration._postgres_database_summary",
                    side_effect=lambda _, name: {
                        "name": name,
                        "exists": False,
                        "table_count": 0,
                        "row_count": 0,
                    },
                ),
                patch(
                    "utils.arr_postgres_migration.shutil.disk_usage",
                    return_value=disk_usage,
                ),
            ):
                report = build_arr_postgres_preflight(
                    config,
                    "Sonarr TV",
                    api_state=SimpleNamespace(get_status=lambda _: "running"),
                    root=Path(temp_dir) / "migration",
                )

            self.assertTrue(report["ready"])
            self.assertEqual(report["service_key"], "sonarr")
            self.assertEqual(report["postgres"]["main_database"], "sonarr_tv_main")
            self.assertEqual(report["confirmation_text"], "MIGRATE Sonarr TV")
            self.assertNotIn("secret-value", json.dumps(report))

    def test_preflight_blocks_an_instance_already_using_postgres(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = StubConfig(temp_dir)
            config.config["sonarr"]["instances"]["TV"]["postgres_enabled"] = True
            config_dir = Path(temp_dir) / "sonarr"
            config_dir.mkdir()
            (config_dir / "config.xml").write_text("<Config />", encoding="utf-8")
            create_sqlite(config_dir / "sonarr.db")
            with (
                patch(
                    "utils.arr_postgres_migration._postgres_role_summary",
                    return_value={"superuser": True, "createdb": True},
                ),
                patch(
                    "utils.arr_postgres_migration._postgres_database_summary",
                    return_value={
                        "name": "target",
                        "exists": False,
                        "table_count": 0,
                        "row_count": 0,
                    },
                ),
                patch(
                    "utils.arr_postgres_migration.shutil.disk_usage",
                    return_value=SimpleNamespace(total=10**12, used=0, free=10**12),
                ),
            ):
                report = build_arr_postgres_preflight(
                    config, "Sonarr TV", root=Path(temp_dir) / "migration"
                )
            self.assertFalse(report["ready"])
            self.assertEqual(
                next(item for item in report["checks"] if item["id"] == "sqlite_mode")[
                    "status"
                ],
                "fail",
            )

    def test_sqlite_backup_is_consistent_and_reports_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.db"
            destination = Path(temp_dir) / "backup" / "source.db"
            create_sqlite(source, rows=25)
            progress = []

            _backup_sqlite(
                source,
                destination,
                lambda done, total: progress.append((done, total)),
            )

            connection = sqlite3.connect(destination)
            try:
                count = connection.execute('SELECT COUNT(*) FROM "Series"').fetchone()[
                    0
                ]
            finally:
                connection.close()
            self.assertEqual(count, 25)
            self.assertTrue(progress)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_sqlite_backup_failure_removes_incomplete_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.db"
            destination = Path(temp_dir) / "backup" / "source.db"
            create_sqlite(source, rows=25)

            with self.assertRaisesRegex(RuntimeError, "synthetic backup failure"):
                _backup_sqlite(
                    source,
                    destination,
                    lambda _done, _total: (_ for _ in ()).throw(
                        RuntimeError("synthetic backup failure")
                    ),
                )

            self.assertFalse(destination.exists())
            self.assertEqual(list(destination.parent.iterdir()), [])

    def test_cutover_requires_verified_dumb_config_backup_before_stop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config, process_handler, api_state = self.make_runtime(temp_dir)
            Path(config.file_path).unlink()
            manager = ArrPostgresMigrationManager(Path(temp_dir) / "migration")
            payload = {
                "job_id": "8" * 32,
                "process_name": "Sonarr TV",
                "service_key": "sonarr",
                "instance_name": "TV",
                "mode": "cutover",
                "include_logs": False,
                "status": "queued",
                "events": [],
            }
            self.persist_job(manager, payload)
            with (
                patch("utils.arr_postgres_migration._stop_process") as stop_process,
                patch("utils.arr_postgres_migration._drop_database"),
            ):
                manager._run_job(payload, config, process_handler, api_state, Mock())

            job = manager.get_job(payload["job_id"])
            self.assertEqual(job["status"], "failed")
            self.assertFalse(job["rollback_available"])
            stop_process.assert_not_called()

    def test_unverified_partial_infini_snapshot_is_ignored_and_live_sqlite_restarts(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main = root / "db.sqlite"
            create_sqlite(main)
            migration_root = root / "migration"
            manager = ArrPostgresMigrationManager(migration_root)
            backup_dir = manager.backups_dir / "infinidysk" / "job"
            backup_dir.mkdir(parents=True, mode=0o700)
            manager.root.chmod(0o700)
            manager.backups_dir.chmod(0o700)
            backup_dir.parent.chmod(0o700)
            backup_dir.chmod(0o700)
            partial = backup_dir / "db.sqlite"
            partial.write_bytes(b"incomplete")
            partial.chmod(0o600)
            instance = {
                "process_name": "InfiniDysk",
                "postgres_enabled": False,
                "config_dir": str(root),
                "env": {"CONFIG_PATH": str(root)},
            }
            config = SimpleNamespace(save_config=Mock())
            payload = {
                "process_name": "InfiniDysk",
                "backup_dir": str(backup_dir),
            }

            with (
                patch("utils.arr_postgres_migration._stop_infinidysk_if_running"),
                patch(
                    "utils.arr_postgres_migration._validate_infinidysk_snapshot"
                ) as validate_snapshot,
                patch("utils.arr_postgres_migration._apply_database_config"),
                patch("utils.arr_postgres_migration._restore_database_entries"),
                patch(
                    "utils.arr_postgres_migration._clear_infinidysk_rollback_authorization"
                ),
                patch("utils.arr_postgres_migration._start_process") as start_process,
                patch("utils.arr_postgres_migration._wait_for_running_service"),
                patch(
                    "utils.arr_postgres_migration._restore_infinidysk_sqlite_snapshot"
                ) as restore_snapshot,
            ):
                result = manager._restore_sqlite_runtime(
                    payload,
                    config,
                    Mock(),
                    "infinidysk",
                    None,
                    instance,
                    {"main": main},
                    None,
                    {},
                    True,
                    sqlite_backups={"main": partial},
                    binding={},
                    original_postgres_config={},
                )

            restore_snapshot.assert_not_called()
            validate_snapshot.assert_called_once_with(main, {})
            start_process.assert_called_once()
            self.assertTrue(result["service_restarted"])

    def test_start_requires_all_explicit_confirmations(self):
        manager = ArrPostgresMigrationManager("/tmp/dumb-migration-test")
        with self.assertRaisesRegex(ArrPostgresMigrationError, "All migration risk"):
            manager.create_job(
                config_manager=None,
                process_handler=None,
                api_state=None,
                logger=None,
                process_name="Sonarr TV",
                mode="rehearsal",
                include_logs=False,
                confirmation="MIGRATE Sonarr TV",
                acknowledge_unsupported=True,
                acknowledge_backup=False,
                acknowledge_target_reset=True,
            )

    def test_infinidysk_database_job_refuses_active_namespace_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            postgres_manager = ArrPostgresMigrationManager(root / "postgres")
            namespace_manager = InfiniDyskMigrationManager(
                root / "namespace" / "infinidysk.json"
            )
            namespace_manager._save_job(
                {
                    "job_id": "a" * 32,
                    "status": "queued",
                    "worker_id": namespace_manager._worker_id,
                    "worker_pid": os.getpid(),
                }
            )
            config_manager = MagicMock()
            config_manager.find_key_for_process.return_value = (
                "infinidysk",
                None,
            )
            with (
                patch(
                    "utils.infinidysk_migration.INFINIDYSK_MIGRATION_MANAGER",
                    namespace_manager,
                ),
                patch(
                    "utils.arr_postgres_migration.build_arr_postgres_preflight"
                ) as preflight,
                self.assertRaisesRegex(
                    ArrPostgresMigrationError,
                    "namespace migration job is active",
                ) as raised,
            ):
                postgres_manager.create_job(
                    config_manager=config_manager,
                    process_handler=MagicMock(),
                    api_state=MagicMock(),
                    logger=MagicMock(),
                    process_name="InfiniDysk",
                    mode="rehearsal",
                    include_logs=False,
                    confirmation="MIGRATE InfiniDysk",
                    acknowledge_unsupported=True,
                    acknowledge_backup=True,
                    acknowledge_target_reset=True,
                )

            preflight.assert_not_called()
            self.assertEqual(ACTIVE_NAMESPACE_MIGRATION_BLOCKER, str(raised.exception))
            self.assertFalse(postgres_manager.jobs_dir.exists())

    def test_infinidysk_database_job_refuses_active_service_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            postgres_manager = ArrPostgresMigrationManager(Path(temp_dir) / "postgres")
            namespace_manager = InfiniDyskMigrationManager(
                Path(temp_dir) / "namespace" / "infinidysk.json"
            )
            config_manager = MagicMock()
            config_manager.find_key_for_process.return_value = (
                "infinidysk",
                None,
            )
            with (
                patch(
                    "utils.infinidysk_migration.INFINIDYSK_MIGRATION_MANAGER",
                    namespace_manager,
                ),
                patch(
                    "utils.arr_postgres_migration.infinidysk_external_mutation_active",
                    return_value=True,
                ),
                patch(
                    "utils.arr_postgres_migration.build_arr_postgres_preflight"
                ) as preflight,
                self.assertRaisesRegex(
                    ArrPostgresMigrationError,
                    "Another DUMB operation is changing",
                ),
            ):
                postgres_manager.create_job(
                    config_manager=config_manager,
                    process_handler=MagicMock(),
                    api_state=MagicMock(),
                    logger=MagicMock(),
                    process_name="InfiniDysk",
                    mode="rehearsal",
                    include_logs=False,
                    confirmation="MIGRATE InfiniDysk",
                    acknowledge_unsupported=True,
                    acknowledge_backup=True,
                    acknowledge_target_reset=True,
                )

            preflight.assert_not_called()
            self.assertFalse(postgres_manager.jobs_dir.exists())

    def test_linked_arr_database_job_also_refuses_active_namespace_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            postgres_manager = ArrPostgresMigrationManager(root / "postgres")
            namespace_manager = InfiniDyskMigrationManager(
                root / "namespace" / "infinidysk.json"
            )
            namespace_manager._save_job(
                {
                    "job_id": "e" * 32,
                    "status": "running",
                    "worker_id": namespace_manager._worker_id,
                    "worker_pid": os.getpid(),
                }
            )
            config_manager = MagicMock()
            config_manager.find_key_for_process.return_value = ("sonarr", "TV")

            with (
                patch(
                    "utils.infinidysk_migration.INFINIDYSK_MIGRATION_MANAGER",
                    namespace_manager,
                ),
                patch(
                    "utils.arr_postgres_migration.build_arr_postgres_preflight"
                ) as preflight,
                self.assertRaisesRegex(
                    ArrPostgresMigrationError,
                    "namespace migration job is active",
                ),
            ):
                postgres_manager.create_job(
                    config_manager=config_manager,
                    process_handler=MagicMock(),
                    api_state=MagicMock(),
                    logger=MagicMock(),
                    process_name="Sonarr TV",
                    mode="rehearsal",
                    include_logs=False,
                    confirmation="MIGRATE Sonarr TV",
                    acknowledge_unsupported=True,
                    acknowledge_backup=True,
                    acknowledge_target_reset=True,
                )

            preflight.assert_not_called()
            self.assertFalse(postgres_manager.jobs_dir.exists())

    def test_any_postgres_recovery_hold_conflicts_with_namespace_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ArrPostgresMigrationManager(temp_dir)
            self.persist_job(
                manager,
                {
                    "job_id": "f" * 32,
                    "process_name": "Sonarr TV",
                    "service_key": "sonarr",
                    "status": "rollback_failed",
                    "rollback_available": False,
                    "events": [],
                },
            )

            self.assertTrue(manager.has_namespace_conflicting_job())
            self.assertFalse(manager.has_active_infinidysk_job())

    def test_boolean_values_are_normalized_for_postgres(self):
        self.assertIs(_convert_value(1, "boolean"), True)
        self.assertIs(_convert_value(0, "boolean"), False)
        self.assertIs(_convert_value("false", "boolean"), False)
        self.assertEqual(_convert_value("value", "text"), "value")
        for invalid in (2, -1, "yes", "bogus"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ArrPostgresMigrationError):
                    _convert_value(invalid, "boolean")

    def test_infinidysk_import_batches_are_bounded_by_converted_bytes(self):
        class Cursor:
            def __init__(self, rows):
                self.rows = iter(rows)

            def fetchone(self):
                return next(self.rows, None)

        rows = [
            (bytearray(b"a" * 80),),
            (memoryview(b"b" * 80),),
            (b"c" * 200,),
        ]
        batches = list(
            _converted_import_batches(
                Cursor(rows),
                ["Payload"],
                {
                    "Payload": {
                        "data_type": "bytea",
                        "is_generated": "NEVER",
                    }
                },
                batch_size=500,
                max_batch_bytes=150,
            )
        )

        self.assertEqual([len(batch) for batch in batches], [1, 1, 1])
        self.assertEqual(batches[-1], [(b"c" * 200,)])

    def test_full_row_digest_detects_non_key_value_corruption(self):
        source = sqlite3.connect(":memory:")
        source.execute('CREATE TABLE "Items" ("Id" INTEGER PRIMARY KEY, "Value" TEXT)')
        source.execute('INSERT INTO "Items" VALUES (1, "expected")')

        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.__iter__.return_value = iter([(1, "corrupt")])
        target = Mock()
        target.cursor.return_value = cursor
        target_columns = {
            "Id": {"data_type": "integer"},
            "Value": {"data_type": "text"},
        }
        try:
            with (
                patch(
                    "utils.arr_postgres_migration._postgres_table_columns",
                    return_value=target_columns,
                ),
                self.assertRaisesRegex(
                    ArrPostgresMigrationError, "Full-row content validation failed"
                ),
            ):
                _validate_full_row_digests(source, target, ["Items"])
        finally:
            source.close()

    def test_timestamp_digest_matches_postgres_microsecond_rounding(self):
        source_rows = [
            ("2025-12-29 09:12:47.2158351",),
            ("2025-12-29 09:13:43.9553597",),
            ("2025-12-29 09:14:43.1234565",),
            ("2025-12-29 09:15:43.1234555",),
            ("2025-12-29 09:16:43.1234545",),
            ("2025-12-29 09:17:43.1234565001",),
            ("2025-12-29 09:18:43.9999995",),
            ("2025-12-29 09:19:43.5084555",),
            ("2025-12-29 09:20:43.2525065",),
        ]
        postgres_rows = [
            (datetime(2025, 12, 29, 9, 12, 47, 215835),),
            (datetime(2025, 12, 29, 9, 13, 43, 955360),),
            (datetime(2025, 12, 29, 9, 14, 43, 123456),),
            (datetime(2025, 12, 29, 9, 15, 43, 123456),),
            (datetime(2025, 12, 29, 9, 16, 43, 123454),),
            (datetime(2025, 12, 29, 9, 17, 43, 123457),),
            (datetime(2025, 12, 29, 9, 18, 44),),
            (datetime(2025, 12, 29, 9, 19, 43, 508455),),
            (datetime(2025, 12, 29, 9, 20, 43, 252507),),
        ]

        source_digest = _digest_rows(
            source_rows,
            ["timestamp without time zone"],
            source_values=True,
        )
        postgres_digest = _digest_rows(
            postgres_rows,
            ["timestamp without time zone"],
            source_values=False,
        )

        self.assertEqual(source_digest, postgres_digest)

    def test_infinidysk_schema_helper_uses_isolated_loopback_migration_mode(self):
        process_handler = Mock()
        process_handler.start_process.return_value = (True, None)
        process_handler.returncode = 0
        with tempfile.TemporaryDirectory() as temp_dir:
            staging_path = Path(temp_dir) / "isolated-config"
            with (
                patch(
                    "utils.arr_postgres_migration._infinidysk_runtime_command",
                    return_value=(["dotnet", "InfiniDysk.dll"], None),
                ),
                patch(
                    "utils.arr_postgres_migration._validate_infinidysk_postgres_schema",
                    return_value={"fingerprint": "schema"},
                ),
            ):
                result = _prepare_service_schema(
                    "infinidysk",
                    {
                        "config_dir": "/infinidysk",
                        "backend_output_dir": "/infinidysk/app",
                        "env": {"CONFIG_PATH": "/live-config"},
                    },
                    process_handler,
                    postgres_config={"host": "127.0.0.1"},
                    database="stage",
                    staging_config_path=staging_path,
                )

        self.assertEqual(result, {"fingerprint": "schema"})
        args = process_handler.start_process.call_args
        self.assertEqual(args.args[2], ["dotnet", "InfiniDysk.dll", "--db-migration"])
        self.assertEqual(args.kwargs["env"]["CONFIG_PATH"], str(staging_path))
        self.assertEqual(args.kwargs["env"]["ASPNETCORE_URLS"], "http://127.0.0.1:0")
        process_handler.wait.assert_called_once()

    def test_sequence_reset_uses_oid_for_mixed_case_identity_sequences(self):
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [(7,), (None,)]
        connection = Mock()
        connection.cursor.return_value = cursor
        sequence_specs = [
            {
                "table": "IndexerApiHits",
                "column": "Id",
                "sequence_oid": 101,
            },
            {
                "table": "WatchdogEntries",
                "column": "Id",
                "sequence_oid": 202,
            },
        ]

        with patch(
            "utils.arr_postgres_migration._postgres_sequence_specs",
            return_value=sequence_specs,
        ):
            reset_count = _reset_postgres_sequences(connection)

        self.assertEqual(reset_count, 2)
        setval_calls = [
            item
            for item in cursor.execute.call_args_list
            if isinstance(item.args[0], str) and "setval" in item.args[0]
        ]
        self.assertEqual(setval_calls[0].args[1], [101, 7])
        self.assertEqual(setval_calls[1].args[1], [202])
        self.assertTrue(all("::oid::regclass" in item.args[0] for item in setval_calls))

    def test_matching_infinidysk_rehearsal_requires_validated_main_import(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ArrPostgresMigrationManager(Path(temp_dir) / "migration")
            binding = {
                "service_key": "infinidysk",
                "database_contract": INFINIDYSK_DATABASE_CONTRACTS[-1]["id"],
                "source_schema_fingerprint": INFINIDYSK_SQLITE_SCHEMA_FINGERPRINT,
                "source_migration_history_fingerprint": (
                    INFINIDYSK_SQLITE_MIGRATION_HISTORY_SHA256
                ),
                "launch_config_fingerprint": "launch-a",
            }
            payload = {
                "job_id": "9" * 32,
                "process_name": "InfiniDysk",
                "service_key": "infinidysk",
                "mode": "rehearsal",
                "status": "completed",
                "binding": binding,
                "result": {
                    "validated": True,
                    "binding": binding,
                    "adapter_schema": INFINIDYSK_POSTGRES_ADAPTER_SCHEMA,
                    "postgres_schema_fingerprint": (
                        INFINIDYSK_POSTGRES_SCHEMA_FINGERPRINT
                    ),
                    "imports": {
                        "main": {
                            "validated": False,
                            "tables": 23,
                            "primary_key_digests_validated": 23,
                            "full_row_digests_validated": 23,
                            "foreign_keys_validated": 4,
                            "sequences_validated": 2,
                            "postgres_schema_fingerprint": (
                                INFINIDYSK_POSTGRES_SCHEMA_FINGERPRINT
                            ),
                        }
                    },
                },
            }
            self.persist_job(manager, payload)
            self.assertIsNone(manager._matching_rehearsal("InfiniDysk", binding))

            payload["result"]["imports"]["main"]["validated"] = True
            manager._save(payload)

            self.assertEqual(
                manager._matching_rehearsal("InfiniDysk", binding)["job_id"],
                payload["job_id"],
            )
            changed_binding = {**binding, "launch_config_fingerprint": "launch-b"}
            self.assertIsNone(
                manager._matching_rehearsal("InfiniDysk", changed_binding)
            )

    def test_infinidysk_command_drift_during_final_health_check_refuses_before_stop(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main = root / "db.sqlite"
            create_sqlite(main)
            instance = {
                "enabled": True,
                "process_name": "InfiniDysk",
                "config_dir": str(root),
                "postgres_enabled": False,
                "postgres_database": "infinidysk",
                "command": ["/infinidysk/start.sh"],
                "env": {"CONFIG_PATH": str(root)},
            }
            config_file = root / "dumb_config.json"
            config_file.write_text("{}", encoding="utf-8")
            config = SimpleNamespace(
                config={"infinidysk": instance, "postgres": {}},
                file_path=str(config_file),
                find_key_for_process=lambda _name: ("infinidysk", None),
                get_instance=lambda _name, _key: instance,
                get=lambda key, default=None: {
                    "infinidysk": instance,
                    "postgres": {},
                }.get(key, default),
                save_config=Mock(),
            )
            manager = ArrPostgresMigrationManager(root / "migration")
            payload = {
                "job_id": "d" * 32,
                "process_name": "InfiniDysk",
                "service_key": "infinidysk",
                "instance_name": None,
                "mode": "cutover",
                "include_logs": False,
                "status": "queued",
                "binding": {
                    "launch_config_fingerprint": "bound-launch",
                    "source_schema_fingerprint": "bound-schema",
                },
                "events": [],
            }
            self.persist_job(manager, payload)

            with (
                patch(
                    "utils.arr_postgres_migration._source_paths",
                    return_value={
                        "main": main,
                        "config_dir": root,
                        "auxiliary": [],
                    },
                ),
                patch(
                    "utils.arr_postgres_migration.infinidysk_launch_config_fingerprint",
                    side_effect=["bound-launch", "changed-launch"],
                ),
                patch(
                    "utils.arr_postgres_migration._sqlite_schema_fingerprint",
                    return_value="bound-schema",
                ),
                patch(
                    "utils.arr_postgres_migration._tracked_process_identity",
                    return_value=(123, 123),
                ),
                patch(
                    "utils.arr_postgres_migration._infinidysk_application_health",
                    return_value=(True, "healthy"),
                ),
                patch(
                    "utils.arr_postgres_migration._stop_tracked_infinidysk_process"
                ) as stop_process,
                patch.object(manager, "_restore_sqlite_runtime") as restore_runtime,
                patch("utils.arr_postgres_migration._drop_database"),
            ):
                manager._run_job(payload, config, Mock(), Mock(), Mock())

            job = manager.get_job(payload["job_id"])
            self.assertEqual(job["status"], "failed_rolled_back")
            self.assertIn("launch configuration changed", job["error"]["message"])
            stop_process.assert_not_called()
            restore_runtime.assert_not_called()

    def test_rollback_refuses_untracked_but_running_infinidysk(self):
        process_handler = SimpleNamespace(
            process_names={},
            _prefixed_name=lambda name: name,
        )
        api_state = SimpleNamespace(get_status=lambda _: "running")
        with (
            patch(
                "utils.arr_postgres_migration._infinidysk_loopback_health",
                return_value=(False, "not listening"),
            ),
            patch(
                "utils.arr_postgres_migration.is_port_available",
                return_value=True,
            ),
            self.assertRaisesRegex(
                ArrPostgresMigrationError,
                "without a bound DUMB process",
            ),
        ):
            _stop_infinidysk_if_running(
                process_handler,
                "InfiniDysk",
                api_state=api_state,
                instance={"backend_port": 8080},
            )

    def test_schema_helper_timeout_stops_and_verifies_exact_group(self):
        running = {"value": True}
        process = Mock(pid=1234, returncode=None)
        process.poll.side_effect = lambda: None if running["value"] else 1
        process.wait.side_effect = subprocess.TimeoutExpired("helper", 1)
        process_handler = SimpleNamespace(
            process_names={"helper": process},
            _prefixed_name=lambda name: name,
            _process_group_alive=lambda _: False,
            stop_process=lambda _: running.update(value=False),
        )

        with (
            patch("utils.arr_postgres_migration.os.getpgid", return_value=4321),
            self.assertRaisesRegex(
                ArrPostgresMigrationError,
                "exceeded its timeout",
            ),
        ):
            _wait_for_schema_helper(process_handler, "helper", timeout=1)

        self.assertFalse(running["value"])

    def test_cold_stop_refuses_process_replacement_after_health_binding(self):
        original = Mock(pid=1234)
        original.poll.return_value = None
        replacement = Mock(pid=5678)
        replacement.poll.return_value = None
        process_handler = SimpleNamespace(
            process_names={"InfiniDysk": replacement},
            _prefixed_name=lambda name: name,
            _process_group_alive=lambda _: False,
            stop_process=Mock(),
        )

        with (
            patch("utils.arr_postgres_migration.os.getpgid", return_value=9999),
            self.assertRaisesRegex(
                ArrPostgresMigrationError,
                "process changed after the health check",
            ),
        ):
            _stop_tracked_infinidysk_process(
                process_handler,
                "InfiniDysk",
                {"backend_port": 8080},
                expected_identity=(original, 1111),
            )

        process_handler.stop_process.assert_not_called()

    def test_cold_stop_refuses_an_occupied_backend_port_after_group_exit(self):
        running = {"value": True}
        process = Mock(pid=1234)
        process.poll.side_effect = lambda: None if running["value"] else 0
        process_handler = SimpleNamespace(
            process_names={"InfiniDysk": process},
            _prefixed_name=lambda name: name,
            _process_group_alive=lambda _: False,
            stop_process=lambda _: running.update(value=False),
        )

        with (
            patch("utils.arr_postgres_migration.os.getpgid", return_value=4321),
            patch(
                "utils.arr_postgres_migration.is_port_available",
                return_value=False,
            ),
            self.assertRaisesRegex(
                ArrPostgresMigrationError,
                "backend port",
            ),
        ):
            _stop_tracked_infinidysk_process(
                process_handler,
                "InfiniDysk",
                {"backend_port": 8080},
            )

        self.assertFalse(running["value"])

    def test_rollback_refuses_untracked_degraded_listener(self):
        process_handler = SimpleNamespace(
            process_names={},
            _prefixed_name=lambda name: name,
        )
        api_state = SimpleNamespace(get_status=lambda _: "stopped")
        with (
            patch(
                "utils.arr_postgres_migration._infinidysk_loopback_health",
                return_value=(False, "migrating"),
            ),
            patch(
                "utils.arr_postgres_migration.is_port_available",
                return_value=False,
            ),
            self.assertRaisesRegex(
                ArrPostgresMigrationError,
                "without a bound DUMB process",
            ),
        ):
            _stop_infinidysk_if_running(
                process_handler,
                "InfiniDysk",
                api_state=api_state,
                instance={"backend_port": 8080},
            )

    def test_schema_wait_fails_immediately_when_service_exits(self):
        process = Mock()
        process.poll.return_value = 1
        process_handler = SimpleNamespace(process_names={"Bazarr": process})

        with (
            patch(
                "utils.arr_postgres_migration._postgres_database_summary",
                return_value={
                    "name": "stage",
                    "exists": True,
                    "table_count": 0,
                    "row_count": 0,
                },
            ),
            self.assertRaisesRegex(
                ArrPostgresMigrationError,
                "Bazarr exited while initializing",
            ),
        ):
            _wait_for_schema(
                {},
                ["stage"],
                timeout=180,
                process_handler=process_handler,
                process_name="Bazarr",
            )

    def test_bazarr_schema_preparation_ensures_postgres_driver(self):
        process_handler = Mock()

        with patch(
            "utils.arr_postgres_migration._ensure_bazarr_postgres_driver",
            return_value=(True, None),
        ) as ensure_driver:
            _prepare_service_schema(
                "bazarr",
                {"config_dir": "/opt/bazarr"},
                process_handler,
            )

        ensure_driver.assert_called_once_with(process_handler, "/opt/bazarr")

    def test_altmount_postgres_migration_010_repair_is_narrow_and_atomic(self):
        cursor = Mock()
        cursor.__enter__ = Mock(return_value=cursor)
        cursor.__exit__ = Mock(return_value=False)
        cursor.fetchone.side_effect = [(9,), (True,)]
        connection = Mock()
        connection.cursor.return_value = cursor

        with patch(
            "utils.arr_postgres_migration._pg_connect", return_value=connection
        ) as connect:
            repaired = _repair_altmount_postgres_migration_010(
                {"host": "127.0.0.1"}, "dumb_stage_altmount_test_main"
            )

        self.assertTrue(repaired)
        connect.assert_called_once_with(
            {"host": "127.0.0.1"}, "dumb_stage_altmount_test_main"
        )
        executed_sql = [entry.args[0] for entry in cursor.execute.call_args_list]
        self.assertTrue(
            any(
                "ON import_queue ((metadata::jsonb ->> 'nzbdav_id'))" in statement
                for statement in executed_sql
            )
        )
        self.assertTrue(
            any(
                "SELECT 10, TRUE WHERE NOT EXISTS" in statement
                for statement in executed_sql
            )
        )
        connection.commit.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_altmount_postgres_migration_010_repair_refuses_other_versions(self):
        cursor = Mock()
        cursor.__enter__ = Mock(return_value=cursor)
        cursor.__exit__ = Mock(return_value=False)
        cursor.fetchone.return_value = (8,)
        connection = Mock()
        connection.cursor.return_value = cursor

        with patch("utils.arr_postgres_migration._pg_connect", return_value=connection):
            repaired = _repair_altmount_postgres_migration_010(
                {}, "dumb_stage_altmount_test_main"
            )

        self.assertFalse(repaired)
        connection.rollback.assert_called_once_with()
        connection.commit.assert_not_called()
        connection.close.assert_called_once_with()

    def test_single_database_service_rehearsal_restores_sqlite_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = StubPulsarrConfig(temp_dir)
            db_dir = Path(temp_dir) / "pulsarr" / "data" / "db"
            db_dir.mkdir(parents=True)
            create_sqlite(db_dir / "pulsarr.db", table="users")
            process_handler = Mock()
            process_handler.start_process.return_value = (True, None)
            process_handler.returncode = 0
            api_state = SimpleNamespace(get_status=lambda _: "running")
            manager = ArrPostgresMigrationManager(Path(temp_dir) / "migration")
            payload = {
                "job_id": "d" * 32,
                "process_name": "Pulsarr",
                "mode": "rehearsal",
                "include_logs": True,
                "status": "queued",
                "events": [],
            }
            self.persist_job(manager, payload)
            with (
                patch(
                    "utils.arr_postgres_migration._initialize_database_names"
                ) as initialize,
                patch("utils.arr_postgres_migration._wait_for_schema"),
                patch("utils.arr_postgres_migration._wait_for_running_service"),
                patch(
                    "utils.arr_postgres_migration.import_sqlite_to_postgres",
                    return_value={
                        "database": "stage",
                        "tables": 1,
                        "rows": 2,
                        "sequences_reset": 1,
                        "row_counts": {"users": 2},
                        "validated": True,
                    },
                ) as importer,
                patch("utils.arr_postgres_migration._drop_database"),
            ):
                manager._run_job(payload, config, process_handler, api_state, Mock())

            job = manager.get_job(payload["job_id"])
            service = config.config["pulsarr"]
            self.assertEqual(job["status"], "completed")
            self.assertTrue(job["result"]["sqlite_runtime_restored"])
            self.assertFalse(service["postgres_enabled"])
            self.assertEqual(service["env"]["dbType"], "sqlite")
            self.assertEqual(config.config["postgres"]["databases"], [])
            initialize.assert_any_call(
                config.config["postgres"],
                ["dumb_stage_pulsarr_dddddddd_main"],
            )
            self.assertEqual(importer.call_count, 1)
            process_handler.start_process.assert_any_call(
                "bun_migrate",
                str(Path(temp_dir) / "pulsarr"),
                [
                    "/config/.bun/bin/bun",
                    "run",
                    "--bun",
                    "migrations/migrate.ts",
                ],
                env=ANY,
            )
            pulsarr_start = next(
                item
                for item in process_handler.start_process.call_args_list
                if item.args and item.args[0] == "Pulsarr"
            )
            self.assertEqual(
                pulsarr_start.kwargs["env"]["dbName"],
                "dumb_stage_pulsarr_dddddddd_main",
            )

    def test_job_status_rejects_path_traversal_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ArrPostgresMigrationManager(temp_dir)
            self.assertIsNone(manager.get_job("../../dumb_config"))

    def test_job_status_rejects_job_file_symlink_outside_jobs_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = ArrPostgresMigrationManager(root / "migration")
            manager.jobs_dir.mkdir(parents=True)
            job_id = "f" * 32
            outside = root / "outside.json"
            outside.write_text(
                json.dumps({"job_id": job_id, "status": "completed"}),
                encoding="utf-8",
            )
            (manager.jobs_dir / f"{job_id}.json").symlink_to(outside)

            self.assertIsNone(manager.get_job(job_id))

    def test_job_status_rejects_mismatched_persisted_job_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ArrPostgresMigrationManager(temp_dir)
            manager.jobs_dir.mkdir(parents=True)
            requested_job_id = "f" * 32
            (manager.jobs_dir / f"{requested_job_id}.json").write_text(
                json.dumps(
                    {
                        "job_id": "e" * 32,
                        "status": "completed",
                    }
                ),
                encoding="utf-8",
            )

            self.assertIsNone(manager.get_job(requested_job_id))

    def test_rollback_accepts_legacy_config_xml_backup_location(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config, process_handler, api_state = self.make_runtime(temp_dir)
            manager = ArrPostgresMigrationManager(Path(temp_dir) / "migration")
            backup_dir = manager.backups_dir / "legacy-backup"
            backup_dir.mkdir(parents=True, mode=0o700)
            manager.root.chmod(0o700)
            manager.backups_dir.chmod(0o700)
            backup_dir.chmod(0o700)
            (backup_dir / "config.xml").write_text("<Config />", encoding="utf-8")
            (backup_dir / "dumb_config.json").write_text(
                json.dumps(config.config), encoding="utf-8"
            )
            (backup_dir / "sonarr.db").write_bytes(b"sqlite backup")
            for path in backup_dir.iterdir():
                path.chmod(0o600)
            payload = {
                "job_id": "e" * 32,
                "process_name": "Sonarr TV",
                "service_key": "sonarr",
                "instance_name": "TV",
                "mode": "cutover",
                "status": "completed",
                "include_logs": False,
                "rollback_available": True,
                "backup_dir": str(backup_dir),
                "events": [],
            }
            self.persist_job(manager, payload)

            with patch.object(
                manager,
                "_restore_sqlite_runtime",
                return_value={"sqlite_preserved": True},
            ) as restore:
                manager.rollback_job(
                    payload["job_id"],
                    "ROLLBACK Sonarr TV",
                    config,
                    process_handler,
                    api_state,
                )

            self.assertEqual(restore.call_args.args[7], backup_dir / "config.xml")

    def test_completed_rehearsal_is_never_rollback_eligible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ArrPostgresMigrationManager(Path(temp_dir) / "migration")
            payload = {
                "job_id": "1" * 32,
                "process_name": "Sonarr TV",
                "service_key": "sonarr",
                "instance_name": "TV",
                "mode": "rehearsal",
                "status": "completed",
                "rollback_available": True,
                "events": [],
            }
            self.persist_job(manager, payload)
            with (
                patch.object(manager, "_restore_sqlite_runtime") as restore,
                self.assertRaisesRegex(
                    ArrPostgresMigrationError,
                    "unused guarded cutover rollback",
                ),
            ):
                manager.rollback_job(
                    payload["job_id"],
                    "ROLLBACK Sonarr TV",
                    Mock(),
                    Mock(),
                    Mock(),
                )
            restore.assert_not_called()

    def test_rollback_stop_failure_is_terminal_and_retryable_before_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config, process_handler, api_state = self.make_runtime(temp_dir)
            manager = ArrPostgresMigrationManager(Path(temp_dir) / "migration")
            backup_dir = manager.backups_dir / "retry-backup"
            backup_dir.mkdir(parents=True, mode=0o700)
            manager.root.chmod(0o700)
            manager.backups_dir.chmod(0o700)
            backup_dir.chmod(0o700)
            (backup_dir / "config.xml").write_text("<Config />", encoding="utf-8")
            (backup_dir / "dumb_config.json").write_text(
                json.dumps(config.config), encoding="utf-8"
            )
            (backup_dir / "sonarr.db").write_bytes(b"sqlite backup")
            for path in backup_dir.iterdir():
                path.chmod(0o600)
            payload = {
                "job_id": "2" * 32,
                "process_name": "Sonarr TV",
                "service_key": "sonarr",
                "instance_name": "TV",
                "mode": "cutover",
                "status": "failed",
                "include_logs": False,
                "rollback_available": True,
                "backup_dir": str(backup_dir),
                "events": [],
            }
            self.persist_job(manager, payload)

            with (
                patch.object(
                    manager,
                    "_restore_sqlite_runtime",
                    side_effect=ArrPostgresMigrationError("still running"),
                ),
                self.assertRaisesRegex(
                    ArrPostgresMigrationError,
                    "may be retried",
                ),
            ):
                manager.rollback_job(
                    payload["job_id"],
                    "ROLLBACK Sonarr TV",
                    config,
                    process_handler,
                    api_state,
                )

            failed = manager.get_job(payload["job_id"])
            self.assertEqual(failed["status"], "rollback_failed")
            self.assertTrue(failed["rollback_available"])
            with patch.object(
                manager,
                "_restore_sqlite_runtime",
                return_value={"sqlite_preserved": True},
            ):
                retried = manager.rollback_job(
                    payload["job_id"],
                    "ROLLBACK Sonarr TV",
                    config,
                    process_handler,
                    api_state,
                )
            self.assertEqual(retried["status"], "rolled_back")
            self.assertFalse(retried["rollback_available"])

    def test_duplicate_rollback_request_is_refused_after_atomic_reservation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config, process_handler, api_state = self.make_runtime(temp_dir)
            manager = ArrPostgresMigrationManager(Path(temp_dir) / "migration")
            backup_dir = manager.backups_dir / "atomic-rollback"
            backup_dir.mkdir(parents=True, mode=0o700)
            manager.root.chmod(0o700)
            manager.backups_dir.chmod(0o700)
            backup_dir.chmod(0o700)
            (backup_dir / "config.xml").write_text("<Config />", encoding="utf-8")
            (backup_dir / "dumb_config.json").write_text(
                json.dumps(config.config), encoding="utf-8"
            )
            (backup_dir / "sonarr.db").write_bytes(b"sqlite backup")
            for path in backup_dir.iterdir():
                path.chmod(0o600)
            payload = {
                "job_id": "4" * 32,
                "process_name": "Sonarr TV",
                "service_key": "sonarr",
                "instance_name": "TV",
                "mode": "cutover",
                "status": "completed",
                "include_logs": False,
                "rollback_available": True,
                "backup_dir": str(backup_dir),
                "events": [],
            }
            self.persist_job(manager, payload)
            entered = threading.Event()
            release = threading.Event()
            first_errors = []

            def restore(*_args, **_kwargs):
                entered.set()
                release.wait(timeout=2)
                return {"sqlite_preserved": True}

            def first_rollback():
                try:
                    manager.rollback_job(
                        payload["job_id"],
                        "ROLLBACK Sonarr TV",
                        config,
                        process_handler,
                        api_state,
                    )
                except Exception as error:  # pragma: no cover - assertion below
                    first_errors.append(error)

            with patch.object(manager, "_restore_sqlite_runtime", side_effect=restore):
                worker = threading.Thread(target=first_rollback)
                worker.start()
                self.assertTrue(entered.wait(timeout=1))
                with self.assertRaisesRegex(
                    ArrPostgresMigrationError,
                    "while the job is active",
                ):
                    manager.rollback_job(
                        payload["job_id"],
                        "ROLLBACK Sonarr TV",
                        config,
                        process_handler,
                        api_state,
                    )
                release.set()
                worker.join(timeout=2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(first_errors, [])
            self.assertEqual(
                manager.get_job(payload["job_id"])["status"], "rolled_back"
            )

    def test_infinidysk_rollback_refuses_active_namespace_before_reservation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ArrPostgresMigrationManager(Path(temp_dir) / "migration")
            payload = {
                "job_id": "5" * 32,
                "process_name": "InfiniDysk",
                "service_key": "infinidysk",
                "instance_name": None,
                "mode": "cutover",
                "status": "failed",
                "rollback_available": True,
                "events": [],
            }
            self.persist_job(manager, payload)
            with (
                patch(
                    "utils.arr_postgres_migration.infinidysk_namespace_migration_active",
                    return_value=True,
                ),
                patch.object(manager, "_reserve_rollback") as reserve,
                self.assertRaisesRegex(
                    ArrPostgresMigrationError,
                    "namespace migration job is active",
                ),
            ):
                manager.rollback_job(
                    payload["job_id"],
                    "ROLLBACK InfiniDysk",
                    Mock(),
                    Mock(),
                    Mock(),
                )
            reserve.assert_not_called()

    def test_infinidysk_rollback_refuses_changed_source_path_before_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_main = root / "old" / "db.sqlite"
            current_root = root / "current"
            current_root.mkdir()
            instance = {
                "process_name": "InfiniDysk",
                "config_dir": str(current_root),
                "env": {"CONFIG_PATH": str(current_root)},
            }
            config = SimpleNamespace(
                config={"infinidysk": instance, "postgres": {}},
                find_key_for_process=lambda _: ("infinidysk", None),
                get_instance=lambda _name, _key: instance,
                get=lambda key, default=None: {
                    "infinidysk": instance,
                    "postgres": {},
                }.get(key, default),
            )
            manager = ArrPostgresMigrationManager(root / "migration")
            payload = {
                "job_id": "3" * 32,
                "process_name": "InfiniDysk",
                "service_key": "infinidysk",
                "instance_name": None,
                "mode": "cutover",
                "status": "failed",
                "rollback_available": True,
                "rollback_checkpoint": "cold_backup_verified",
                "preflight": {"sqlite": {"main": {"path": str(old_main)}}},
                "binding": {
                    "source_path_fingerprint": (
                        __import__(
                            "utils.service_postgres", fromlist=["unused"]
                        ).infinidysk_sqlite_source_path_fingerprint(old_main)
                    )
                },
                "events": [],
            }
            self.persist_job(manager, payload)
            with (
                patch.object(manager, "_restore_sqlite_runtime") as restore,
                self.assertRaisesRegex(
                    ArrPostgresMigrationError,
                    "source path changed",
                ),
            ):
                manager.rollback_job(
                    payload["job_id"],
                    "ROLLBACK InfiniDysk",
                    config,
                    Mock(),
                    Mock(),
                )
            restore.assert_not_called()

    def test_rehearsal_uses_isolated_stage_and_restores_sqlite_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config, process_handler, api_state = self.make_runtime(temp_dir)
            manager = ArrPostgresMigrationManager(Path(temp_dir) / "migration")
            payload = {
                "job_id": "a" * 32,
                "process_name": "Sonarr TV",
                "mode": "rehearsal",
                "include_logs": False,
                "status": "queued",
                "events": [],
            }
            self.persist_job(manager, payload)
            import_result = {
                "database": "stage",
                "tables": 1,
                "rows": 2,
                "sequences_reset": 1,
                "row_counts": {"Series": 2},
                "validated": True,
            }
            with (
                patch(
                    "utils.arr_postgres_migration._initialize_database_names"
                ) as initialize,
                patch("utils.arr_postgres_migration._wait_for_schema"),
                patch("utils.arr_postgres_migration._wait_for_running_service"),
                patch(
                    "utils.arr_postgres_migration.import_sqlite_to_postgres",
                    return_value=import_result,
                ) as importer,
                patch("utils.arr_postgres_migration._drop_database") as drop_database,
                patch("utils.arr_postgres_migration._clone_database") as clone_database,
            ):
                manager._run_job(payload, config, process_handler, api_state, Mock())

            job = manager.get_job(payload["job_id"])
            self.assertEqual(job["status"], "completed")
            self.assertTrue(job["result"]["sqlite_runtime_restored"])
            self.assertFalse(
                config.config["sonarr"]["instances"]["TV"]["postgres_enabled"]
            )
            stage_names = [
                "dumb_stage_sonarr_aaaaaaaa_main",
                "dumb_stage_sonarr_aaaaaaaa_log",
            ]
            initialize.assert_any_call(config.config["postgres"], stage_names)
            self.assertEqual(importer.call_args.args[2], stage_names[0])
            clone_database.assert_not_called()
            drop_database.assert_has_calls(
                [call(config.config["postgres"], name) for name in stage_names]
            )

    def test_cutover_clones_current_stage_schema_before_persisting_postgres(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config, process_handler, api_state = self.make_runtime(temp_dir)
            manager = ArrPostgresMigrationManager(Path(temp_dir) / "migration")
            payload = {
                "job_id": "b" * 32,
                "process_name": "Sonarr TV",
                "mode": "cutover",
                "include_logs": False,
                "status": "queued",
                "events": [],
            }
            self.persist_job(manager, payload)
            with (
                patch("utils.arr_postgres_migration._initialize_database_names"),
                patch("utils.arr_postgres_migration._wait_for_schema"),
                patch("utils.arr_postgres_migration._wait_for_running_service"),
                patch(
                    "utils.arr_postgres_migration.import_sqlite_to_postgres",
                    return_value={
                        "database": "target",
                        "tables": 1,
                        "rows": 2,
                        "sequences_reset": 1,
                        "row_counts": {"Series": 2},
                        "validated": True,
                    },
                ),
                patch("utils.arr_postgres_migration._drop_database"),
                patch("utils.arr_postgres_migration._clone_database") as clone_database,
            ):
                manager._run_job(payload, config, process_handler, api_state, Mock())

            job = manager.get_job(payload["job_id"])
            instance = config.config["sonarr"]["instances"]["TV"]
            self.assertEqual(job["status"], "completed")
            self.assertTrue(job["rollback_available"])
            self.assertEqual(job["rollback_checkpoint"], "cold_backup_verified")
            self.assertTrue(instance["postgres_enabled"])
            self.assertEqual(instance["postgres_main_db"], "sonarr_tv_main")
            clone_database.assert_has_calls(
                [
                    call(
                        config.config["postgres"],
                        "dumb_stage_sonarr_bbbbbbbb_main",
                        "sonarr_tv_main",
                    ),
                    call(
                        config.config["postgres"],
                        "dumb_stage_sonarr_bbbbbbbb_log",
                        "sonarr_tv_log",
                    ),
                ]
            )

    def test_cutover_crash_after_cold_backup_retains_interrupted_rollback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config, process_handler, api_state = self.make_runtime(temp_dir)
            root = Path(temp_dir) / "migration"
            manager = ArrPostgresMigrationManager(root)
            payload = {
                "job_id": "6" * 32,
                "process_name": "Sonarr TV",
                "mode": "cutover",
                "include_logs": False,
                "status": "queued",
                "events": [],
            }
            self.persist_job(manager, payload)
            with (
                patch(
                    "utils.arr_postgres_migration._initialize_database_names",
                    side_effect=KeyboardInterrupt,
                ),
                patch("utils.arr_postgres_migration._drop_database"),
                self.assertRaises(KeyboardInterrupt),
            ):
                manager._run_job(payload, config, process_handler, api_state, Mock())

            restarted = ArrPostgresMigrationManager(root)
            recovered = restarted.get_job(payload["job_id"])
            self.assertEqual(recovered["status"], "interrupted")
            self.assertTrue(recovered["rollback_available"])
            self.assertEqual(recovered["rollback_checkpoint"], "cold_backup_verified")

    def test_cutover_failure_automatically_restores_sqlite_configuration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config, process_handler, api_state = self.make_runtime(temp_dir)
            manager = ArrPostgresMigrationManager(Path(temp_dir) / "migration")
            payload = {
                "job_id": "c" * 32,
                "process_name": "Sonarr TV",
                "mode": "cutover",
                "include_logs": False,
                "status": "queued",
                "events": [],
            }
            self.persist_job(manager, payload)
            with (
                patch("utils.arr_postgres_migration._initialize_database_names"),
                patch("utils.arr_postgres_migration._wait_for_schema"),
                patch("utils.arr_postgres_migration._wait_for_running_service"),
                patch(
                    "utils.arr_postgres_migration.import_sqlite_to_postgres",
                    side_effect=ArrPostgresMigrationError("synthetic import failure"),
                ),
                patch("utils.arr_postgres_migration._drop_database"),
                patch("utils.arr_postgres_migration._clone_database"),
            ):
                manager._run_job(payload, config, process_handler, api_state, Mock())

            job = manager.get_job(payload["job_id"])
            instance = config.config["sonarr"]["instances"]["TV"]
            config_xml = Path(instance["config_file"]).read_text(encoding="utf-8")
            self.assertEqual(job["status"], "failed_rolled_back")
            self.assertTrue(job["rollback"]["sqlite_preserved"])
            self.assertFalse(job["rollback_available"])
            self.assertFalse(instance["postgres_enabled"])
            self.assertNotIn("PostgresHost", config_xml)
            with self.assertRaisesRegex(
                ArrPostgresMigrationError,
                "unused guarded cutover rollback",
            ):
                manager.rollback_job(
                    payload["job_id"],
                    "ROLLBACK Sonarr TV",
                    config,
                    process_handler,
                    api_state,
                )


if __name__ == "__main__":
    unittest.main()
