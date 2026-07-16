import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.database_health import DatabaseHealthCollector


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


if __name__ == "__main__":
    unittest.main()
