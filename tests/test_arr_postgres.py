import os
import tempfile
import unittest

from utils.arr_postgres import (
    ARR_POSTGRES_KEYS,
    apply_arr_postgres_config,
    arr_postgres_database_names,
    configure_arr_postgres_runtime,
)


class StubConfig:
    def __init__(self, config):
        self.config = config

    def get(self, key, default=None):
        return self.config.get(key, default)


class ArrPostgresTests(unittest.TestCase):
    def test_supported_arr_keys_include_all_servarr_postgres_apps_in_dumb(self):
        self.assertEqual(
            ARR_POSTGRES_KEYS,
            ("sonarr", "radarr", "lidarr", "prowlarr", "whisparr"),
        )

    def test_database_names_default_and_instance_override(self):
        self.assertEqual(
            arr_postgres_database_names("sonarr", "Default", {}),
            ("sonarr-main", "sonarr-log"),
        )
        self.assertEqual(
            arr_postgres_database_names("prowlarr", "Default", {}),
            ("prowlarr-main", "prowlarr-log"),
        )
        self.assertEqual(
            arr_postgres_database_names("radarr", "Movies 4K", {}),
            ("radarr_movies_4k_main", "radarr_movies_4k_log"),
        )
        self.assertEqual(
            arr_postgres_database_names(
                "radarr",
                "Movies",
                {"postgres_main_db": "custom_main", "postgres_log_db": "custom_log"},
            ),
            ("custom_main", "custom_log"),
        )

    def test_configure_runtime_enables_postgres_and_registers_databases(self):
        cfg = StubConfig(
            {
                "postgres": {
                    "enabled": False,
                    "databases": [{"name": "postgres", "enabled": True}],
                },
                "sonarr": {
                    "instances": {
                        "Default": {"enabled": True, "postgres_enabled": True}
                    }
                },
                "radarr": {
                    "instances": {
                        "Default": {"enabled": True, "postgres_enabled": False}
                    }
                },
            }
        )

        changed = configure_arr_postgres_runtime(cfg)

        self.assertTrue(changed)
        self.assertTrue(cfg.config["postgres"]["enabled"])
        self.assertIn(
            {"name": "sonarr-main", "enabled": True},
            cfg.config["postgres"]["databases"],
        )
        self.assertIn(
            {"name": "sonarr-log", "enabled": True},
            cfg.config["postgres"]["databases"],
        )
        self.assertNotIn(
            {"name": "radarr-main", "enabled": True},
            cfg.config["postgres"]["databases"],
        )

    def test_apply_config_writes_servarr_postgres_elements(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = os.path.join(temp_dir, "config.xml")
            with open(config_file, "w", encoding="utf-8") as handle:
                handle.write("<Config><Port>8989</Port></Config>")

            changed = apply_arr_postgres_config(
                "sonarr",
                "Default",
                {"postgres_enabled": True, "process_name": "Sonarr"},
                config_file,
                {
                    "user": "DUMB",
                    "password": "postgres",
                    "host": "127.0.0.1",
                    "port": 5432,
                },
            )

            self.assertTrue(changed)
            with open(config_file, "r", encoding="utf-8") as handle:
                content = handle.read()
            self.assertIn("<PostgresUser>DUMB</PostgresUser>", content)
            self.assertIn("<PostgresPassword>postgres</PostgresPassword>", content)
            self.assertIn("<PostgresHost>127.0.0.1</PostgresHost>", content)
            self.assertIn("<PostgresPort>5432</PostgresPort>", content)
            self.assertIn("<PostgresMainDb>sonarr-main</PostgresMainDb>", content)
            self.assertIn("<PostgresLogDb>sonarr-log</PostgresLogDb>", content)


if __name__ == "__main__":
    unittest.main()
