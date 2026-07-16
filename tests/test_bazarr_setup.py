import unittest
from unittest.mock import call, patch

from utils import setup


class BazarrSetupTests(unittest.TestCase):
    def test_configure_prepares_runtime_binary_and_data_directories(self):
        config = {
            "enabled": True,
            "process_name": "Bazarr",
            "port": 6767,
            "config_dir": "/opt/bazarr",
            "config_file": "/bazarr/data/config.yaml",
            "env": {"NO_UPDATE": "true"},
        }

        with (
            patch.object(setup.CONFIG_MANAGER, "get", return_value=config),
            patch.object(setup.os.path, "isfile", return_value=True),
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


if __name__ == "__main__":
    unittest.main()
