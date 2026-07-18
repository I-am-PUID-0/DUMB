import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils import setup


class RivenFrontendSetupTests(unittest.TestCase):
    def test_runtime_requires_runnable_build_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "package.json").write_text("{}", encoding="utf-8")

            self.assertFalse(setup._riven_frontend_runtime_ready(str(root)))
            self.assertTrue(setup._needs_riven_bootstrap("riven_frontend", str(root)))

            (root / "build").mkdir()
            self.assertFalse(setup._riven_frontend_runtime_ready(str(root)))

            (root / "build" / "index.js").write_text(
                "console.log('riven')\n", encoding="utf-8"
            )
            self.assertTrue(setup._riven_frontend_runtime_ready(str(root)))
            self.assertFalse(setup._needs_riven_bootstrap("riven_frontend", str(root)))

    def test_install_repairs_source_tree_with_missing_build(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "package.json").write_text("{}", encoding="utf-8")
            config = {
                "enabled": True,
                "process_name": "Riven Frontend",
                "config_dir": str(root),
                "release_version_enabled": False,
                "release_version": "v0.17.0",
                "branch_enabled": False,
                "env": {},
            }
            process_handler = Mock()
            process_handler.setup_tracker = set()
            process_handler.setup_tracker_lock = threading.Lock()
            requested_versions = []

            def install_release(_handler, release_config, _process_name, _key):
                requested_versions.append(release_config["release_version"])
                return True, None

            with (
                patch.object(
                    setup.CONFIG_MANAGER,
                    "find_key_for_process",
                    return_value=("riven_frontend", None),
                ),
                patch.object(setup.CONFIG_MANAGER, "get_instance", return_value=config),
                patch.object(
                    setup, "setup_release_version", side_effect=install_release
                ) as install_release,
            ):
                success, error = setup.install_project(
                    process_handler, "Riven Frontend"
                )

            self.assertTrue(success, error)
            install_release.assert_called_once()
            self.assertEqual(["latest"], requested_versions)
            self.assertEqual("v0.17.0", config["release_version"])


if __name__ == "__main__":
    unittest.main()
