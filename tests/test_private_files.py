import os
import tempfile
import unittest
from pathlib import Path

from utils.private_files import atomic_write_private_text


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


if __name__ == "__main__":
    unittest.main()
