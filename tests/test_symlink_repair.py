import json
import os
import sys
import tempfile
import types
import unittest
import warnings
from pathlib import Path


class _Logger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


config_loader = types.ModuleType("utils.config_loader")
config_loader.CONFIG_MANAGER = types.SimpleNamespace(get=lambda *args, **kwargs: {})
sys.modules["utils.config_loader"] = config_loader

global_logger = types.ModuleType("utils.global_logger")
global_logger.logger = _Logger()
sys.modules["utils.global_logger"] = global_logger

from utils import symlink_repair


class SymlinkRepairHelperTests(unittest.TestCase):
    def test_rewrite_target_matches_exact_and_nested_prefixes(self):
        rules = [
            symlink_repair.RewriteRule("/old/root/", "/new/root/"),
            symlink_repair.RewriteRule("/unused", "/also-unused"),
        ]

        self.assertEqual(
            symlink_repair._rewrite_target("/old/root", rules)[0],
            "/new/root",
        )
        self.assertEqual(
            symlink_repair._rewrite_target("/old/root/movie/file.mkv", rules)[0],
            "/new/root/movie/file.mkv",
        )
        unchanged, rule = symlink_repair._rewrite_target("/other/file.mkv", rules)
        self.assertEqual(unchanged, "/other/file.mkv")
        self.assertIsNone(rule)

    def test_collect_symlink_paths_reports_missing_roots_and_nested_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "root")
            nested = root / "nested"
            nested.mkdir(parents=True)
            target = Path(temp_dir, "target.mkv")
            target.write_text("media")
            link = nested / "movie.mkv"
            link.symlink_to(target)

            paths, missing = symlink_repair._collect_symlink_paths(
                [str(root), str(Path(temp_dir, "missing"))]
            )

        self.assertEqual(paths, [str(link)])
        self.assertEqual(len(missing), 1)

    def test_repair_symlinks_dry_run_reports_changes_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "links")
            root.mkdir()
            link = root / "movie.mkv"
            link.symlink_to("/old/root/movie.mkv")

            report = symlink_repair.repair_symlinks(
                [str(root)],
                [{"from_prefix": "/old/root", "to_prefix": "/new/root"}],
                dry_run=True,
            )

            self.assertEqual(os.readlink(link), "/old/root/movie.mkv")

        self.assertEqual(report["changed"], 1)
        self.assertEqual(report["changes"][0]["new_target"], "/new/root/movie.mkv")
        self.assertEqual(report["skipped_unchanged"], 0)

    def test_backup_manifest_records_valid_and_broken_symlinks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "links")
            root.mkdir()
            target = Path(temp_dir, "target.mkv")
            target.write_text("media")
            good = root / "good.mkv"
            broken = root / "broken.mkv"
            good.symlink_to(target)
            broken.symlink_to(Path(temp_dir, "missing.mkv"))
            manifest_path = Path(temp_dir, "snapshot.json")

            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                report = symlink_repair.backup_symlink_manifest(
                    [str(root)], str(manifest_path), include_broken=False
                )
            manifest = json.loads(manifest_path.read_text())

        self.assertEqual(report["scanned_symlinks"], 2)
        self.assertEqual(report["recorded_entries"], 1)
        self.assertEqual(report["skipped_broken"], 1)
        self.assertEqual(manifest["entries"][0]["link_path"], str(good))

    def test_preview_and_restore_symlink_manifest_handle_existing_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            link = Path(temp_dir, "links", "movie.mkv")
            target = Path(temp_dir, "target.mkv")
            target.write_text("media")
            manifest_path = Path(temp_dir, "snapshot.json")
            manifest_path.write_text(
                json.dumps(
                    {
                        "created_at": "2026-05-29T00:00:00Z",
                        "entries": [
                            {"link_path": str(link), "target": str(target)},
                            {"link_path": "", "target": ""},
                        ],
                    }
                )
            )

            preview = symlink_repair.preview_symlink_manifest_restore(
                str(manifest_path)
            )
            restore_report = symlink_repair.restore_symlink_manifest(
                str(manifest_path), dry_run=False
            )
            second_preview = symlink_repair.preview_symlink_manifest_restore(
                str(manifest_path)
            )

            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), str(target))

        self.assertEqual(preview["projected_restored"], 1)
        self.assertEqual(preview["projected_skipped_invalid_entries"], 1)
        self.assertEqual(restore_report["restored"], 1)
        self.assertEqual(restore_report["skipped_invalid_entries"], 1)
        self.assertEqual(second_preview["projected_skipped_unchanged"], 1)


if __name__ == "__main__":
    unittest.main()
