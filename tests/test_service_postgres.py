import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from api.routers import process as process_router
from utils.service_postgres import (
    apply_service_postgres_config,
    configure_service_postgres_runtime,
    service_postgres_database_name,
)


class ServicePostgresTests(unittest.TestCase):
    def setUp(self):
        self.postgres = {
            "host": "127.0.0.1",
            "port": 5432,
            "user": "DUMB user",
            "password": "secret/value",
        }

    def test_database_name_sanitizes_multi_instance_name(self):
        self.assertEqual(
            service_postgres_database_name("seerr", "Family / 4K", {}),
            "seerr_family_4k",
        )

    def test_blank_infinidysk_database_name_uses_service_default(self):
        self.assertEqual(
            service_postgres_database_name(
                "infinidysk", None, {"postgres_database": "  "}
            ),
            "infinidysk",
        )

    def test_runtime_reenables_an_existing_database_entry(self):
        class Config:
            def __init__(self):
                self.config = {
                    "bazarr": {
                        "enabled": True,
                        "postgres_enabled": True,
                        "process_name": "Bazarr",
                        "env": {},
                    },
                    "postgres": {
                        "enabled": True,
                        "databases": [{"name": "bazarr", "enabled": False}],
                    },
                }

            def get(self, key, default=None):
                return self.config.get(key, default)

        config = Config()

        self.assertTrue(configure_service_postgres_runtime(config))
        self.assertTrue(config.config["postgres"]["databases"][0]["enabled"])

    def test_bazarr_environment_switches_both_directions(self):
        service = {"env": {}}

        apply_service_postgres_config(
            "bazarr", service, self.postgres, "bazarr", enabled=True
        )
        self.assertEqual(service["env"]["POSTGRES_ENABLED"], "true")
        self.assertEqual(service["env"]["POSTGRES_DATABASE"], "bazarr")

        apply_service_postgres_config(
            "bazarr", service, self.postgres, "bazarr", enabled=False
        )
        self.assertEqual(service["env"]["POSTGRES_ENABLED"], "false")
        self.assertNotIn("POSTGRES_PASSWORD", service["env"])

    def test_pulsarr_and_seerr_use_their_upstream_environment_names(self):
        pulsarr = {"env": {}}
        seerr = {"env": {}}

        apply_service_postgres_config(
            "pulsarr", pulsarr, self.postgres, "pulsarr", enabled=True
        )
        apply_service_postgres_config(
            "seerr", seerr, self.postgres, "seerr_family", enabled=True
        )

        self.assertEqual(pulsarr["env"]["dbType"], "postgres")
        self.assertEqual(pulsarr["env"]["dbName"], "pulsarr")
        self.assertEqual(seerr["env"]["DB_TYPE"], "postgres")
        self.assertEqual(seerr["env"]["DB_NAME"], "seerr_family")

    def test_infinidysk_uses_npgsql_environment_and_restores_sqlite(self):
        service = {"env": {}}
        postgres = {
            **self.postgres,
            "password": 'secret;with"quote',
        }

        apply_service_postgres_config(
            "infinidysk", service, postgres, "infinidysk", enabled=True
        )

        self.assertEqual(service["env"]["DATABASE_PROVIDER"], "postgres")
        self.assertEqual(
            service["env"]["DATABASE_CONNECTION_STRING"],
            'Host="127.0.0.1";Port="5432";Database="infinidysk";'
            'Username="DUMB user";Password="secret;with""quote"',
        )

        apply_service_postgres_config(
            "infinidysk", service, postgres, "infinidysk", enabled=False
        )
        self.assertEqual(service["env"]["DATABASE_PROVIDER"], "sqlite")
        self.assertNotIn("DATABASE_CONNECTION_STRING", service["env"])

    def test_runtime_registers_infinidysk_database_and_environment(self):
        class Config:
            def __init__(self, config_dir):
                self.config = {
                    "infinidysk": {
                        "enabled": True,
                        "postgres_enabled": True,
                        "postgres_database": "",
                        "process_name": "InfiniDysk",
                        "config_dir": config_dir,
                        "env": {},
                    },
                    "postgres": {"enabled": False, "databases": []},
                }

            def get(self, key, default=None):
                return self.config.get(key, default)

        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config(temp_dir)

            self.assertTrue(configure_service_postgres_runtime(config))
        self.assertTrue(config.config["postgres"]["enabled"])
        self.assertEqual(
            config.config["postgres"]["databases"],
            [{"name": "infinidysk", "enabled": True}],
        )
        self.assertEqual(
            config.config["infinidysk"]["env"]["DATABASE_PROVIDER"], "postgres"
        )
        self.assertIn(
            'Database="infinidysk"',
            config.config["infinidysk"]["env"]["DATABASE_CONNECTION_STRING"],
        )

    def test_early_runtime_sync_accepts_static_cutover_authorization(self):
        class Config:
            def __init__(self):
                self.config = {
                    "infinidysk": {
                        "enabled": True,
                        "postgres_enabled": True,
                        "process_name": "InfiniDysk",
                        "config_dir": "/infinidysk",
                        "env": {},
                    },
                    "postgres": {"enabled": True, "databases": []},
                }

            def get(self, key, default=None):
                return self.config.get(key, default)

        config = Config()
        with patch(
            "utils.service_postgres.validate_infinidysk_postgres_fresh_install",
            return_value=(True, None),
        ) as validate:
            self.assertTrue(
                configure_service_postgres_runtime(
                    config,
                    allow_offline_authorization=True,
                )
            )

        validate.assert_called_once_with(
            "/infinidysk",
            True,
            service=config.config["infinidysk"],
            postgres_config=config.config["postgres"],
            allow_offline_authorization=True,
        )

    def test_existing_infinidysk_sqlite_is_skipped_while_other_services_configure(
        self,
    ):
        class Config:
            def __init__(self, config_dir):
                self.config = {
                    "infinidysk": {
                        "enabled": True,
                        "postgres_enabled": True,
                        "process_name": "InfiniDysk",
                        "config_dir": config_dir,
                        "env": {"EXISTING_VALUE": "preserved"},
                    },
                    "bazarr": {
                        "enabled": True,
                        "postgres_enabled": True,
                        "process_name": "Bazarr",
                        "env": {},
                    },
                    "postgres": {"enabled": False, "databases": []},
                }

            def get(self, key, default=None):
                return self.config.get(key, default)

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "db.sqlite").write_bytes(b"existing application data")
            config = Config(temp_dir)

            self.assertTrue(configure_service_postgres_runtime(config))

        self.assertTrue(config.config["infinidysk"]["postgres_enabled"])
        self.assertEqual(
            config.config["infinidysk"]["env"],
            {"EXISTING_VALUE": "preserved"},
        )
        self.assertTrue(config.config["postgres"]["enabled"])
        self.assertEqual(
            config.config["postgres"]["databases"],
            [{"name": "bazarr", "enabled": True}],
        )
        self.assertEqual(config.config["bazarr"]["env"]["POSTGRES_ENABLED"], "true")

    def test_altmount_writes_postgres_dsn_and_restores_sqlite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "config.yaml"
            config_file.write_text(
                "database:\n  type: sqlite\n  path: /data/original.db\n",
                encoding="utf-8",
            )
            service = {
                "config_dir": temp_dir,
                "config_file": str(config_file),
            }

            apply_service_postgres_config(
                "altmount", service, self.postgres, "altmount", enabled=True
            )
            database = yaml.safe_load(config_file.read_text(encoding="utf-8"))[
                "database"
            ]
            self.assertEqual(database["type"], "postgres")
            self.assertIn("DUMB%20user:secret%2Fvalue", database["dsn"])

            apply_service_postgres_config(
                "altmount", service, self.postgres, "altmount", enabled=False
            )
            database = yaml.safe_load(config_file.read_text(encoding="utf-8"))[
                "database"
            ]
            self.assertEqual(database["type"], "sqlite")
            self.assertNotIn("dsn", database)

    def test_altmount_does_not_create_an_incomplete_first_run_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "config.yaml"
            changed = apply_service_postgres_config(
                "altmount",
                {"config_dir": temp_dir, "config_file": str(config_file)},
                self.postgres,
                "altmount",
                enabled=True,
            )

            self.assertFalse(changed)
            self.assertFalse(config_file.exists())

    def test_optional_pulsarr_starts_postgres_before_pulsarr(self):
        pulsarr = {
            "enabled": True,
            "process_name": "Pulsarr",
            "port": 3003,
            "auto_update": False,
            "postgres_enabled": False,
        }
        updater = Mock()
        api_state = Mock()
        api_state.get_status.return_value = "stopped"
        logger = Mock()
        order = Mock()

        with (
            patch.object(process_router, "_reserve_config_port"),
            patch.object(process_router.CONFIG_MANAGER, "save_config", create=True),
            patch.object(
                process_router,
                "ensure_arr_postgres_dependency_running",
                side_effect=order.ensure_postgres,
            ) as ensure_postgres,
            patch.object(
                process_router,
                "_ensure_optional_process_running",
                side_effect=order.start_pulsarr,
            ) as start_pulsarr,
        ):
            process_router._start_optional_service(
                opt_key="pulsarr",
                opt_cfg=pulsarr,
                merged_options={"postgres_enabled": True},
                used_ports={},
                updater=updater,
                api_state=api_state,
                logger=logger,
                template_config={},
            )

        self.assertTrue(pulsarr["postgres_enabled"])
        ensure_postgres.assert_called_once_with(
            "pulsarr", pulsarr, updater, api_state, logger
        )
        start_pulsarr.assert_called_once_with("Pulsarr", False, updater, api_state)
        self.assertEqual(
            [entry[0] for entry in order.mock_calls],
            ["ensure_postgres", "start_pulsarr"],
        )


if __name__ == "__main__":
    unittest.main()
