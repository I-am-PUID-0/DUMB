import tempfile
import unittest
from pathlib import Path

import yaml

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


if __name__ == "__main__":
    unittest.main()
