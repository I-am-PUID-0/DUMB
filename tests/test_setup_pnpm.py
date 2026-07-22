import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils import setup


class SetupPnpmTests(unittest.TestCase):
    def test_runtime_install_allows_lockfile_refresh(self):
        setup_source = Path("utils/setup.py").read_text(encoding="utf-8")

        self.assertIn('"--no-frozen-lockfile"', setup_source)
        self.assertIn('"npm_config_frozen_lockfile", "false"', setup_source)

    def test_decypharr_build_surfaces_immediate_compiler_failure(self):
        process_handler = Mock()
        process_handler.start_process.return_value = (
            False,
            "go_build failed to stay running.",
        )
        process_handler.stderr = "fatal error: fuse.h: No such file or directory"
        process_handler.stdout = ""

        with patch.object(setup, "setup_pnpm_environment", return_value=(True, None)):
            success, error = setup.build_decypharr_dev(
                process_handler,
                {"config_dir": "/decypharr", "branch": "beta"},
            )

        self.assertFalse(success)
        self.assertIn("fatal error: fuse.h: No such file or directory", error)
        self.assertEqual(3, process_handler.start_process.call_count)
        process_handler.wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
