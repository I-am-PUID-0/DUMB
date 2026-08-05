import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import setup


class SetupDecypharrTests(unittest.TestCase):
    def test_runtime_ownership_repairs_mismatched_mutable_tree(self):
        user_id = os.getuid()
        group_id = os.getgid()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for dirname in ("cache", "db", "logs", "rclone"):
                directory = root / dirname
                directory.mkdir()
                (directory / "state").write_text("state", encoding="utf-8")
            for filename in (
                "auth.json",
                "config.json",
                "config_old.json",
                "decypharr",
                "version.txt",
            ):
                (root / filename).write_text("data", encoding="utf-8")

            real_stat = os.stat
            mismatched_path = str(root / "db")
            mismatch_calls = 0

            def stat_with_one_mismatch(path, *args, **kwargs):
                nonlocal mismatch_calls
                result = real_stat(path, *args, **kwargs)
                if str(path) == mismatched_path and mismatch_calls < 2:
                    mismatch_calls += 1
                    values = list(result)
                    values[4] = user_id + 1
                    values[5] = group_id + 1
                    return os.stat_result(values)
                return result

            with (
                patch.object(setup.os, "stat", side_effect=stat_with_one_mismatch),
                patch.object(setup, "chown_single") as chown_single,
                patch.object(setup, "_chown_recursive_if_needed") as recursive,
            ):
                success, error = setup._normalize_decypharr_runtime_ownership(
                    temp_dir, user_id, group_id
                )

        self.assertTrue(success)
        self.assertIsNone(error)
        self.assertEqual(5, chown_single.call_count)
        recursive.assert_any_call(mismatched_path, user_id, group_id, force=True)


if __name__ == "__main__":
    unittest.main()
