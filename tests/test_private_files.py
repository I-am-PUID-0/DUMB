import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from utils.private_files import atomic_write_private_text, read_private_text


class PrivateFileTests(unittest.TestCase):
    def test_atomic_write_uses_private_mode_and_replaces_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "service.conf"
            path.write_text("old", encoding="utf-8")
            path.chmod(0o644)

            atomic_write_private_text(path, "credential=value\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "credential=value\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                (path.stat().st_uid, path.stat().st_gid),
                (os.geteuid(), os.getegid()),
            )
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_atomic_write_replaces_symlink_without_touching_its_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.conf"
            outside.write_text("keep", encoding="utf-8")
            path = root / "service.conf"
            path.symlink_to(outside)

            atomic_write_private_text(path, "replacement")

            self.assertFalse(path.is_symlink())
            self.assertEqual(path.read_text(encoding="utf-8"), "replacement")
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class PrivateReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="private-read-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "ownership.json"
        atomic_write_private_text(self.path, '{"owned":true}\n')

    def test_safe_read_preserves_private_record_and_supports_parent_alias(self):
        alias = self.root / "managed-app"
        alias.symlink_to(self.root, target_is_directory=True)
        before = self.path.stat()
        self.assertEqual(read_private_text(alias / self.path.name), '{"owned":true}\n')
        self.assertEqual(self.path.stat().st_ino, before.st_ino)
        self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_group_or_world_readable_records_are_rejected_without_rewriting(self):
        for mode in (0o644, 0o640, 0o660):
            with self.subTest(mode=oct(mode)):
                self.path.chmod(mode)
                with self.assertRaisesRegex(PermissionError, "private regular file"):
                    read_private_text(self.path)
                self.assertEqual(self.path.read_text(), '{"owned":true}\n')
                self.assertEqual(self.path.stat().st_mode & 0o777, mode)

    def test_hardlinked_record_is_rejected_without_forgetting_or_changing_it(self):
        second = self.root / "second.json"
        second.hardlink_to(self.path)
        with self.assertRaises(PermissionError):
            read_private_text(self.path)
        self.assertEqual(self.path.stat().st_nlink, 2)
        self.assertEqual(second.read_text(), '{"owned":true}\n')

    def test_leaf_symlinks_directories_and_fifos_are_rejected_without_blocking(self):
        link = self.root / "link.json"
        link.symlink_to(self.path)
        directory = self.root / "directory"
        directory.mkdir()
        fifo = self.root / "pipe"
        os.mkfifo(fifo, 0o600)
        for path in (link, directory, fifo):
            with self.subTest(path=path.name):
                with self.assertRaises((OSError, PermissionError)):
                    read_private_text(path)
        self.assertEqual(self.path.read_text(), '{"owned":true}\n')

    def test_foreign_file_owner_requires_explicit_trusted_service_uid(self):
        before = self.path.stat()
        foreign = before.st_uid + 10000
        fields = (
            "st_mode",
            "st_nlink",
            "st_size",
            "st_dev",
            "st_ino",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        snapshot = SimpleNamespace(
            **{key: getattr(before, key) for key in fields}, st_uid=foreign
        )
        with patch("utils.private_files.os.fstat", return_value=snapshot):
            with self.assertRaises(PermissionError):
                read_private_text(self.path)
            self.assertEqual(
                read_private_text(self.path, owner_ids=(foreign,)), '{"owned":true}\n'
            )

    def test_application_directory_owner_remains_a_compatible_trusted_owner(self):
        before = self.path.stat()
        with patch(
            "utils.private_files.os.geteuid", return_value=before.st_uid + 10000
        ):
            self.assertEqual(read_private_text(self.path), '{"owned":true}\n')

    def test_read_is_bounded_and_missing_records_still_raise_missing(self):
        with self.assertRaises(PermissionError):
            read_private_text(self.path, max_bytes=3)
        with self.assertRaises(FileNotFoundError):
            read_private_text(self.root / "missing.json")

    def test_permissions_changed_after_open_are_detected(self):
        original = os.fdopen

        def change_mode(descriptor, *args, **kwargs):
            self.path.chmod(0o644)
            return original(descriptor, *args, **kwargs)

        with patch("utils.private_files.os.fdopen", side_effect=change_mode):
            with self.assertRaisesRegex(PermissionError, "changed while"):
                read_private_text(self.path)

    def test_path_replaced_after_open_is_detected(self):
        original = os.fdopen
        replacement = self.root / "replacement.json"
        atomic_write_private_text(replacement, '{"foreign":true}\n')

        def replace_path(descriptor, *args, **kwargs):
            replacement.replace(self.path)
            return original(descriptor, *args, **kwargs)

        with patch("utils.private_files.os.fdopen", side_effect=replace_path):
            with self.assertRaisesRegex(PermissionError, "changed while"):
                read_private_text(self.path)
        self.assertEqual(self.path.read_text(), '{"foreign":true}\n')


if __name__ == "__main__":
    unittest.main()
