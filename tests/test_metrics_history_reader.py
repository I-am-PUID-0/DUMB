import json
import tempfile
import unittest
from pathlib import Path

from utils import metrics_history_reader


class MetricsHistoryReaderTests(unittest.TestCase):
    def test_parse_history_date_accepts_only_expected_metric_names(self):
        self.assertEqual(
            metrics_history_reader._parse_history_date("metrics-20260529-000.jsonl"),
            "20260529",
        )
        self.assertIsNone(
            metrics_history_reader._parse_history_date("metrics-2026052-000.jsonl")
        )
        self.assertIsNone(
            metrics_history_reader._parse_history_date("metrics-20260529-a.jsonl")
        )
        self.assertIsNone(
            metrics_history_reader._parse_history_date("other-20260529-000.jsonl")
        )

    def test_read_history_skips_invalid_lines_filters_since_and_sorts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "metrics-20260529-000.jsonl"
            path.write_text(
                "not-json\n"
                + json.dumps({"timestamp": 300, "value": "latest"})
                + "\n"
                + json.dumps({"timestamp": 100, "value": "old"})
                + "\n"
                + json.dumps({"timestamp": 200, "value": "kept"})
                + "\n"
                + json.dumps({"value": "missing timestamp"})
                + "\n"
            )

            items, truncated = metrics_history_reader.read_history(
                temp_dir, since=150, full=False, limit=10
            )

        self.assertFalse(truncated)
        self.assertEqual([item["value"] for item in items], ["kept", "latest"])

    def test_read_history_reports_truncation_when_limit_reached(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "metrics-20260529-000.jsonl"
            path.write_text(
                "".join(json.dumps({"timestamp": ts}) + "\n" for ts in (100, 200, 300))
            )

            items, truncated = metrics_history_reader.read_history(
                temp_dir, since=0, full=True, limit=2
            )

        self.assertTrue(truncated)
        self.assertEqual(len(items), 2)

    def test_downsample_history_items_buckets_and_preserves_latest_point(self):
        items = [
            {"timestamp": 0, "value": "a"},
            {"timestamp": 5, "value": "b"},
            {"timestamp": 10, "value": "c"},
            {"timestamp": 20, "value": "d"},
            {"timestamp": 30, "value": "e"},
        ]

        bucketed = metrics_history_reader._downsample_history_items(
            items, bucket_seconds=10
        )
        limited = metrics_history_reader._downsample_history_items(items, max_points=2)

        self.assertEqual([item["value"] for item in bucketed], ["b", "c", "d", "e"])
        self.assertEqual(limited[-1]["value"], "e")
        self.assertLessEqual(len(limited), 3)

    def test_build_rate_series_handles_resets_and_bad_timestamps(self):
        self.assertEqual(
            metrics_history_reader._build_rate_series(
                [100, 160, 120, 180], [0, 10, 20, 20]
            ),
            [None, 6.0, None, None],
        )

    def test_compact_and_series_output_keep_expected_metric_shape(self):
        items = [
            {
                "timestamp": 1,
                "system": {
                    "cpu_percent": 10,
                    "cpu_count": 4,
                    "mem": {"percent": 50, "extra": "drop"},
                    "disk": {"percent": 70},
                    "inode": {"percent": 80, "extra": "drop"},
                    "disk_io": {"read_bytes": 100, "write_bytes": 200},
                    "net_io": {"sent_bytes": 300, "recv_bytes": 400},
                },
                "dumb_managed": [
                    {"name": "prowlarr", "pid": 123, "cpu_percent": 1.5, "rss": 2048}
                ],
                "database_health": {
                    "enabled": True,
                    "services": [{"id": "prowlarr:Default", "score": 20}],
                },
            },
            {
                "timestamp": 2,
                "system": {
                    "cpu_percent": 20,
                    "mem": {"percent": 60},
                    "disk": {"percent": 71},
                    "inode": {"percent": 81},
                    "disk_io": {"read_bytes": 160, "write_bytes": 260},
                    "net_io": {"sent_bytes": 330, "recv_bytes": 460},
                },
            },
        ]

        compact = metrics_history_reader.compact_history_items(items)
        series = metrics_history_reader.build_history_series(items)

        self.assertEqual(compact[0]["system"]["mem"], {"percent": 50})
        self.assertEqual(compact[0]["system"]["inode"], {"percent": 80})
        self.assertEqual(compact[0]["dumb_managed"][0]["name"], "prowlarr")
        self.assertEqual(
            compact[0]["database_health"]["services"][0]["id"],
            "prowlarr:Default",
        )
        self.assertEqual(series["cpu"], [10, 20])
        self.assertEqual(series["inode"], [80, 81])
        self.assertEqual(series["disk_read_rate"], [None, 60.0])
        self.assertEqual(series["net_recv_rate"], [None, 60.0])


if __name__ == "__main__":
    unittest.main()
