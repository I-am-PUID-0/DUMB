import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import patch

from utils import nzbdav_settings


class InfiniDyskRcloneRcTests(unittest.TestCase):
    def test_401_recovery_uses_active_key_once_and_keeps_persistent_env_unchanged(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"status":true}'

        config = {
            "postgres_enabled": True,
            "env": {"FRONTEND_BACKEND_API_KEY": "stale-fixture-key"},
        }
        runtime = nzbdav_settings._infinidysk_config_snapshot(config)
        unauthorized = urllib.error.HTTPError(
            "http://127.0.0.1/api/update-config", 401, "Unauthorized", {}, None
        )
        with (
            patch.object(
                nzbdav_settings.nzbdav_db,
                "get_active_config_value",
                return_value="public-fixture-key",
            ) as read_key,
            patch.object(
                nzbdav_settings, "safe_urlopen", side_effect=[unauthorized, Response()]
            ) as urlopen,
        ):
            payload, error = nzbdav_settings._infinidysk_config_api_request(
                runtime,
                "/api/update-config",
                [("rclone.host", "http://127.0.0.1:5572")],
            )
        self.assertEqual(payload, {"status": True})
        self.assertIsNone(error)
        read_key.assert_called_once_with("api.key", runtime)
        self.assertEqual(
            [call.args[0].get_header("X-api-key") for call in urlopen.call_args_list],
            ["stale-fixture-key", "public-fixture-key"],
        )
        self.assertEqual(
            urlopen.call_args_list[0].args[0].data,
            urlopen.call_args_list[1].args[0].data,
        )
        self.assertTrue(
            all(call.kwargs["timeout"] == 5 for call in urlopen.call_args_list)
        )
        self.assertEqual(config["env"]["FRONTEND_BACKEND_API_KEY"], "stale-fixture-key")
        self.assertEqual(
            runtime["env"]["FRONTEND_BACKEND_API_KEY"], "public-fixture-key"
        )

    def test_recovery_is_bounded_and_does_not_run_for_other_http_failures(self):
        cases = [
            (401, None, 1),
            (401, OSError("fixture PostgreSQL unavailable"), 1),
            (401, "stale-fixture-key", 1),
            (401, "public-fixture-key", 2),
            (403, "public-fixture-key", 1),
            (500, "public-fixture-key", 1),
        ]
        for status, key, expected_requests in cases:
            with self.subTest(
                status=status, key=type(key).__name__, requests=expected_requests
            ):
                config = nzbdav_settings._infinidysk_config_snapshot(
                    {
                        "postgres_enabled": True,
                        "env": {"FRONTEND_BACKEND_API_KEY": "stale-fixture-key"},
                    }
                )
                error = urllib.error.HTTPError(
                    "http://127.0.0.1/api/get-config",
                    status,
                    "fixture HTTP failure",
                    {},
                    None,
                )
                with (
                    patch.object(
                        nzbdav_settings.nzbdav_db,
                        "get_active_config_value",
                        return_value=key,
                        side_effect=key if isinstance(key, Exception) else None,
                    ) as read_key,
                    patch.object(
                        nzbdav_settings, "safe_urlopen", side_effect=error
                    ) as urlopen,
                    patch.object(
                        nzbdav_settings.nzbdav_db,
                        "get_config_value",
                        side_effect=AssertionError("retained SQLite must not be read"),
                    ),
                ):
                    payload, message = nzbdav_settings._infinidysk_config_api_request(
                        config, "/api/get-config", []
                    )
                self.assertIsNone(payload)
                self.assertEqual(
                    message, f"InfiniDysk configuration API returned HTTP {status}."
                )
                self.assertEqual(urlopen.call_count, expected_requests)
                self.assertEqual(read_key.call_count, int(status == 401))
                self.assertEqual(
                    config["env"]["FRONTEND_BACKEND_API_KEY"], "stale-fixture-key"
                )

    def test_sqlite_rclone_sync_recovers_against_running_backend_auth_contract(self):
        observed_keys = []
        observed_fields = []
        # Official ApiKeyValidator permits either the running environment key or
        # ConfigItems api.key; these intentionally differ from DUMB's stale key.
        accepted_keys = {"running-env-fixture-key", "public-fixture-key"}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                key = self.headers.get("x-api-key")
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                observed_keys.append(key)
                observed_fields.append(parse_qs(body.decode()))
                self.send_response(200 if key in accepted_keys else 401)
                self.end_headers()
                self.wfile.write(b'{"status":true}')

            def log_message(self, *_args):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            with sqlite3.connect(Path(tmp, "db.sqlite")) as connection:
                connection.execute(
                    "CREATE TABLE ConfigItems (ConfigName TEXT PRIMARY KEY, ConfigValue TEXT)"
                )
                connection.executemany(
                    "INSERT INTO ConfigItems VALUES (?, ?)",
                    [
                        ("api.key", "public-fixture-key"),
                        ("rclone.host", ""),
                        ("rclone.rc-enabled", "false"),
                        ("rclone.user", ""),
                        ("rclone.pass", ""),
                    ],
                )
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            config = {
                "config_dir": tmp,
                "backend_port": server.server_port,
                "env": {
                    "CONFIG_PATH": tmp,
                    "FRONTEND_BACKEND_API_KEY": "stale-fixture-key",
                },
            }
            try:
                with (
                    patch.object(
                        nzbdav_settings.CONFIG_MANAGER, "get", return_value=config
                    ),
                    patch.object(
                        nzbdav_settings.nzbdav_db, "_get_config_path", return_value=tmp
                    ),
                ):
                    success, error = nzbdav_settings.sync_nzbdav_rclone_rc(
                        "http://127.0.0.1:5572"
                    )
                self.assertTrue(success, error)
                self.assertEqual(
                    observed_keys, ["stale-fixture-key", "public-fixture-key"]
                )
                self.assertEqual(observed_fields[0], observed_fields[1])
                self.assertEqual(
                    observed_fields[1]["rclone.host"], ["http://127.0.0.1:5572"]
                )
                self.assertEqual(
                    config["env"]["FRONTEND_BACKEND_API_KEY"], "stale-fixture-key"
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

    def test_recovered_key_reaches_arr_clients_in_both_providers_without_persistence(
        self,
    ):
        values = {
            "arr.instances": "{}",
            "api.categories": "movies",
            "rclone.mount-dir": "/mnt/debrid/infinidysk",
        }

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "configItems": [
                            {"configName": key, "configValue": value}
                            for key, value in values.items()
                        ]
                    }
                ).encode()

        for postgres in (False, True):
            with self.subTest(postgres=postgres):
                config = {
                    "process_name": "InfiniDysk",
                    "postgres_enabled": postgres,
                    "env": {"FRONTEND_BACKEND_API_KEY": "stale-fixture-key"},
                }
                keys = []

                def urlopen(request, **_kwargs):
                    key = request.get_header("X-api-key")
                    keys.append(key)
                    if key == "stale-fixture-key":
                        raise urllib.error.HTTPError(
                            request.full_url, 401, "fixture unauthorized", {}, None
                        )
                    return Response()

                with (
                    patch.object(
                        nzbdav_settings.CONFIG_MANAGER, "get", return_value=config
                    ),
                    patch.object(
                        nzbdav_settings,
                        "_collect_arr_entries",
                        return_value=(
                            [
                                {
                                    "Host": "http://example.invalid:7878",
                                    "ApiKey": "arr-fixture-key",
                                    "Category": "movies",
                                }
                            ],
                            [],
                            [],
                            [],
                        ),
                    ),
                    patch.object(
                        nzbdav_settings, "_instance_core_services", return_value=[]
                    ),
                    patch.object(nzbdav_settings, "_ensure_symlink_roots"),
                    patch.object(nzbdav_settings, "_ensure_arr_permissions"),
                    patch.object(nzbdav_settings, "_ensure_arr_rootfolder"),
                    patch.object(nzbdav_settings, "_wait_for_arr", return_value=True),
                    patch.object(
                        nzbdav_settings, "_wait_for_nzbdav", return_value=True
                    ),
                    patch.object(nzbdav_settings, "_schedule_nzbdav_retry"),
                    patch.object(
                        nzbdav_settings.nzbdav_db,
                        "get_config_value",
                        side_effect=lambda name: values.get(name),
                    ),
                    patch.object(
                        nzbdav_settings.nzbdav_db,
                        "set_config_value",
                        return_value=(True, None),
                    ),
                    patch.object(
                        nzbdav_settings.nzbdav_db,
                        "get_active_config_value",
                        return_value="public-fixture-key",
                    ),
                    patch.object(nzbdav_settings, "safe_urlopen", side_effect=urlopen),
                    patch.object(
                        nzbdav_settings, "ensure_nzbdav_download_client"
                    ) as wire_client,
                    patch.object(
                        nzbdav_settings._NZBDAV_PATCH_CONTEXT,
                        "defer_runtime_integrations",
                        False,
                        create=True,
                    ),
                ):
                    _, error = nzbdav_settings._patch_nzbdav_config_admitted()
                self.assertIsNone(error)
                wire_client.assert_called_once()
                self.assertEqual(wire_client.call_args.args[3], "public-fixture-key")
                self.assertEqual(keys[:2], ["stale-fixture-key", "public-fixture-key"])
                self.assertEqual(
                    config["env"]["FRONTEND_BACKEND_API_KEY"], "stale-fixture-key"
                )

    def test_postgres_config_api_uses_env_key_and_repeated_config_fields(self):
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
                "get_active_config_value",
                side_effect=AssertionError(
                    "a valid environment key must not read any database"
                ),
            ),
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
        self.assertEqual(payload["configItems"][0]["configName"], "rclone.host")
        request = urlopen.call_args.args[0]
        self.assertEqual(
            parse_qs(request.data.decode("utf-8")),
            {"config-keys": ["rclone.host", "rclone.user"]},
        )
        self.assertEqual(request.get_header("X-api-key"), "env-api-key")

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
