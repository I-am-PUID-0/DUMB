import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils import setup


class DumbFrontendBootstrapTests(unittest.TestCase):
    def test_frontend_proxy_environment_tracks_configured_traefik_port(self):
        dumb_config = {
            "frontend": {
                "host": "0.0.0.0",
                "port": 3005,
                "env": {
                    "DMB_TRAEFIK_URL": "http://127.0.0.1:18080",
                    "DUMB_TRAEFIK_URL": "http://127.0.0.1:18080",
                },
            },
            "api_service": {"host": "127.0.0.1", "port": 8000},
        }
        traefik_config = {
            "port": 18082,
            "entrypoints": {"web": {"address": ":18082"}},
        }

        def get_config(key, default=None):
            if key == "dumb":
                return dumb_config
            if key == "traefik":
                return traefik_config
            return default

        with patch.object(setup.CONFIG_MANAGER, "get", side_effect=get_config):
            success, error = setup.dumb_frontend_setup()

        self.assertTrue(success, error)
        frontend_env = dumb_config["frontend"]["env"]
        self.assertEqual(frontend_env["DMB_TRAEFIK_URL"], "http://127.0.0.1:18082")
        self.assertEqual(frontend_env["DUMB_TRAEFIK_URL"], "http://127.0.0.1:18082")

    def test_frontend_requires_runnable_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "package.json").write_text("{}", encoding="utf-8")

            self.assertTrue(setup._needs_riven_bootstrap("dumb_frontend", str(root)))

            entrypoint = root / ".output" / "server" / "index.mjs"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_text("console.log('dmbdb')\n", encoding="utf-8")

            self.assertFalse(setup._needs_riven_bootstrap("dumb_frontend", str(root)))

    def test_missing_unpinned_frontend_bootstraps_latest_release(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = {
                "enabled": True,
                "process_name": "DUMB Frontend",
                "config_dir": temp_dir,
                "release_version_enabled": False,
                "release_version": "v1.2.0",
                "branch_enabled": False,
                "commit_sha": "",
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
                    return_value=("dumb_frontend", None),
                ),
                patch.object(setup.CONFIG_MANAGER, "get_instance", return_value=config),
                patch.object(
                    setup, "setup_release_version", side_effect=install_release
                ) as install_release,
            ):
                success, error = setup.install_project(process_handler, "DUMB Frontend")

            self.assertTrue(success, error)
            install_release.assert_called_once()
            self.assertEqual(["latest"], requested_versions)
            self.assertEqual("v1.2.0", config["release_version"])


if __name__ == "__main__":
    unittest.main()
