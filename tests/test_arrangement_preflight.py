"""Filesystem preflight must not walk remote aliases or overstate access."""

import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from utils.arrangement_preflight import _probe


class StoragePathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o711)

    def no_remote_metadata(self, remote, path):
        original = Path.lstat
        touched = []

        def guarded(candidate, *args, **kwargs):
            touched.append(str(candidate))
            if str(candidate) == str(remote) or str(candidate).startswith(
                str(remote) + "/"
            ):
                raise AssertionError("remote metadata touched")
            return original(candidate, *args, **kwargs)

        with (
            patch("utils.arrangement_preflight.Path.lstat", guarded),
            patch(
                "utils.arrangement_preflight.os.statvfs",
                side_effect=AssertionError("remote statvfs"),
            ),
            patch(
                "utils.arrangement_preflight.os.path.realpath",
                side_effect=AssertionError("unbounded realpath"),
            ),
        ):
            result = _probe(
                str(path),
                12345,
                12345,
                False,
                [("/", "overlay"), (str(remote), "fuse.rclone")],
            )
        self.assertIsNone(result["accessible"])
        self.assertEqual(result["filesystem"], "fuse.rclone")
        return touched

    def test_symlink_alias_child_mount_is_classified_before_metadata(self):
        real = self.root / "real"
        real.mkdir(mode=0o755)
        (real / "remote").mkdir()
        (self.root / "alias").symlink_to(real, target_is_directory=True)
        touched = self.no_remote_metadata(
            real / "remote", self.root / "alias/remote/library"
        )
        self.assertNotIn(str(self.root / "alias/remote"), touched)

    def test_relative_symlink_and_dotdot_keep_canonical_mount_identity(self):
        real = self.root / "real"
        (real / "inner").mkdir(parents=True, mode=0o755)
        (real / "remote").mkdir()
        (self.root / "alias").symlink_to("real/inner", target_is_directory=True)
        self.no_remote_metadata(real / "remote", self.root / "alias/../remote/library")

    def test_absolute_symlink_to_remote_never_traverses_target(self):
        remote = self.root / "remote"
        remote.mkdir()
        (self.root / "alias").symlink_to(
            str(remote / "nested"), target_is_directory=True
        )
        self.no_remote_metadata(remote, self.root / "alias/library")

    def test_execute_only_ancestors_allow_writable_final_directory(self):
        ancestor = self.root / "searchable"
        ancestor.mkdir(mode=0o711)
        final = ancestor / "config"
        final.mkdir()
        final.chmod(0o777)
        result = _probe(str(final), 12345, 12345, True, [("/", "overlay")])
        self.assertTrue(result["present"])
        self.assertTrue(result["accessible"])

    def test_readonly_filesystem_never_reports_writable_even_for_root(self):
        final = self.root / "config"
        final.mkdir(mode=0o777)
        capacity = types.SimpleNamespace(
            f_bavail=100000, f_frsize=4096, f_flag=os.ST_RDONLY
        )
        with patch("utils.arrangement_preflight.os.statvfs", return_value=capacity):
            result = _probe(str(final), 0, 0, True, [("/", "overlay")])
        self.assertFalse(result["accessible"])
        self.assertIn("read-only", result["reason"])

    def test_missing_directory_uses_nearest_writable_ancestor_without_creating_it(self):
        self.root.chmod(0o777)
        absent = self.root / "missing/child"
        result = _probe(str(absent), 12345, 12345, True, [("/", "overlay")])
        self.assertFalse(result["present"])
        self.assertTrue(result["accessible"])
        self.assertFalse((self.root / "missing").exists())

    def test_symlink_cycle_has_a_bounded_metadata_budget(self):
        (self.root / "loop").symlink_to("loop", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            _probe(str(self.root / "loop/child"), 0, 0, False, [("/", "overlay")])
