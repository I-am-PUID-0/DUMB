import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, call, patch

from utils.arr_postgres_migration import (
    ArrPostgresMigrationError,
    ArrPostgresMigrationManager,
    SUPPORTED_SERVICES,
    _backup_sqlite,
    _convert_value,
    _prepare_service_schema,
    _repair_altmount_postgres_migration_010,
    _source_paths,
    _wait_for_schema,
    build_arr_postgres_preflight,
)


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
    def test_supported_inventory_includes_every_confirmed_dual_backend_service(self):
        self.assertEqual(
            set(SUPPORTED_SERVICES),
            {
                "altmount",
                "bazarr",
                "lidarr",
                "prowlarr",
                "pulsarr",
                "radarr",
                "seerr",
                "sonarr",
                "whisparr",
            },
        )

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

    def test_boolean_values_are_normalized_for_postgres(self):
        self.assertIs(_convert_value(1, "boolean"), True)
        self.assertIs(_convert_value(0, "boolean"), False)
        self.assertIs(_convert_value("false", "boolean"), False)
        self.assertEqual(_convert_value("value", "text"), "value")

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
            self.assertEqual(
                initialize.call_args.args[1],
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

    def test_rollback_accepts_legacy_config_xml_backup_location(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config, process_handler, api_state = self.make_runtime(temp_dir)
            manager = ArrPostgresMigrationManager(Path(temp_dir) / "migration")
            backup_dir = Path(temp_dir) / "legacy-backup"
            backup_dir.mkdir()
            (backup_dir / "config.xml").write_text("<Config />", encoding="utf-8")
            (backup_dir / "dumb_config.json").write_text("{}", encoding="utf-8")
            payload = {
                "job_id": "e" * 32,
                "process_name": "Sonarr TV",
                "mode": "cutover",
                "status": "completed",
                "backup_dir": str(backup_dir),
                "events": [],
            }
            manager._save(payload)

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
            stage_names = initialize.call_args.args[1]
            self.assertEqual(
                stage_names,
                ["dumb_stage_sonarr_aaaaaaaa_main", "dumb_stage_sonarr_aaaaaaaa_log"],
            )
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
            self.assertFalse(instance["postgres_enabled"])
            self.assertNotIn("PostgresHost", config_xml)


if __name__ == "__main__":
    unittest.main()
