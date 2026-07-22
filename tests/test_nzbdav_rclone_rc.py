import unittest
from unittest.mock import patch

from utils import nzbdav_settings


class NzbDAVRcloneRcTests(unittest.TestCase):
    def test_empty_settings_are_seeded_once(self):
        values = {
            "rclone.rc-enabled": "false",
            "rclone.host": "",
            "rclone.user": "",
            "rclone.pass": "",
            "api.key": "",
        }
        writes = {}

        def set_value(key, value):
            writes[key] = value
            return True, None

        with (
            patch.object(
                nzbdav_settings.nzbdav_db,
                "get_config_value",
                side_effect=lambda key: values.get(key),
            ),
            patch.object(
                nzbdav_settings.nzbdav_db,
                "set_config_value",
                side_effect=set_value,
            ),
            patch.object(
                nzbdav_settings.CONFIG_MANAGER,
                "get",
                return_value={"backend_port": 8080, "env": {}},
            ),
        ):
            success, error = nzbdav_settings.sync_nzbdav_rclone_rc(
                "http://127.0.0.1:5572"
            )

        self.assertTrue(success, error)
        self.assertEqual(
            {
                "rclone.host": "http://127.0.0.1:5572",
                "rclone.rc-enabled": "true",
            },
            writes,
        )

    def test_user_changes_are_preserved(self):
        values = {
            "rclone.rc-enabled": "false",
            "rclone.host": "http://example.invalid:6000",
            "rclone.user": "custom-user",
            "rclone.pass": "custom-pass",
        }

        with (
            patch.object(
                nzbdav_settings.nzbdav_db,
                "get_config_value",
                side_effect=lambda key: values.get(key),
            ),
            patch.object(nzbdav_settings.nzbdav_db, "set_config_value") as setter,
        ):
            success, error = nzbdav_settings.sync_nzbdav_rclone_rc(
                "http://127.0.0.1:5572",
                previous_managed_host="http://127.0.0.1:5580",
                user="generated-user",
                password="generated-pass",
            )

        self.assertTrue(success, error)
        setter.assert_not_called()

    def test_managed_host_tracks_reallocated_port_without_reenabling(self):
        values = {
            "rclone.rc-enabled": "false",
            "rclone.host": "http://127.0.0.1:5572",
            "rclone.user": "",
            "rclone.pass": "",
            "api.key": "",
        }
        writes = {}

        def set_value(key, value):
            writes[key] = value
            return True, None

        with (
            patch.object(
                nzbdav_settings.nzbdav_db,
                "get_config_value",
                side_effect=lambda key: values.get(key),
            ),
            patch.object(
                nzbdav_settings.nzbdav_db,
                "set_config_value",
                side_effect=set_value,
            ),
            patch.object(
                nzbdav_settings.CONFIG_MANAGER,
                "get",
                return_value={"backend_port": 8080, "env": {}},
            ),
        ):
            success, error = nzbdav_settings.sync_nzbdav_rclone_rc(
                "http://127.0.0.1:5574",
                previous_managed_host="http://127.0.0.1:5572",
            )

        self.assertTrue(success, error)
        self.assertEqual({"rclone.host": "http://127.0.0.1:5574"}, writes)


if __name__ == "__main__":
    unittest.main()
