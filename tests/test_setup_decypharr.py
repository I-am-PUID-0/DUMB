import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import setup


class SetupDecypharrTests(unittest.TestCase):
    def test_source_cleanup_missing_directory_is_a_noop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "missing"

            self.assertEqual(
                (True, None), setup._prepare_decypharr_source_tree(str(target))
            )
            self.assertFalse(target.exists())

    def test_source_cleanup_removes_deleted_root_go_files_preserving_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for dirname in ("pkg", "internal", "node_modules"):
                directory = root / dirname
                directory.mkdir()
                (directory / "old-source").write_text("source", encoding="utf-8")
            for filename in ("main.go", "downgrade.go", "downgrade_test.go", "go.mod"):
                (root / filename).write_text("source", encoding="utf-8")

            runtime_paths = []
            for dirname in ("cache", "db", "logs", "rclone", "downloads"):
                directory = root / dirname
                directory.mkdir()
                runtime_paths.append(directory / "state")
            runtime_paths.extend(
                root / filename
                for filename in (
                    "config.json",
                    "auth.json",
                    "decypharr",
                    "version.txt",
                    ".dumb_branch_build",
                )
            )
            for path in runtime_paths:
                path.write_text("runtime", encoding="utf-8")

            success, error = setup._prepare_decypharr_source_tree(temp_dir)

            self.assertTrue(success, error)
            self.assertIsNone(error)
            self.assertEqual([], list(root.glob("*.go")))
            for filename in ("go.mod", "pkg", "internal", "node_modules"):
                self.assertFalse((root / filename).exists())
            for path in runtime_paths:
                self.assertEqual("runtime", path.read_text(encoding="utf-8"))

    def test_source_cleanup_unlinks_go_symlinks_without_following_them(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "source"
            source_dir.mkdir()
            runtime = root / "runtime"
            runtime.write_text("runtime", encoding="utf-8")
            stale_source = source_dir / "downgrade.go"
            stale_source.symlink_to(runtime)
            dangling_source = source_dir / "old_test.go"
            dangling_source.symlink_to(root / "missing")

            success, error = setup._prepare_decypharr_source_tree(str(source_dir))

            self.assertTrue(success, error)
            self.assertFalse(stale_source.is_symlink())
            self.assertFalse(dangling_source.is_symlink())
            self.assertEqual("runtime", runtime.read_text(encoding="utf-8"))

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
