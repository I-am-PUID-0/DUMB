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
    def __init__(self, rclone_config, infinidysk_config=None):
        self.config = {"rclone": rclone_config}
        self._rclone_config = rclone_config
        self._infinidysk_config = infinidysk_config or {
            "backend_port": 8080,
            "env": {},
        }

    def get(self, key, default=None):
        if key == "rclone":
            return self._rclone_config
        if key == "infinidysk":
            return self._infinidysk_config
        return default

    def save_config(self, _process_name=None):
        return None


class RcloneSetupTests(unittest.TestCase):
    def test_postgres_infinidysk_credentials_use_live_config_api(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            instance = {
                "enabled": True,
                "process_name": "rclone w/ InfiniDysk",
                "log_level": "INFO",
                "key_type": "infinidysk",
                "zurg_enabled": False,
                "decypharr_enabled": False,
                "mount_dir": str(root / "mounts"),
                "mount_name": "infinidysk",
                "cache_dir": str(root / "cache"),
                "config_dir": str(root / "config"),
                "config_file": str(root / "config" / "rclone.config"),
                "zurg_config_file": "",
                "command": [],
            }
            manager = _ConfigManager(
                {"instances": {"InfiniDysk": instance}},
                {
                    "backend_port": 8080,
                    "postgres_enabled": True,
                    "env": {
                        "DATABASE_PROVIDER": "postgres",
                        "WEBDAV_PASSWORD": "generated-password",
                    },
                },
            )

            with (
                patch.object(setup, "CONFIG_MANAGER", manager),
                patch.object(setup, "fuse_config", return_value=(True, None)),
                patch("utils.dependencies.get_api_state", return_value=_ApiState()),
                patch.object(
                    nzbdav_db,
                    "get_config_value",
                    side_effect=AssertionError(
                        "PostgreSQL mode must not read db.sqlite credentials"
                    ),
                ),
                patch.object(
                    nzbdav_settings,
                    "_read_nzbdav_config_values",
                    return_value=({"webdav.user": "custom-user"}, None),
                ) as read_config,
                patch.object(riven_settings, "parse_config_keys"),
                patch.object(
                    nzbdav_settings,
                    "sync_nzbdav_rclone_rc",
                    return_value=(True, None),
                ),
                patch.object(setup, "obscure_password", return_value="obscured"),
                patch.object(setup, "chown_recursive", return_value=(True, None)),
                patch.object(setup.os, "chown"),
                patch.object(setup, "_is_rclone_rc_port_available", return_value=True),
            ):
                success, error = setup.rclone_setup()
            config_text = Path(instance["config_file"]).read_text(encoding="utf-8")

        self.assertTrue(success, error)
        read_config.assert_called_once()
        self.assertIn("user = custom-user", config_text)
        self.assertIn("pass = obscured", config_text)
        self.assertEqual(
            instance["wait_for_url"][0]["auth"],
            {"user": "custom-user", "password": "generated-password"},
        )

    def test_postgres_infinidysk_rclone_fails_closed_when_config_api_is_unavailable(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            instance = {
                "enabled": True,
                "process_name": "rclone w/ InfiniDysk",
                "log_level": "INFO",
                "key_type": "infinidysk",
                "zurg_enabled": False,
                "decypharr_enabled": False,
                "mount_dir": str(root / "mounts"),
                "mount_name": "infinidysk",
                "cache_dir": str(root / "cache"),
                "config_dir": str(root / "config"),
                "config_file": str(root / "config" / "rclone.config"),
                "zurg_config_file": "",
                "command": [],
            }
            manager = _ConfigManager(
                {"instances": {"InfiniDysk": instance}},
                {
                    "backend_port": 8080,
                    "postgres_enabled": True,
                    "env": {
                        "DATABASE_PROVIDER": "postgres",
                        "WEBDAV_PASSWORD": "generated-password",
                    },
                },
            )

            with (
                patch.object(setup, "CONFIG_MANAGER", manager),
                patch.object(setup, "fuse_config", return_value=(True, None)),
                patch("utils.dependencies.get_api_state", return_value=_ApiState()),
                patch.object(
                    nzbdav_db,
                    "get_config_value",
                    side_effect=AssertionError(
                        "PostgreSQL mode must not read db.sqlite credentials"
                    ),
                ),
                patch.object(
                    nzbdav_settings,
                    "_read_nzbdav_config_values",
                    return_value=(None, "configuration API unavailable"),
                ),
                patch.object(setup, "chown_recursive", return_value=(True, None)),
            ):
                success, error = setup.rclone_setup()

        self.assertFalse(success)
        self.assertIn("could not be read", error)
        self.assertFalse(Path(instance["config_file"]).exists())

    def test_postgres_infinidysk_uses_default_user_when_config_row_is_absent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            instance = {
                "enabled": True,
                "process_name": "rclone w/ InfiniDysk",
                "log_level": "INFO",
                "key_type": "infinidysk",
                "zurg_enabled": False,
                "decypharr_enabled": False,
                "mount_dir": str(root / "mounts"),
                "mount_name": "infinidysk",
                "cache_dir": str(root / "cache"),
                "config_dir": str(root / "config"),
                "config_file": str(root / "config" / "rclone.config"),
                "zurg_config_file": "",
                "command": [],
            }
            manager = _ConfigManager(
                {"instances": {"InfiniDysk": instance}},
                {
                    "backend_port": 8080,
                    "postgres_enabled": True,
                    "env": {
                        "DATABASE_PROVIDER": "postgres",
                        "WEBDAV_PASSWORD": "generated-password",
                    },
                },
            )

            with (
                patch.object(setup, "CONFIG_MANAGER", manager),
                patch.object(setup, "fuse_config", return_value=(True, None)),
                patch("utils.dependencies.get_api_state", return_value=_ApiState()),
                patch.object(
                    nzbdav_db,
                    "get_config_value",
                    side_effect=AssertionError(
                        "PostgreSQL mode must not read db.sqlite credentials"
                    ),
                ),
                patch.object(
                    nzbdav_settings,
                    "_read_nzbdav_config_values",
                    return_value=({"webdav.user": ""}, None),
                ),
                patch.object(riven_settings, "parse_config_keys"),
                patch.object(
                    nzbdav_settings,
                    "sync_nzbdav_rclone_rc",
                    return_value=(True, None),
                ),
                patch.object(setup, "obscure_password", return_value="obscured"),
                patch.object(setup, "chown_recursive", return_value=(True, None)),
                patch.object(setup.os, "chown"),
                patch.object(setup, "_is_rclone_rc_port_available", return_value=True),
            ):
                success, error = setup.rclone_setup()
            config_text = Path(instance["config_file"]).read_text(encoding="utf-8")

        self.assertTrue(success, error)
        self.assertIn("user = admin", config_text)
        self.assertIn("pass = obscured", config_text)
        self.assertEqual(
            instance["wait_for_url"][0]["auth"],
            {"user": "admin", "password": "generated-password"},
        )

    def test_fresh_nzbdav_mount_uses_week_long_cache_time_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            instance = {
                "enabled": True,
                "process_name": "rclone w/ InfiniDysk",
                "log_level": "INFO",
                "key_type": "infinidysk",
                "zurg_enabled": False,
                "decypharr_enabled": False,
                "mount_dir": str(root / "mounts"),
                "mount_name": "infinidysk",
                "cache_dir": str(root / "cache"),
                "config_dir": str(root / "config"),
                "config_file": str(root / "config" / "rclone.config"),
                "zurg_config_file": "",
                "command": [],
            }
            manager = _ConfigManager({"instances": {"InfiniDysk": instance}})

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
                ),
                patch.object(setup, "chown_recursive", return_value=(True, None)),
                patch.object(setup.os, "chown"),
                patch.object(setup, "_is_rclone_rc_port_available", return_value=True),
            ):
                success, error = setup.rclone_setup()

        self.assertTrue(success, error)
        self.assertIn("--dir-cache-time=1w", instance["command"])
        self.assertIn("--vfs-cache-max-age=1w", instance["command"])

    def test_saved_dir_cache_time_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            instance = {
                "enabled": True,
                "process_name": "rclone w/ InfiniDysk",
                "log_level": "INFO",
                "key_type": "infinidysk",
                "zurg_enabled": False,
                "decypharr_enabled": False,
                "mount_dir": str(root / "mounts"),
                "mount_name": "infinidysk",
                "cache_dir": str(root / "cache"),
                "config_dir": str(root / "config"),
                "config_file": str(root / "config" / "rclone.config"),
                "zurg_config_file": "",
                "command": [
                    "rclone",
                    "mount",
                    "nzbdav:",
                    str(root / "mounts" / "infinidysk"),
                    "--dir-cache-time=20s",
                ],
            }
            manager = _ConfigManager({"instances": {"InfiniDysk": instance}})

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
                patch.object(setup.os, "chown"),
                patch.object(setup, "_is_rclone_rc_port_available", return_value=True),
            ):
                success, error = setup.rclone_setup()
            config_mode = Path(instance["config_file"]).stat().st_mode & 0o777

        self.assertTrue(success, error)
        self.assertEqual(config_mode, 0o600)
        self.assertIn("--dir-cache-time=20s", instance["command"])
        self.assertNotIn("--dir-cache-time=10s", instance["command"])
        self.assertIn("--vfs-cache-max-age=1w", instance["command"])
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
            "InfiniDysk": instance,
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
            {"InfiniDysk": instance},
            port_available=lambda _port: True,
        )

        self.assertEqual(5580, port)


if __name__ == "__main__":
    unittest.main()
