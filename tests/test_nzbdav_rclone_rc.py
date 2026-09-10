import unittest
from urllib.parse import parse_qs
from unittest.mock import patch

from utils import nzbdav_settings


class InfiniDyskRcloneRcTests(unittest.TestCase):
    def test_postgres_config_api_prefers_live_db_key_over_env_fallback(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read():
                return (
                    b'{"configItems":[{"configName":"rclone.host",'
                    b'"configValue":"http://127.0.0.1:5572"}]}'
                )

        config = {
            "postgres_enabled": True,
            "backend_port": 8080,
            "env": {"FRONTEND_BACKEND_API_KEY": "env-api-key"},
        }
        with (
            patch.object(
                nzbdav_settings.nzbdav_db,
                "get_config_value",
                # get_config_value is PostgreSQL-aware: in postgres mode it
                # reads InfiniDysk's live api.key from Postgres rather than
                # db.sqlite. That live value must win over the possibly
                # stale FRONTEND_BACKEND_API_KEY env var.
                return_value="live-postgres-key",
            ) as get_config_value,
            patch.object(
                nzbdav_settings, "safe_urlopen", return_value=Response()
            ) as urlopen,
        ):
            payload, error = nzbdav_settings._infinidysk_config_api_request(
                config,
                "/api/get-config",
                [
                    ("config-keys", "rclone.host"),
                    ("config-keys", "rclone.user"),
                ],
            )

        self.assertIsNone(error)
        get_config_value.assert_called_with("api.key")
        self.assertEqual(payload["configItems"][0]["configName"], "rclone.host")
        request = urlopen.call_args.args[0]
        self.assertEqual(
            parse_qs(request.data.decode("utf-8")),
            {"config-keys": ["rclone.host", "rclone.user"]},
        )
        self.assertEqual(request.get_header("X-api-key"), "live-postgres-key")

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
                nzbdav_settings.CONFIG_MANAGER,
                "get",
                return_value={"backend_port": 8080, "env": {}},
            ),
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

    def test_postgres_rclone_sync_uses_api_without_sqlite_fallback(self):
        config = {
            "postgres_enabled": True,
            "backend_port": 8080,
            "env": {"FRONTEND_BACKEND_API_KEY": "env-api-key"},
        }
        responses = [
            (
                {
                    "configItems": [
                        {"configName": "rclone.rc-enabled", "configValue": "false"},
                        {"configName": "rclone.host", "configValue": ""},
                        {"configName": "rclone.user", "configValue": ""},
                        {"configName": "rclone.pass", "configValue": ""},
                    ]
                },
                None,
            ),
            ({"status": True}, None),
        ]
        with (
            patch.object(nzbdav_settings.CONFIG_MANAGER, "get", return_value=config),
            patch.object(
                nzbdav_settings,
                "_infinidysk_config_api_request",
                side_effect=responses,
            ) as api_request,
            patch.object(
                nzbdav_settings.nzbdav_db,
                "get_config_value",
                side_effect=AssertionError("PostgreSQL mode must not read db.sqlite"),
            ),
            patch.object(
                nzbdav_settings.nzbdav_db,
                "set_config_value",
                side_effect=AssertionError("PostgreSQL mode must not write db.sqlite"),
            ),
        ):
            success, error = nzbdav_settings.sync_nzbdav_rclone_rc(
                "http://127.0.0.1:5572",
                user="generated-user",
                password="generated-password",
            )

        self.assertTrue(success, error)
        self.assertEqual(api_request.call_count, 2)
        self.assertEqual(api_request.call_args_list[0].args[1], "/api/get-config")
        self.assertEqual(api_request.call_args_list[1].args[1], "/api/update-config")
        self.assertEqual(
            dict(api_request.call_args_list[1].args[2]),
            {
                "rclone.host": "http://127.0.0.1:5572",
                "rclone.rc-enabled": "true",
                "rclone.user": "generated-user",
                "rclone.pass": "generated-password",
            },
        )


if __name__ == "__main__":
    unittest.main()
