import unittest
import os
import tempfile
from unittest.mock import Mock, call, patch

import yaml

from utils import setup


class BazarrSetupTests(unittest.TestCase):
    def test_configure_prepares_runtime_binary_and_data_directories(self):
        config = {
            "enabled": True,
            "process_name": "Bazarr",
            "port": 6767,
            "config_dir": "/opt/bazarr",
            "config_file": "/bazarr/data/config/config.yaml",
            "command": [
                "/opt/bazarr/venv/bin/python",
                "/opt/bazarr/bazarr.py",
                "--config",
                "/bazarr/data",
                "--port",
                "6767",
            ],
            "env": {"NO_UPDATE": "true"},
        }

        with (
            patch.object(setup.CONFIG_MANAGER, "get", return_value=config),
            patch.object(setup.os.path, "isfile", return_value=True),
            patch.object(
                setup, "_ensure_bazarr_postgres_driver", return_value=(True, None)
            ),
            patch.object(
                setup, "_sync_bazarr_port", return_value=(False, None)
            ) as sync_port,
            patch.object(setup.os, "makedirs") as makedirs,
            patch.object(
                setup, "chown_recursive", return_value=(True, None)
            ) as chown_recursive,
        ):
            success, error = setup.setup_bazarr(configure_only=True)

        self.assertTrue(success, error)
        self.assertEqual(
            makedirs.call_args_list,
            [
                call("/opt/bazarr/bin", exist_ok=True),
                call("/bazarr/data", exist_ok=True),
            ],
        )
        self.assertEqual(
            chown_recursive.call_args_list,
            [
                call("/opt/bazarr/bin", setup.user_id, setup.group_id),
                call("/bazarr/data", setup.user_id, setup.group_id),
            ],
        )
        self.assertEqual(
            config["command"],
            [
                "/opt/bazarr/venv/bin/python",
                "/opt/bazarr/bazarr.py",
                "--config",
                "/bazarr/data",
                "--port",
                "6767",
            ],
        )
        sync_port.assert_called_once_with("/bazarr/data/config/config.yaml", 6767)

    def test_missing_postgres_driver_is_installed_from_bazarr_requirements(self):
        process_handler = Mock()
        process_handler.start_process.return_value = (True, None)
        process_handler.returncode = 0

        with (
            patch.object(setup.os.path, "isfile", return_value=True),
            patch.object(
                setup.subprocess,
                "run",
                return_value=Mock(returncode=1),
            ),
        ):
            success, error = setup._ensure_bazarr_postgres_driver(
                process_handler, "/opt/bazarr"
            )

        self.assertTrue(success, error)
        process_handler.start_process.assert_called_once_with(
            "install_requirements",
            "/opt/bazarr",
            [
                "/opt/bazarr/venv/bin/python",
                "-m",
                "pip",
                "install",
                "-r",
                "/opt/bazarr/postgres-requirements.txt",
            ],
        )
        process_handler.wait.assert_called_once_with("install_requirements")

    def test_sync_bazarr_port_preserves_existing_configuration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = os.path.join(temp_dir, "config.yaml")
            with open(config_file, "w", encoding="utf-8") as handle:
                handle.write(
                    "---\n"
                    "general:\n"
                    "  port: 6767\n"
                    "  hostname: bazarr.example.invalid\n"
                    "sonarr:\n"
                    "  enabled: true\n"
                )
            os.chmod(config_file, 0o640)

            changed, error = setup._sync_bazarr_port(config_file, 6780)

            self.assertTrue(changed, error)
            self.assertIsNone(error)
            with open(config_file, "r", encoding="utf-8") as handle:
                updated = yaml.safe_load(handle)
            self.assertEqual(updated["general"]["port"], 6780)
            self.assertEqual(updated["general"]["hostname"], "bazarr.example.invalid")
            self.assertTrue(updated["sonarr"]["enabled"])
            self.assertEqual(os.stat(config_file).st_mode & 0o777, 0o640)

    def test_sync_bazarr_port_defers_until_config_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = os.path.join(temp_dir, "config.yaml")

            changed, error = setup._sync_bazarr_port(config_file, 6780)

            self.assertFalse(changed)
            self.assertIsNone(error)
            self.assertFalse(os.path.exists(config_file))


if __name__ == "__main__":
    unittest.main()
