import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import nzbdav_db, nzbdav_settings, riven_settings, setup


class _ApiState:
    @staticmethod
    def get_status(_process_name):
        return "stopped"


class _ConfigManager:
    def __init__(self, rclone_config):
        self.config = {"rclone": rclone_config}
        self._rclone_config = rclone_config

    def get(self, key, default=None):
        if key == "rclone":
            return self._rclone_config
        if key == "nzbdav":
            return {"backend_port": 8080, "env": {}}
        return default

    def save_config(self, _process_name=None):
        return None


class RcloneSetupTests(unittest.TestCase):
    def test_saved_dir_cache_time_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            instance = {
                "enabled": True,
                "process_name": "rclone w/ NzbDAV",
                "log_level": "INFO",
                "key_type": "nzbdav",
                "zurg_enabled": False,
                "decypharr_enabled": False,
                "mount_dir": str(root / "mounts"),
                "mount_name": "nzbdav",
                "cache_dir": str(root / "cache"),
                "config_dir": str(root / "config"),
                "config_file": str(root / "config" / "rclone.config"),
                "zurg_config_file": "",
                "command": [
                    "rclone",
                    "mount",
                    "nzbdav:",
                    str(root / "mounts" / "nzbdav"),
                    "--dir-cache-time=20s",
                ],
            }
            manager = _ConfigManager({"instances": {"NzbDAV": instance}})

            with (
                patch.object(setup, "CONFIG_MANAGER", manager),
                patch.object(setup, "fuse_config", return_value=(True, None)),
                patch("utils.dependencies.get_api_state", return_value=_ApiState()),
                patch.object(nzbdav_db, "get_config_value", return_value=None),
                patch.object(riven_settings, "parse_config_keys"),
                patch.object(
                    nzbdav_settings,
                    "sync_nzbdav_rclone_rc",
                    return_value=(True, None),
                ) as sync_rc,
                patch.object(setup, "chown_recursive", return_value=(True, None)),
                patch.object(setup, "_is_rclone_rc_port_available", return_value=True),
            ):
                success, error = setup.rclone_setup()

        self.assertTrue(success, error)
        self.assertIn("--dir-cache-time=20s", instance["command"])
        self.assertNotIn("--dir-cache-time=10s", instance["command"])
        self.assertIn("--rc", instance["command"])
        self.assertIn("--rc-no-auth", instance["command"])
        self.assertIn("--rc-addr", instance["command"])
        self.assertEqual(
            "127.0.0.1:5572",
            instance["command"][instance["command"].index("--rc-addr") + 1],
        )
        sync_rc.assert_called_once_with(
            "http://127.0.0.1:5572",
            previous_managed_host=None,
            user=None,
            password=None,
        )

    def test_rc_port_skips_other_rclone_and_altmount_ports(self):
        instance = {"command": []}
        instances = {
            "Other": {"command": ["rclone", "mount", "--rc-addr", ":5572"]},
            "NzbDAV": instance,
        }

        port = setup._select_rclone_rc_port(
            instance,
            instances,
            {"enabled": True, "mount_type": "rclone", "rclone_rc_port": 5573},
            port_available=lambda _port: True,
        )

        self.assertEqual(5574, port)

    def test_rc_port_preserves_available_saved_port(self):
        instance = {"command": ["rclone", "mount", "--rc-addr=:5580"]}

        port = setup._select_rclone_rc_port(
            instance,
            {"NzbDAV": instance},
            port_available=lambda _port: True,
        )

        self.assertEqual(5580, port)


if __name__ == "__main__":
    unittest.main()
