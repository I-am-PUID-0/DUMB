import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import setup


class ArchiveRecoveryClearTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "service"
        self.root.mkdir()
        self.runtime = self.root / "runtime.py"
        self.runtime.write_text("working", encoding="utf-8")
        self.recovery = self.root / (".dumb-overlay-" + "a" * 32)
        self.backup = self.recovery / "backup" / "runtime.py"
        self.backup.parent.mkdir(parents=True)
        self.backup.write_text("previous", encoding="utf-8")

    def assert_recovery_untouched(self):
        self.assertEqual("working", self.runtime.read_text(encoding="utf-8"))
        self.assertEqual("previous", self.backup.read_text(encoding="utf-8"))

    def test_direct_clear_refuses_retained_archive_recovery(self):
        token = setup._INSTALL_CLEAR_SCOPE.set(None)
        try:
            success, error = setup.clear_directory(str(self.root))
        finally:
            setup._INSTALL_CLEAR_SCOPE.reset(token)

        self.assertFalse(success)
        self.assertIn(str(self.recovery), error)
        self.assert_recovery_untouched()

    def test_deferred_clear_refuses_recovery_before_snapshot(self):
        transactions = []
        token = setup._INSTALL_CLEAR_SCOPE.set(transactions)
        try:
            with patch.object(setup.DeferredClearTransaction, "capture") as capture:
                success, error = setup.clear_directory(str(self.root))
        finally:
            setup._INSTALL_CLEAR_SCOPE.reset(token)

        self.assertFalse(success)
        self.assertIn(str(self.recovery), error)
        capture.assert_not_called()
        self.assertEqual([], transactions)
        self.assert_recovery_untouched()

    def test_excluding_recovery_does_not_allow_runtime_clear(self):
        token = setup._INSTALL_CLEAR_SCOPE.set(None)
        try:
            success, error = setup.clear_directory(
                str(self.root), exclude_dirs=[str(self.recovery)]
            )
        finally:
            setup._INSTALL_CLEAR_SCOPE.reset(token)

        self.assertFalse(success)
        self.assertIn(str(self.recovery), error)
        self.assert_recovery_untouched()

    def test_configured_root_symlink_preserves_recovery(self):
        link = Path(self.temporary.name) / "service-link"
        link.symlink_to(self.root, target_is_directory=True)
        token = setup._INSTALL_CLEAR_SCOPE.set(None)
        try:
            success, error = setup.clear_directory(str(link))
        finally:
            setup._INSTALL_CLEAR_SCOPE.reset(token)

        self.assertFalse(success)
        self.assertIn(str(self.recovery), error)
        self.assertTrue(link.is_symlink())
        self.assert_recovery_untouched()

    def test_failed_recovery_inspection_prevents_clear(self):
        token = setup._INSTALL_CLEAR_SCOPE.set(None)
        try:
            with patch.object(
                setup,
                "archive_overlay_recovery_paths",
                side_effect=PermissionError("inspection denied"),
            ):
                success, error = setup.clear_directory(str(self.root))
        finally:
            setup._INSTALL_CLEAR_SCOPE.reset(token)

        self.assertFalse(success)
        self.assertIn("inspection denied", error)
        self.assert_recovery_untouched()

    def test_normal_clear_still_removes_source(self):
        self.backup.unlink()
        self.backup.parent.rmdir()
        self.recovery.rmdir()
        hidden = self.root / ".git"
        hidden.mkdir()
        (hidden / "HEAD").write_text("source metadata", encoding="utf-8")
        token = setup._INSTALL_CLEAR_SCOPE.set(None)
        try:
            success, error = setup.clear_directory(str(self.root))
        finally:
            setup._INSTALL_CLEAR_SCOPE.reset(token)

        self.assertTrue(success, error)
        self.assertEqual([], list(self.root.iterdir()))


if __name__ == "__main__":
    unittest.main()
