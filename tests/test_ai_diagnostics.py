import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from utils.ai_diagnostics import (
    DiagnosticEventStore,
    build_runtime_comparison,
    collect_nzbdav_diagnostics,
    record_config_change,
    record_diagnostic_event,
    scan_retained_logs,
)


class _HistoryManager:
    def __init__(self, items):
        self.items = items

    def read(self, **_kwargs):
        return self.items, False


class AiDiagnosticsTests(unittest.TestCase):
    def test_event_recording_is_best_effort_without_debug_logger(self):
        logger_without_debug = object()

        with patch(
            "utils.ai_diagnostics.DiagnosticEventStore",
            side_effect=sqlite3.OperationalError("readonly database"),
        ):
            record_config_change(
                {"workers": 1},
                {"workers": 2},
                process_name="NzbDAV",
                actor=None,
                source="test",
                logger=logger_without_debug,
            )
            record_diagnostic_event(
                "restart",
                "Restart requested",
                process_name="NzbDAV",
                actor=None,
                logger=logger_without_debug,
            )

    def test_event_store_preserves_change_structure_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DiagnosticEventStore(str(Path(temp_dir) / "events.sqlite"))

            changed = store.record_config_change(
                {"workers": 1, "api_key": "old-secret"},
                {"workers": 4, "api_key": "new-secret"},
                process_name="NzbDAV",
                actor="private-user@example.invalid",
                source="test",
            )
            events = store.list(process_name="NzbDAV")

        self.assertEqual(changed, 2)
        self.assertEqual(events[0]["actor"], "authenticated")
        changes = events[0]["details"]["changes"]
        self.assertIsInstance(changes, list)
        secret_change = next(item for item in changes if item["path"] == "api_key")
        self.assertEqual(secret_change["before"], "[REDACTED]")
        self.assertEqual(secret_change["after"], "[REDACTED]")

    def test_retained_log_scan_is_bounded_and_reports_new_errors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "NzbDAV.log"
            now = datetime.now(timezone.utc)
            stamp = now.isoformat()
            content = (
                f"{stamp} - INFO - Application started\n"
                f"{stamp} - ERROR - Playback fetch failed token=private\n"
            )
            padding = "x" * (2 * 1024 * 1024)
            log_path.write_text(f"{padding}\n{content}", encoding="utf-8")

            result = scan_retained_logs(
                log_path,
                since=now.timestamp() - 60,
                until=now.timestamp() + 60,
                question="playback failures",
                max_scan_mb=1,
            )

        self.assertLessEqual(result["coverage"]["bytes_scanned"], 1024 * 1024)
        self.assertTrue(result["coverage"]["partial_file_scanned"])
        self.assertEqual(result["levels"]["error"], 1)
        self.assertNotIn("private", json.dumps(result["excerpts"]))

    def test_runtime_comparison_reports_cpu_change(self):
        items = [
            {
                "timestamp": 100.0,
                "dumb_managed": [
                    {"name": "NzbDAV", "pid": 1, "cpu_percent": 10, "rss": 100}
                ],
            },
            {
                "timestamp": 200.0,
                "dumb_managed": [
                    {"name": "NzbDAV", "pid": 2, "cpu_percent": 20, "rss": 150}
                ],
            },
        ]

        result = build_runtime_comparison(
            _HistoryManager(items),
            "NzbDAV",
            since=150,
            until=250,
            comparison_since=50,
            comparison_until=150,
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["changes"]["cpu_percent_average_percent"], 100.0)
        self.assertEqual(result["current"]["restart_indications"], 0)

    def test_nzbdav_collector_compares_metrics_and_reads_worker_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            now = 1_800_000_000.0
            current_at = int((now - 60) * 1000)
            baseline_at = int((now - 3660) * 1000)
            self._create_nzbdav_config(base / "db.sqlite")
            self._create_nzbdav_metrics(
                base / "metrics.sqlite",
                current_at,
                baseline_at,
            )
            windows = {
                "current": {"since": now - 3600, "until": now},
                "baseline": {"since": now - 7200, "until": now - 3600},
            }

            result = collect_nzbdav_diagnostics(
                {"config_dir": str(base)},
                windows=windows,
                log_scan=None,
            )

        self.assertTrue(result["available"])
        self.assertEqual(result["config"]["queue_worker_count"], 4)
        self.assertEqual(
            result["metrics"]["current"]["segment_fetches"]["count"],
            2,
        )
        self.assertEqual(
            result["metrics"]["current"]["segment_fetches"]["missing_percent"],
            50.0,
        )
        self.assertIn("segment_fetches", result["metrics"]["changes"])

    @staticmethod
    def _create_nzbdav_config(path):
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE ConfigItems (ConfigName TEXT, ConfigValue TEXT)"
            )
            connection.executemany(
                "INSERT INTO ConfigItems VALUES (?, ?)",
                [
                    ("queue.worker-count", "4"),
                    ("usenet.max-queue-connections", "40"),
                    (
                        "usenet.providers",
                        json.dumps(
                            {
                                "TotalPooledConnections": 40,
                                "Providers": [{}, {}],
                            }
                        ),
                    ),
                ],
            )

    @staticmethod
    def _create_nzbdav_metrics(path, current_at, baseline_at):
        with sqlite3.connect(path) as connection:
            connection.execute("""
                CREATE TABLE SegmentFetches (
                    At INTEGER,
                    Status INTEGER,
                    Retries INTEGER,
                    DurationMs INTEGER,
                    ReadSessionId TEXT
                )
                """)
            connection.execute("""
                CREATE TABLE ReadSessions (
                    StartedAt INTEGER,
                    BytesServed INTEGER,
                    BytesFetched INTEGER,
                    DurationMs INTEGER,
                    EndReason INTEGER
                )
                """)
            connection.executemany(
                "INSERT INTO SegmentFetches VALUES (?, ?, ?, ?, ?)",
                [
                    (baseline_at, 0, 0, 100, "baseline"),
                    (current_at, 0, 1, 150, "current"),
                    (current_at, 1, 2, 300, "current"),
                ],
            )
            connection.executemany(
                "INSERT INTO ReadSessions VALUES (?, ?, ?, ?, ?)",
                [
                    (baseline_at, 1000, 1000, 1000, 0),
                    (current_at, 2000, 2000, 2000, 0),
                ],
            )


if __name__ == "__main__":
    unittest.main()
