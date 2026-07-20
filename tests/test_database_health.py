import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.database_health import (
    SUPPORTED_SERVICE_KEYS,
    DatabaseHealthCollector,
    _storage_for_path,
)


def _config(config_dir, log_file, service_settings=None, mode="standard"):
    return {
        "dumb": {
            "metrics": {
                "database_health": {
                    "enabled": True,
                    "interval_sec": 15,
                    "log_tail_bytes": 16384,
                    "services": {
                        "nzbdav": {
                            "enabled": True,
                            "mode": mode,
                        }
                    },
                }
            }
        },
        "nzbdav": {
            "enabled": True,
            "process_name": "NzbDAV",
            "config_dir": config_dir,
            "log_file": log_file,
            "env": {},
            **(service_settings or {}),
        },
    }


class DatabaseHealthCollectorTests(unittest.TestCase):
    def test_monitoring_is_opt_in_and_discovers_enabled_services(self):
        collector = DatabaseHealthCollector()
        config = {
            "dumb": {"metrics": {"database_health": {"enabled": False}}},
            "nzbdav": {"enabled": True, "process_name": "NzbDAV"},
            "plex": {"enabled": False, "process_name": "Plex Media Server"},
        }

        result = collector.snapshot(config)

        self.assertFalse(result["enabled"])
        self.assertEqual(result["supported_count"], 1)
        self.assertEqual(result["services"][0]["id"], "nzbdav")
        self.assertEqual(result["services"][0]["pressure"], "disabled")

    def test_standard_mode_only_reads_file_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "db.sqlite"
            sqlite3.connect(db_path).close()
            log_path = Path(temp_dir) / "nzbdav.log"
            log_path.write_text("startup complete\n", encoding="utf-8")
            collector = DatabaseHealthCollector()

            result = collector.snapshot(_config(temp_dir, str(log_path)))
            database = result["services"][0]["databases"][0]

            self.assertTrue(database["exists"])
            self.assertFalse(database["enhanced_probe"])
            self.assertNotIn("page_count", database)
            self.assertEqual(result["services"][0]["pressure"], "healthy")

    def test_fast_snapshot_does_not_run_stale_database_probe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "db.sqlite").touch()
            log_path = Path(temp_dir) / "nzbdav.log"
            log_path.touch()
            collector = DatabaseHealthCollector()

            result = collector.snapshot(
                _config(temp_dir, str(log_path)), refresh_if_stale=False
            )

            self.assertEqual(result["services"][0]["pressure"], "collecting")
            self.assertEqual(result["services"][0]["databases"], [])
            self.assertEqual(collector._cache, {})

    def test_changed_storage_override_does_not_reuse_old_score(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "db.sqlite").touch()
            log_path = Path(temp_dir) / "nzbdav.log"
            log_path.touch()
            collector = DatabaseHealthCollector()
            config = _config(temp_dir, str(log_path))
            storage = {
                "mount_point": temp_dir,
                "fs_type": "nfs4",
                "source": "server:/data",
                "network": True,
            }
            with patch("utils.database_health._storage_for_path", return_value=storage):
                first = collector.snapshot(config)["services"][0]
                config["dumb"]["metrics"]["database_health"]["services"]["nzbdav"][
                    "ignore_network_storage"
                ] = True
                waiting = collector.snapshot(config, refresh_if_stale=False)[
                    "services"
                ][0]
                refreshed = collector.snapshot(config)["services"][0]

            self.assertEqual(first["score"], 35)
            self.assertEqual(waiting["pressure"], "collecting")
            self.assertEqual(refreshed["score"], 0)

    def test_enhanced_mode_uses_bounded_read_only_sqlite_probe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "db.sqlite"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
            connection.commit()
            connection.close()
            before = db_path.read_bytes()
            log_path = Path(temp_dir) / "nzbdav.log"
            log_path.touch()
            collector = DatabaseHealthCollector()

            result = collector.snapshot(
                _config(temp_dir, str(log_path), mode="enhanced")
            )
            database = result["services"][0]["databases"][0]

            self.assertTrue(database["enhanced_probe"])
            self.assertGreater(database["page_count"], 0)
            self.assertGreaterEqual(database["probe_ms"], 0)
            self.assertEqual(db_path.read_bytes(), before)

    def test_new_lock_log_lines_raise_pressure_without_recounting_old_lines(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "db.sqlite").touch()
            log_path = Path(temp_dir) / "nzbdav.log"
            log_path.write_text("database is locked\n", encoding="utf-8")
            now = [100.0]
            collector = DatabaseHealthCollector(clock=lambda: now[0])
            config = _config(temp_dir, str(log_path))

            first = collector.snapshot(config)["services"][0]
            now[0] = 116.0
            second = collector.snapshot(config)["services"][0]
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write("SQLITE_BUSY busy timeout\n")
            now[0] = 132.0
            third = collector.snapshot(config)["services"][0]

            self.assertEqual(first["log_signals"]["locked"], 1)
            self.assertEqual(second["log_signals"]["locked"], 1)
            self.assertEqual(third["log_signals"]["locked"], 1)
            self.assertGreaterEqual(third["log_signals"]["busy"], 1)
            self.assertIn(third["pressure"], {"high", "critical"})

    def test_history_snapshot_omits_paths_and_storage_details(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "db.sqlite").touch()
            log_path = Path(temp_dir) / "nzbdav.log"
            log_path.touch()
            collector = DatabaseHealthCollector()

            result = collector.snapshot(_config(temp_dir, str(log_path)), details=False)
            service = result["services"][0]

            self.assertNotIn("recommendation", service)
            self.assertNotIn("path", service["databases"][0])
            json.dumps(result)

    def test_network_storage_adds_pressure_and_local_storage_does_not(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "db.sqlite").touch()
            log_path = Path(temp_dir) / "nzbdav.log"
            log_path.touch()
            collector = DatabaseHealthCollector()
            storage = {
                "mount_point": temp_dir,
                "fs_type": "nfs4",
                "source": "server:/data",
                "network": True,
            }
            with patch("utils.database_health._storage_for_path", return_value=storage):
                service = collector.snapshot(_config(temp_dir, str(log_path)))[
                    "services"
                ][0]

            self.assertGreaterEqual(service["score"], 20)
            self.assertIn("local storage", service["recommendation"])

    def test_network_storage_is_scored_once_per_mount(self):
        storage = {
            "mount_point": "/config",
            "fs_type": "nfs4",
            "source": "server:/config",
            "network": True,
        }
        score, reasons = DatabaseHealthCollector._score(
            {
                "collected_at": 100,
                "log_signals": {},
                "databases": [
                    {"role": "main", "storage": storage},
                    {"role": "metrics", "storage": storage},
                ],
            }
        )

        self.assertEqual(score, 35)
        self.assertEqual(len(reasons), 1)
        self.assertIn("main, metrics", reasons[0])

    def test_network_storage_can_be_ignored_without_hiding_storage_details(self):
        storage = {
            "mount_point": "/config",
            "fs_type": "nfs4",
            "source": "server:/config",
            "network": True,
        }
        result = {
            "provider": "sqlite",
            "ignore_network_storage": True,
            "collected_at": 100,
            "log_signals": {},
            "databases": [{"role": "main", "storage": storage}],
        }

        score, reasons = DatabaseHealthCollector._score(result)

        self.assertEqual(score, 0)
        self.assertEqual(reasons, [])
        self.assertTrue(result["databases"][0]["storage"]["network"])

    def test_filesystem_capacity_and_inode_pressure_are_scored_once_per_mount(self):
        storage = {
            "mount_point": "/config",
            "fs_type": "ext4",
            "source": "/dev/example",
            "network": False,
            "used_percent": 99.0,
            "inode_used_percent": 99.0,
            "read_only": False,
        }

        score, reasons = DatabaseHealthCollector._score(
            {
                "collected_at": 100,
                "log_signals": {},
                "databases": [
                    {"role": "main", "storage": storage},
                    {"role": "logs", "storage": storage},
                ],
            }
        )

        self.assertEqual(score, 80)
        self.assertEqual(len(reasons), 2)
        self.assertTrue(any("98% full" in reason for reason in reasons))
        self.assertTrue(any("98% of its inodes" in reason for reason in reasons))

    def test_ignoring_network_storage_keeps_inode_pressure_active(self):
        storage = {
            "mount_point": "/config",
            "fs_type": "nfs4",
            "source": "server:/config",
            "network": True,
            "used_percent": 20.0,
            "inode_used_percent": 96.0,
            "read_only": False,
        }

        score, reasons = DatabaseHealthCollector._score(
            {
                "ignore_network_storage": True,
                "collected_at": 100,
                "log_signals": {},
                "databases": [{"role": "main", "storage": storage}],
            }
        )

        self.assertEqual(score, 30)
        self.assertEqual(len(reasons), 1)
        self.assertIn("inodes", reasons[0])

    def test_storage_inspection_reports_capacity_inode_and_read_only_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = _storage_for_path(temp_dir)

        self.assertIsInstance(storage.get("free_bytes"), int)
        self.assertIsInstance(storage.get("used_percent"), float)
        self.assertIn("free_inodes", storage)
        self.assertIn("inode_used_percent", storage)
        self.assertIsInstance(storage.get("read_only"), bool)

    def test_postgres_passive_recommendation_prioritizes_filesystem_pressure(self):
        result = {
            "provider": "postgresql",
            "mode": "standard",
            "pressure": "high",
            "databases": [
                {
                    "role": "main",
                    "storage": {
                        "mount_point": "/postgres_data",
                        "inode_used_percent": 96.0,
                    },
                }
            ],
        }

        recommendation = DatabaseHealthCollector._recommendation(
            result, ["Database storage has at least 95% of its inodes in use."]
        )

        self.assertIn("Free inodes", recommendation)

    def test_discovers_sql_postgres_and_custom_store_services(self):
        collector = DatabaseHealthCollector()
        config = {
            "dumb": {"metrics": {"database_health": {"enabled": False}}},
            "cli_debrid": {"enabled": True, "process_name": "CLI Debrid"},
            "decypharr": {"enabled": True, "process_name": "Decypharr"},
            "riven_backend": {"enabled": True, "process_name": "Riven Backend"},
            "zurg": {
                "instances": {
                    "RealDebrid": {
                        "enabled": True,
                        "process_name": "Zurg RealDebrid",
                    }
                }
            },
        }

        result = collector.snapshot(config)
        providers = {
            service["id"]: service["provider"] for service in result["services"]
        }

        self.assertEqual(result["supported_count"], 4)
        self.assertEqual(providers["cli_debrid"], "sqlite")
        self.assertEqual(providers["decypharr"], "append-log")
        self.assertEqual(providers["riven_backend"], "postgresql")
        self.assertEqual(providers["zurg:RealDebrid"], "zurg-state")

    def test_zurg_json_state_is_observed_without_sqlite_probe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            data_dir.mkdir()
            state_path = data_dir / "fixers.json"
            state_path.write_text('{"pending": []}\n', encoding="utf-8")
            collector = DatabaseHealthCollector()
            config = {
                "dumb": {
                    "metrics": {
                        "database_health": {
                            "enabled": True,
                            "services": {
                                "zurg:RealDebrid": {
                                    "enabled": True,
                                    "mode": "enhanced",
                                }
                            },
                        }
                    }
                },
                "zurg": {
                    "instances": {
                        "RealDebrid": {
                            "enabled": True,
                            "process_name": "Zurg RealDebrid",
                            "config_dir": temp_dir,
                        }
                    }
                },
            }

            service = collector.snapshot(config)["services"][0]
            store = service["databases"][0]

            self.assertEqual(service["provider"], "zurg-state")
            self.assertEqual(service["pressure"], "healthy")
            self.assertEqual(store["path"], str(data_dir))
            self.assertEqual(store["file_count"], 1)
            self.assertEqual(store["size_bytes"], state_path.stat().st_size)
            self.assertFalse(store["enhanced_probe"])
            self.assertIn("not a SQL provider", service["probe_notice"])

    def test_cli_debrid_discovers_each_sqlite_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for filename in ("media_items.db", "watch_history.db"):
                sqlite3.connect(Path(temp_dir) / filename).close()
            collector = DatabaseHealthCollector()
            candidate = collector._candidate(
                "cli_debrid",
                {"env": {"USER_DB_CONTENT": temp_dir}},
                None,
            )

            paths = collector._sqlite_paths(candidate)

            self.assertEqual(
                paths,
                [
                    ("media", str(Path(temp_dir) / "media_items.db")),
                    ("watch history", str(Path(temp_dir) / "watch_history.db")),
                ],
            )

    def test_altmount_detects_postgres_dsn_from_application_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(
                "database:\n"
                "  type: postgres\n"
                "  dsn: postgresql://altmount:secret@db.example/altmount_prod\n",
                encoding="utf-8",
            )
            collector = DatabaseHealthCollector()
            candidate = collector._candidate(
                "altmount", {"config_file": str(config_path)}, None
            )

            specs = collector._postgres_specs(candidate, {})

            self.assertEqual(candidate["provider"], "postgresql")
            self.assertEqual(specs[0]["name"], "altmount_prod")
            self.assertEqual(
                specs[0]["connection"]["dsn"],
                "postgresql://altmount:secret@db.example/altmount_prod",
            )

    def test_media_server_discovers_multiple_sqlite_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            data_dir.mkdir()
            sqlite3.connect(data_dir / "library.db").close()
            sqlite3.connect(data_dir / "activitylog.db").close()
            collector = DatabaseHealthCollector()
            candidate = collector._candidate("jellyfin", {"config_dir": temp_dir}, None)

            paths = collector._sqlite_paths(candidate)

            self.assertEqual(len(paths), 2)
            self.assertEqual(
                {Path(path).name for _, path in paths},
                {"library.db", "activitylog.db"},
            )

    def test_postgres_service_lists_enabled_cluster_databases(self):
        collector = DatabaseHealthCollector()
        candidate = collector._candidate(
            "postgres",
            {
                "databases": [
                    {"name": "postgres", "enabled": True},
                    {"name": "disabled", "enabled": False},
                    {"name": "zilean", "enabled": True},
                ]
            },
            None,
        )

        specs = collector._postgres_specs(candidate, {})

        self.assertEqual([spec["name"] for spec in specs], ["postgres", "zilean"])

    def test_provider_neutral_services_detect_postgres_settings(self):
        collector = DatabaseHealthCollector()
        cases = (
            (
                "pulsarr",
                {
                    "env": {
                        "dbType": "postgres",
                        "dbHost": "db.example",
                        "dbName": "pulsarr_prod",
                    }
                },
                "pulsarr_prod",
            ),
            (
                "seerr",
                {
                    "env": {
                        "DB_TYPE": "postgres",
                        "DB_HOST": "db.example",
                        "DB_NAME": "seerr_prod",
                    }
                },
                "seerr_prod",
            ),
        )

        for key, service, expected_database in cases:
            with self.subTest(key=key):
                candidate = collector._candidate(key, service, None)
                specs = collector._postgres_specs(candidate, {})
                self.assertEqual(candidate["provider"], "postgresql")
                self.assertEqual(specs[0]["name"], expected_database)

    def test_supported_inventory_includes_every_confirmed_store_service(self):
        self.assertEqual(
            SUPPORTED_SERVICE_KEYS,
            {
                "altmount",
                "bazarr",
                "cli_battery",
                "cli_debrid",
                "decypharr",
                "emby",
                "jellyfin",
                "lidarr",
                "maintainerr",
                "nzbdav",
                "pgadmin",
                "phalanx_db",
                "plex",
                "postgres",
                "profilarr",
                "prowlarr",
                "pulsarr",
                "radarr",
                "riven_backend",
                "seerr",
                "sonarr",
                "tautulli",
                "traefik_proxy_admin",
                "whisparr",
                "zilean",
                "zurg",
            },
        )


if __name__ == "__main__":
    unittest.main()
