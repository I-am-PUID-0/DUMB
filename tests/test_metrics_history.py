import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.metrics_history import MetricsHistoryWriter


class MetricsHistoryWriterTests(unittest.TestCase):
    def test_build_path_and_find_latest_index_use_metric_filename_pattern(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = MetricsHistoryWriter(temp_dir, retention_days=0)
            Path(temp_dir, "metrics-20260529-000.jsonl").write_text("{}\n")
            Path(temp_dir, "metrics-20260529-003.jsonl").write_text("{}\n")
            Path(temp_dir, "metrics-20260529-bad.jsonl").write_text("{}\n")
            Path(temp_dir, "other-20260529-999.jsonl").write_text("{}\n")

            self.assertEqual(
                writer._build_path("20260529", 7),
                os.path.join(temp_dir, "metrics-20260529-007.jsonl"),
            )
            self.assertEqual(writer._find_latest_index("20260529"), 3)

    def test_write_appends_compact_jsonl_to_current_day_file(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("utils.metrics_history.time.strftime", return_value="20260529"),
        ):
            writer = MetricsHistoryWriter(temp_dir, retention_days=0)
            writer.write({"timestamp": 1, "system": {"cpu_percent": 10}})
            writer.write({"timestamp": 2})

            path = Path(temp_dir, "metrics-20260529-000.jsonl")
            lines = path.read_text().splitlines()

        self.assertEqual(len(lines), 2)
        self.assertEqual(
            json.loads(lines[0]), {"timestamp": 1, "system": {"cpu_percent": 10}}
        )
        self.assertEqual(json.loads(lines[1]), {"timestamp": 2})

    def test_ensure_current_file_rotates_when_current_file_would_exceed_limit(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("utils.metrics_history.time.strftime", return_value="20260529"),
        ):
            writer = MetricsHistoryWriter(
                temp_dir, retention_days=0, max_file_mb=0.00001
            )
            first = writer._ensure_current_file(1)
            Path(first).write_text("x" * 20)
            second = writer._ensure_current_file(1)

        self.assertTrue(first.endswith("metrics-20260529-000.jsonl"))
        self.assertTrue(second.endswith("metrics-20260529-001.jsonl"))

    def test_prune_total_size_removes_oldest_metric_files_until_under_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_path = Path(temp_dir, "metrics-20260528-000.jsonl")
            new_path = Path(temp_dir, "metrics-20260529-000.jsonl")
            ignored_path = Path(temp_dir, "notes.txt")
            old_path.write_text("a" * 10)
            new_path.write_text("b" * 10)
            ignored_path.write_text("c" * 100)
            os.utime(old_path, (100, 100))
            os.utime(new_path, (200, 200))

            writer = MetricsHistoryWriter(
                temp_dir, retention_days=0, max_total_mb=0.000012
            )
            writer._prune_total_size()

            self.assertFalse(old_path.exists())
            self.assertTrue(new_path.exists())
            self.assertTrue(ignored_path.exists())


if __name__ == "__main__":
    unittest.main()
