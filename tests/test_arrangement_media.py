"""Stateful media API fixtures; never launch applications or touch real data."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from utils import arrangement_media as media
from utils.arrangement_http import ApiResponse


class FakeMediaServer:
    def __init__(self, service, *, initialized=True):
        self.service = service
        self.initialized = initialized
        self.calls = []
        self.libraries = []
        self.fail_auth = False
        self.drop_created_library = False
        self.initialization_race = False
        self.status_checks = 0

    def factory(self, url, **options):
        server = self

        class Client:
            def request(self, method, path, **kwargs):
                return server.request(method, path, url, options, **kwargs)

        return Client()

    def request(self, method, path, url, options, **kwargs):
        self.calls.append(
            (method, path, copy.deepcopy(kwargs), copy.deepcopy(options), url)
        )
        if path in {"/System/Info/Public", "/status"}:
            self.status_checks += 1
            initialized = (
                self.initialized or self.initialization_race and self.status_checks > 1
            )
            return self.response(
                {"isInit": initialized, "serverVersion": "2.37.1"}
                if self.service == "audiobookshelf"
                else {"StartupWizardCompleted": initialized, "Id": "server-fixture"}
            )
        if path == "/identity":
            return self.response(
                {
                    "MediaContainer": {
                        "claimed": True,
                        "machineIdentifier": "server-fixture",
                    }
                }
            )
        if self.fail_auth:
            return ApiResponse(401, b'{"error":"private-token-fixture"}')
        if path == "/Startup/User" and method == "GET":
            return self.response({"Name": "placeholder"})
        if path == "/Startup/User":
            return ApiResponse(204)
        if path in {"/Startup/Complete", "/init"}:
            self.initialized = True
            return ApiResponse(204)
        if path == "/Users/AuthenticateByName":
            return self.response(
                {
                    "AccessToken": "private-session-fixture",
                    "User": {"Id": "admin-fixture"},
                }
            )
        if path == "/login":
            return self.response(
                {
                    "user": {
                        "id": "root-fixture",
                        "accessToken": "private-session-fixture",
                    }
                }
            )
        if path == "/Auth/Keys":
            return self.response(
                {
                    "AppName": "DUMB Setup and Protection",
                    "AccessToken": "private-dedicated-fixture",
                }
            )
        if path == "/api/api-keys":
            return self.response(
                {"apiKey": {"id": "key-fixture", "apiKey": "private-dedicated-fixture"}}
            )
        if path == "/System/Info":
            return self.response({"Id": "server-fixture", "ServerName": "Fixture"})
        if path == "/Sessions":
            return self.response([])
        if path == "/status/sessions":
            return self.response({"MediaContainer": {"size": 0}})
        if path in {
            "/api/libraries",
            "/Library/VirtualFolders",
            "/Library/VirtualFolders/Query",
            "/library/sections/all",
        }:
            if method == "POST":
                body, params = kwargs.get("json", {}), kwargs.get("params", {})
                if self.service == "plex":
                    kind = {1: "movie", 2: "show", 8: "artist"}[params["type"]]
                    row = {
                        "title": params["name"],
                        "type": kind,
                        "Location": [{"path": path} for path in params["locations"]],
                        "agent": params["agent"],
                        "scanner": params["scanner"],
                    }
                elif self.service == "audiobookshelf":
                    row = {
                        "name": body["name"],
                        "mediaType": body["mediaType"],
                        "folders": copy.deepcopy(body["folders"]),
                    }
                elif self.service == "jellyfin":
                    row = {
                        "Name": params["name"],
                        "CollectionType": params.get("collectionType"),
                        "Locations": [
                            item["Path"] for item in body["LibraryOptions"]["PathInfos"]
                        ],
                    }
                else:
                    row = {
                        "Name": body["Name"],
                        "CollectionType": body["CollectionType"],
                        "Locations": body["Paths"],
                    }
                if not self.drop_created_library:
                    self.libraries.append(row)
                return ApiResponse(204)
            if self.service == "plex":
                return self.response({"MediaContainer": {"Directory": self.libraries}})
            if self.service == "audiobookshelf":
                return self.response({"libraries": self.libraries})
            if self.service == "emby":
                return self.response({"Items": self.libraries})
            return self.response(self.libraries)
        raise AssertionError((method, path))

    @staticmethod
    def response(value):
        return ApiResponse(200, json.dumps(value).encode())


class MediaSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, service, kind="libraries", *, initialized=True):
        server = FakeMediaServer(service, initialized=initialized)
        directory = self.root / service
        directory.mkdir(exist_ok=True)
        config = {
            service: {
                "enabled": True,
                "port": (
                    8096
                    if service in {"jellyfin", "emby"}
                    else 32400 if service == "plex" else 13378
                ),
                "config_dir": str(directory),
                "config_file": str(directory / "Preferences.xml"),
                "process_name": service,
            }
        }
        if service == "plex":
            Path(config[service]["config_file"]).write_text(
                '<Preferences PlexOnlineToken="private-managed-fixture" />'
            )
        action = {"service_key": service, "source": service, "action": kind}
        payload = {
            "arrangement": {"values": {service + "::api_key": "private-api-fixture"}}
        }
        return config, action, payload, server

    def invoke(self, fixture, *, inspect=False):
        config, action, payload, server = fixture
        fn = media.inspect_media if inspect else media.apply_media
        return fn(action, config, payload, client_factory=server.factory)

    def test_selected_libraries_create_once_read_back_and_reuse_without_policy_mutation(
        self,
    ):
        for service in media.MEDIA:
            with self.subTest(service=service):
                fixture = self.fixture(service)
                media_type = "book" if service == "audiobookshelf" else "movie"
                spec = {
                    "name": "Fixture Library",
                    "media_type": media_type,
                    "paths": ["/library/fixture"],
                    "create_if_missing": True,
                }
                fixture[2]["arrangement"]["values"][service + "::libraries"] = [spec]
                self.assertEqual(
                    self.invoke(fixture, inspect=True)["execution"], "planned"
                )
                self.assertFalse(any(call[0] == "POST" for call in fixture[3].calls))
                result = self.invoke(fixture)
                self.assertEqual(result["execution"], "applied")
                self.assertEqual(result["verification"], "passed")
                self.assertFalse(result["evidence"]["playback_tested"])
                before = len(fixture[3].calls)
                self.assertEqual(self.invoke(fixture)["execution"], "already_present")
                self.assertFalse(
                    any(call[0] != "GET" for call in fixture[3].calls[before:])
                )

    def test_library_creation_requires_explicit_choice(self):
        fixture = self.fixture("jellyfin")
        fixture[2]["arrangement"]["values"]["jellyfin::libraries"] = [
            {"name": "Movies", "media_type": "movie", "paths": ["/library/movies"]}
        ]
        self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")
        self.assertFalse(any(call[0] == "POST" for call in fixture[3].calls))

    def test_existing_library_paths_agents_and_extra_options_are_preserved(self):
        fixture = self.fixture("plex")
        existing = {
            "title": "Movies",
            "type": "movie",
            "Location": [{"path": "/library/movies"}, {"path": "/library/extras"}],
            "agent": "custom-fixture",
            "scanner": "custom-fixture",
            "user_option": True,
        }
        fixture[3].libraries = [copy.deepcopy(existing)]
        fixture[2]["arrangement"]["values"]["plex::libraries"] = [
            {
                "name": "Movies",
                "media_type": "movie",
                "paths": ["/library/movies"],
                "create_if_missing": True,
            }
        ]
        self.assertEqual(self.invoke(fixture)["execution"], "already_present")
        self.assertEqual(fixture[3].libraries, [existing])

    def test_name_collision_with_different_path_is_explicit_and_non_destructive(self):
        fixture = self.fixture("audiobookshelf")
        fixture[3].libraries = [
            {
                "name": "Books",
                "mediaType": "book",
                "folders": [{"fullPath": "/library/existing"}],
            }
        ]
        before = copy.deepcopy(fixture[3].libraries)
        fixture[2]["arrangement"]["values"]["audiobookshelf::libraries"] = [
            {
                "name": "Books",
                "media_type": "book",
                "paths": ["/library/new"],
                "create_if_missing": True,
            }
        ]
        self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")
        self.assertEqual(fixture[3].libraries, before)

    def test_differently_named_matching_library_is_not_duplicated(self):
        fixture = self.fixture("emby")
        fixture[3].libraries = [
            {
                "Name": "Existing",
                "CollectionType": "movies",
                "Locations": ["/library/movies"],
            }
        ]
        fixture[2]["arrangement"]["values"]["emby::libraries"] = [
            {
                "name": "New",
                "media_type": "movie",
                "paths": ["/library/movies"],
                "create_if_missing": True,
            }
        ]
        self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")
        self.assertEqual(len(fixture[3].libraries), 1)

    def test_wrong_readback_is_never_reported_verified(self):
        fixture = self.fixture("jellyfin")
        fixture[3].drop_created_library = True
        fixture[2]["arrangement"]["values"]["jellyfin::libraries"] = [
            {
                "name": "Movies",
                "media_type": "movie",
                "paths": ["/library/movies"],
                "create_if_missing": True,
            }
        ]
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "failed")
        self.assertEqual(result["verification"], "failed")

    def test_server_specific_library_contracts_and_languages(self):
        fixture = self.fixture("jellyfin")
        fixture[2]["arrangement"]["values"]["jellyfin::libraries"] = [
            {
                "name": "Series",
                "media_type": "show",
                "paths": ["/library/shows"],
                "language": "en",
                "create_if_missing": True,
            }
        ]
        self.assertEqual(self.invoke(fixture)["execution"], "applied")
        call = next(call for call in fixture[3].calls if call[0] == "POST")
        self.assertEqual(call[2]["params"]["collectionType"], "tvshows")
        self.assertEqual(call[2]["params"]["refreshLibrary"], "false")
        self.assertEqual(
            call[2]["json"]["LibraryOptions"]["PreferredMetadataLanguage"], "en"
        )
        fixture = self.fixture("emby")
        fixture[2]["arrangement"]["values"]["emby::libraries"] = [
            {
                "name": "Music",
                "media_type": "music",
                "paths": ["/library/music"],
                "create_if_missing": True,
            }
        ]
        self.invoke(fixture)
        call = next(call for call in fixture[3].calls if call[0] == "POST")
        self.assertEqual(call[2]["json"]["CollectionType"], "music")
        self.assertFalse(call[2]["json"]["RefreshLibrary"])

    def test_invalid_library_types_system_paths_and_duplicate_names_are_rejected_before_writes(
        self,
    ):
        fixtures = [
            (
                "audiobookshelf",
                [
                    {
                        "name": "Movies",
                        "media_type": "movie",
                        "paths": ["/library/movies"],
                        "create_if_missing": True,
                    }
                ],
            ),
            (
                "jellyfin",
                [
                    {
                        "name": "Secrets",
                        "media_type": "movie",
                        "paths": ["/etc/media"],
                        "create_if_missing": True,
                    }
                ],
            ),
            (
                "plex",
                [
                    {"name": "Same", "media_type": "movie", "paths": ["/a"]},
                    {"name": "same", "media_type": "movie", "paths": ["/b"]},
                ],
            ),
        ]
        for service, supplied in fixtures:
            fixture = self.fixture(service)
            fixture[2]["arrangement"]["values"][service + "::libraries"] = supplied
            self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")
            self.assertFalse(fixture[3].calls)

    def test_fresh_jellyfin_and_abs_initialize_once_exchange_dedicated_key_and_clear_password(
        self,
    ):
        for service in ("jellyfin", "audiobookshelf"):
            with self.subTest(service=service):
                fixture = self.fixture(service, "account", initialized=False)
                values = fixture[2]["arrangement"]["values"]
                values.update(
                    {
                        service + "::admin_username": "initial-fixture",
                        service + "::admin_password": "private-password-fixture",
                    }
                )
                self.assertEqual(
                    self.invoke(fixture, inspect=True)["execution"], "planned"
                )
                self.assertFalse(any(call[0] == "POST" for call in fixture[3].calls))
                result = self.invoke(fixture)
                self.assertEqual(result["execution"], "applied")
                self.assertEqual(
                    result["_private_values"],
                    {service + "::api_key": "private-dedicated-fixture"},
                )
                self.assertEqual(
                    result["_clear_inputs"], [service + "::admin_password"]
                )
                safe = {
                    key: value
                    for key, value in result.items()
                    if not key.startswith("_")
                }
                self.assertNotIn("private-", json.dumps(safe))
                before = len(fixture[3].calls)
                self.assertEqual(self.invoke(fixture)["execution"], "already_present")
                self.assertFalse(
                    any(call[0] == "POST" for call in fixture[3].calls[before:])
                )

    def test_existing_server_is_never_reinitialized_even_when_admin_credentials_supplied(
        self,
    ):
        fixture = self.fixture("jellyfin", "account")
        fixture[2]["arrangement"]["values"].update(
            {
                "jellyfin::admin_username": "different",
                "jellyfin::admin_password": "private-password",
            }
        )
        self.assertEqual(self.invoke(fixture)["execution"], "already_present")
        self.assertFalse(any(call[0] == "POST" for call in fixture[3].calls))

    def test_initialization_race_fails_closed_without_account_mutation(self):
        fixture = self.fixture("jellyfin", "account", initialized=False)
        fixture[3].initialization_race = True
        fixture[2]["arrangement"]["values"].update(
            {
                "jellyfin::admin_username": "initial",
                "jellyfin::admin_password": "private-password",
            }
        )
        self.assertEqual(self.invoke(fixture)["execution"], "waiting_dependency")
        self.assertFalse(any(call[0] == "POST" for call in fixture[3].calls))

    def test_fresh_emby_account_requires_native_wizard_instead_of_assuming_jellyfin_contract(
        self,
    ):
        fixture = self.fixture("emby", "account", initialized=False)
        self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")
        self.assertFalse(any(call[0] == "POST" for call in fixture[3].calls))

    def test_authorization_failure_is_redacted_and_not_verified(self):
        fixture = self.fixture("audiobookshelf", "account")
        fixture[3].fail_auth = True
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "waiting_input")
        self.assertEqual(result["verification"], "failed")
        self.assertNotIn("private-token", json.dumps(result))

    def test_protection_checks_are_read_only_and_do_not_claim_outage_validation(self):
        for service in ("plex", "jellyfin", "emby"):
            fixture = self.fixture(service, "protection")
            result = self.invoke(fixture)
            self.assertEqual(result["verification"], "passed")
            self.assertFalse(result["evidence"]["outage_simulated"])
            self.assertTrue(all(call[0] == "GET" for call in fixture[3].calls))


class MediaFileBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def tautulli_fixture(self):
        path = self.root / "config.ini"
        path.write_text(
            "# preserve comment\n[General]\napi_key = private-tautulli-fixture\nmonitoring = 1\n[PMS]\npms_token = \npms_identifier = \ncustom_setting = untouched\n[Notifications]\nkeep = yes\n"
        )
        config = {
            "tautulli": {"config_file": str(path), "port": 8181},
            "plex": {"enabled": True, "port": 32400},
            "dumb": {"plex_token": "private-plex-fixture"},
        }
        action = {
            "service_key": "tautulli",
            "source": "tautulli",
            "action": "backend_connection",
        }
        payload = {
            "arrangement": {
                "values": {
                    "tautulli::media_server": "plex",
                    "tautulli::restart_after_config": True,
                }
            },
            "_service_stopped": True,
        }
        calls = []

        def factory(url, **options):
            class Client:
                def request(self, method, endpoint, **kwargs):
                    calls.append(
                        (
                            url,
                            method,
                            endpoint,
                            copy.deepcopy(options),
                            copy.deepcopy(kwargs),
                        )
                    )
                    if endpoint == "/identity":
                        value = {
                            "MediaContainer": {
                                "machineIdentifier": "identity-fixture",
                                "claimed": True,
                            }
                        }
                    elif endpoint == "/api/v2":
                        value = {
                            "response": {
                                "result": "success",
                                "data": [{"machine_identifier": "identity-fixture"}],
                            }
                        }
                    else:
                        value = {"MediaContainer": {"Directory": []}}
                    return ApiResponse(200, json.dumps(value).encode())

            return Client()

        return config, action, payload, path, factory, calls

    def test_tautulli_requires_controlled_stopped_service_before_any_ini_write(self):
        config, action, payload, path, factory, _calls = self.tautulli_fixture()
        original = path.read_text()
        payload.pop("_service_stopped")
        result = media.apply_media(action, config, payload, client_factory=factory)
        self.assertEqual(result["execution"], "waiting_input")
        self.assertEqual(path.read_text(), original)

    def test_tautulli_atomic_patch_preserves_other_fields_and_verifies_real_server_identity(
        self,
    ):
        config, action, payload, path, factory, calls = self.tautulli_fixture()
        result = media.apply_media(action, config, payload, client_factory=factory)
        self.assertEqual(result["execution"], "applied")
        self.assertEqual(result["verification"], "passed")
        text = path.read_text()
        self.assertIn("# preserve comment", text)
        self.assertIn("custom_setting = untouched", text)
        self.assertIn("[Notifications]\nkeep = yes", text)
        self.assertIn("pms_identifier = identity-fixture", text)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(calls[-1][2], "/api/v2")
        self.assertNotIn("private-", json.dumps(result))

    def test_tautulli_different_existing_connection_is_preserved(self):
        config, action, payload, path, factory, _calls = self.tautulli_fixture()
        path.write_text(
            path.read_text().replace("pms_token = ", "pms_token = other-token")
        )
        original = path.read_text()
        self.assertEqual(
            media.apply_media(action, config, payload, client_factory=factory)[
                "execution"
            ],
            "waiting_input",
        )
        self.assertEqual(path.read_text(), original)

    def test_external_plex_never_receives_managed_plex_token_and_private_access_is_explicit(
        self,
    ):
        config, action, payload, _path, factory, calls = self.tautulli_fixture()
        payload["arrangement"]["values"].update(
            {
                "tautulli::media_server": "external",
                "tautulli::external_media": {
                    "service_key": "plex",
                    "url": "https://plex.example.invalid/base",
                    "api_key": "external-private-fixture",
                    "allow_private": False,
                    "verify_tls": True,
                },
            }
        )
        media.apply_media(action, config, payload, client_factory=factory)
        external = next(call for call in calls if call[0].startswith("https"))
        self.assertEqual(
            external[3]["headers"]["X-Plex-Token"], "external-private-fixture"
        )
        self.assertFalse(external[3]["allow_private"])
        self.assertTrue(external[3]["verify_tls"])

    def test_riven_frontend_waits_for_generated_key_and_preserves_unowned_connection(
        self,
    ):
        backend = self.root / "backend.json"
        backend.write_text("{}")
        frontend = self.root / "frontend"
        frontend.mkdir()
        config = {
            "riven_backend": {"config_file": str(backend), "port": 8080},
            "riven_frontend": {"config_dir": str(frontend)},
        }
        action = {
            "source": "riven_frontend",
            "service_key": "riven_frontend",
            "action": "backend_connection",
        }
        factory = mock.Mock()
        self.assertEqual(
            media.apply_media(action, config, {}, client_factory=factory)["execution"],
            "waiting_dependency",
        )
        factory.assert_not_called()
        backend.write_text('{"api_key": "private-riven-fixture"}')
        factory.return_value.request.return_value = ApiResponse(
            200, b'{"properties":{}}'
        )
        result = media.apply_media(action, config, {}, client_factory=factory)
        self.assertEqual(result["execution"], "applied")
        target = frontend / "config/server.json"
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            json.loads(target.read_text())["apiKey"], "private-riven-fixture"
        )
        self.assertEqual(
            factory.return_value.request.call_args.args[1], "/api/v1/settings/schema"
        )
        self.assertNotIn("private-riven", json.dumps(result))
        target.write_text('{"backendUrl":"http://example.invalid","apiKey":"user-key"}')
        original = target.read_text()
        self.assertEqual(
            media.apply_media(action, config, {}, client_factory=factory)["execution"],
            "waiting_input",
        )
        self.assertEqual(target.read_text(), original)

    def riven_owner_fixture(self, uid, gid):
        backend = self.root / "owned-backend.json"
        backend.write_text('{"api_key":"private-riven-owner-fixture"}')
        frontend = self.root / "owned-frontend"
        frontend.mkdir()
        config = {
            "puid": uid,
            "pgid": gid,
            "riven_backend": {"config_file": str(backend)},
            "riven_frontend": {"config_dir": str(frontend)},
        }
        action = {
            "source": "riven_frontend",
            "service_key": "riven_frontend",
            "action": "backend_connection",
        }
        factory = mock.Mock()
        factory.return_value.request.return_value = ApiResponse(
            200, b'{"properties":{}}'
        )
        return config, action, factory, frontend

    def test_riven_new_private_configuration_supports_an_unprivileged_current_user(
        self,
    ):
        import os

        config, action, factory, frontend = self.riven_owner_fixture(
            os.geteuid(), os.getegid()
        )
        with mock.patch.object(media.os, "chown", wraps=os.chown) as chown:
            result = media.apply_media(action, config, {}, client_factory=factory)
        self.assertEqual(result["execution"], "applied")
        chown.assert_not_called()
        for path in (frontend / "config", frontend / "config/server.json"):
            self.assertEqual(
                (path.stat().st_uid, path.stat().st_gid), (os.geteuid(), os.getegid())
            )
        self.assertEqual(
            (frontend / "config/server.json").stat().st_mode & 0o777, 0o600
        )

    @unittest.skipUnless(
        __import__("os").geteuid() == 0, "Requires changing a disposable file's UID"
    )
    def test_riven_new_credentials_and_directory_are_readable_by_the_configured_service_uid(
        self,
    ):
        import os
        import subprocess
        import sys

        config, action, factory, frontend = self.riven_owner_fixture(65534, 65534)
        self.root.chmod(0o755)
        result = media.apply_media(action, config, {}, client_factory=factory)
        self.assertEqual(result["execution"], "applied")
        target = frontend / "config/server.json"
        for path in (target.parent, target):
            self.assertEqual((path.stat().st_uid, path.stat().st_gid), (65534, 65534))
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os,pathlib; os.setgroups([]); os.setgid(65534); os.setuid(65534); assert pathlib.Path(os.environ['RIVEN_OWNER_FIXTURE']).read_text(); print('readable')",
            ],
            env={**os.environ, "RIVEN_OWNER_FIXTURE": str(target)},
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), "readable")
        self.assertEqual(
            media.apply_media(action, config, {}, client_factory=factory)["execution"],
            "already_present",
        )
        target.chmod(0o640)
        self.assertEqual(
            media.apply_media(action, config, {}, client_factory=factory)["execution"],
            "applied",
        )
        self.assertEqual((target.stat().st_uid, target.stat().st_gid), (65534, 65534))
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_riven_inspect_does_not_create_config_file(self):
        backend = self.root / "backend.json"
        backend.write_text('{"api_key":"private-riven-fixture"}')
        frontend = self.root / "frontend"
        frontend.mkdir()
        factory = mock.Mock()
        factory.return_value.request.return_value = ApiResponse(
            200, b'{"properties":{}}'
        )
        result = media.inspect_media(
            {
                "source": "riven_frontend",
                "service_key": "riven_frontend",
                "action": "backend_connection",
            },
            {
                "riven_backend": {"config_file": str(backend)},
                "riven_frontend": {"config_dir": str(frontend)},
            },
            {},
            client_factory=factory,
        )
        self.assertEqual(result["execution"], "planned")
        self.assertFalse((frontend / "config/server.json").exists())

    def test_riven_generated_key_rotation_updates_only_a_recorded_owned_copy(self):
        backend = self.root / "backend.json"
        backend.write_text('{"api_key":"first-private-fixture"}')
        frontend = self.root / "frontend"
        frontend.mkdir()
        config = {
            "riven_backend": {"config_file": str(backend)},
            "riven_frontend": {"config_dir": str(frontend)},
        }
        action = {
            "source": "riven_frontend",
            "service_key": "riven_frontend",
            "action": "backend_connection",
        }
        factory = mock.Mock()
        factory.return_value.request.return_value = ApiResponse(
            200, b'{"properties":{}}'
        )
        self.assertEqual(
            media.apply_media(action, config, {}, client_factory=factory)["execution"],
            "applied",
        )
        backend.write_text('{"api_key":"rotated-private-fixture"}')
        self.assertEqual(
            media.apply_media(action, config, {}, client_factory=factory)["execution"],
            "applied",
        )
        target = frontend / "config/server.json"
        self.assertEqual(
            json.loads(target.read_text())["apiKey"], "rotated-private-fixture"
        )
        self.assertEqual(
            Path(str(target) + ".dumb-owned.json").stat().st_mode & 0o777, 0o600
        )

    def test_tautulli_ini_updates_remain_in_pms_section(self):
        config, action, payload, path, factory, _calls = self.tautulli_fixture()
        media.apply_media(action, config, payload, client_factory=factory)
        import configparser

        parser = configparser.ConfigParser(interpolation=None)
        parser.read(path)
        self.assertEqual(parser["PMS"]["pms_identifier"], "identity-fixture")
        self.assertNotIn("pms_identifier", parser["Notifications"])
        self.assertEqual(parser["Notifications"]["keep"], "yes")


class MediaResponseContractTests(unittest.TestCase):
    def test_plex_xml_library_response_is_parsed_without_unsafe_html_handling(self):
        client = mock.Mock()
        client.request.return_value = ApiResponse(
            200,
            b'<MediaContainer><Directory title="Movies" type="movie"><Location path="/library/movies" /></Directory></MediaContainer>',
        )
        self.assertEqual(
            media._read_libraries(client, "plex"),
            [{"name": "Movies", "type": "movie", "paths": ["/library/movies"]}],
        )

    def test_plex_library_legacy_fallback_is_bounded_and_preserves_type_mapping(self):
        client = mock.Mock()
        client.request.side_effect = [
            ApiResponse(404, b"{}"),
            ApiResponse(200, b'{"MediaContainer":{"Directory":[]}}'),
        ]
        params = {
            "name": "Movies",
            "type": 1,
            "agent": "native",
            "scanner": "native",
            "language": "en",
            "locations": ["/library/movies"],
        }
        media._plex_library_request(client, "POST", params)
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(
            client.request.call_args.args[:2], ("POST", "/library/sections")
        )
        self.assertEqual(client.request.call_args.kwargs["params"]["type"], "movie")
        self.assertEqual(
            client.request.call_args.kwargs["params"]["location"], ["/library/movies"]
        )

    def test_library_response_without_expected_envelope_is_not_counted_as_empty_success(
        self,
    ):
        client = mock.Mock()
        client.request.return_value = ApiResponse(200, b"{}")
        with self.assertRaises(media.UnsupportedMediaVersion):
            media._read_libraries(client, "plex")

    def test_session_response_wrong_shape_cannot_mark_protection_verified(self):
        client = mock.Mock()
        client.request.return_value = ApiResponse(200, b"{}")
        factory = mock.Mock(return_value=client)
        result = media.inspect_media(
            {"service_key": "jellyfin", "source": "jellyfin", "action": "protection"},
            {"jellyfin": {"port": 8096}},
            {"arrangement": {"values": {"jellyfin::api_key": "private-fixture"}}},
            client_factory=factory,
        )
        self.assertEqual(result["execution"], "unsupported")
        self.assertEqual(result["verification"], "unavailable")

    def test_jellyfin_api_key_uses_canonical_authorization_and_rejects_real_control_characters(
        self,
    ):
        factory = mock.Mock()
        payload = {"arrangement": {"values": {"jellyfin::api_key": "fixture-key"}}}
        media._client(
            "jellyfin", "jellyfin", {"jellyfin": {"port": 8096}}, payload, factory
        )
        headers = factory.call_args.kwargs["headers"]
        self.assertIn('Token="fixture-key"', headers["Authorization"])
        for invalid in (
            "bad" + chr(13) + "key",
            "bad" + chr(10) + "key",
            "bad" + chr(92) + "key",
            'bad"key',
        ):
            payload["arrangement"]["values"]["jellyfin::api_key"] = invalid
            with self.assertRaises(media.MediaInputError):
                media._client(
                    "jellyfin",
                    "jellyfin",
                    {"jellyfin": {"port": 8096}},
                    payload,
                    factory,
                )


class CamelCaseJellyfinServer(FakeMediaServer):
    @staticmethod
    def response(value):
        def convert(item):
            if isinstance(item, dict):
                return {
                    key[:1].lower() + key[1:]: convert(child)
                    for key, child in item.items()
                }
            if isinstance(item, list):
                return [convert(child) for child in item]
            return item

        return ApiResponse(200, json.dumps(convert(value)).encode())

    def request(self, method, path, url, options, **kwargs):
        if path == "/Auth/Keys":
            self.calls.append(
                (method, path, copy.deepcopy(kwargs), copy.deepcopy(options), url)
            )
            if method == "POST":
                return ApiResponse(204)
            return self.response(
                {
                    "Items": [
                        {
                            "AppName": "DUMB Setup and Protection",
                            "AccessToken": "private-dedicated-fixture",
                        }
                    ]
                }
            )
        return super().request(method, path, url, options, **kwargs)


class JellyfinResponseContractTests(unittest.TestCase):
    def test_explicit_camel_case_freshness_auth_key_listing_and_library_readback(self):
        server = CamelCaseJellyfinServer("jellyfin", initialized=False)
        config = {"jellyfin": {"port": 8096, "enabled": True}}
        values = {
            "jellyfin::admin_username": "initial-admin",
            "jellyfin::admin_password": "private-password-fixture",
        }
        payload = {"arrangement": {"values": values}}
        action = {"source": "jellyfin", "service_key": "jellyfin", "action": "account"}
        preview = media.inspect_media(
            action, config, payload, client_factory=server.factory
        )
        self.assertEqual(preview["execution"], "planned")
        result = media.apply_media(
            action, config, payload, client_factory=server.factory
        )
        self.assertEqual(
            (result["execution"], result["verification"]), ("applied", "passed")
        )
        self.assertIn("jellyfin::admin_password", result["_clear_inputs"])
        values.update(result["_private_values"])
        values.pop("jellyfin::admin_password")
        existing = media.inspect_media(
            action, config, payload, client_factory=server.factory
        )
        self.assertEqual(existing["execution"], "already_present")
        values["jellyfin::libraries"] = [
            {
                "name": "Shows",
                "media_type": "show",
                "paths": ["/media/shows"],
                "create_if_missing": True,
            }
        ]
        action["action"] = "libraries"
        library = media.apply_media(
            action, config, payload, client_factory=server.factory
        )
        self.assertEqual(
            (library["execution"], library["verification"]), ("applied", "passed")
        )
        repeated = media.apply_media(
            action, config, payload, client_factory=server.factory
        )
        self.assertEqual(repeated["execution"], "already_present")
        self.assertEqual(len(server.libraries), 1)

    def test_conflicting_casing_and_non_boolean_freshness_never_authorize_initialization(
        self,
    ):
        for status in [
            {"StartupWizardCompleted": False, "startupWizardCompleted": True},
            {"startupWizardCompleted": 0},
            {"startupWizardCompleted": "false"},
            {"version": "12.2.0"},
        ]:
            with self.subTest(status=status):
                calls = []

                class Client:
                    def request(self, method, path, **kwargs):
                        calls.append((method, path))
                        return ApiResponse(200, json.dumps(status).encode())

                result = media.apply_media(
                    {
                        "source": "jellyfin",
                        "service_key": "jellyfin",
                        "action": "account",
                    },
                    {"jellyfin": {"port": 8096}},
                    {
                        "arrangement": {
                            "values": {
                                "jellyfin::admin_username": "initial-admin",
                                "jellyfin::admin_password": "private-password-fixture",
                            }
                        }
                    },
                    client_factory=lambda *args, **kwargs: Client(),
                )
                self.assertEqual(result["execution"], "unsupported")
                self.assertFalse(any(method == "POST" for method, _ in calls))

    def test_response_aliases_reject_equal_values_of_different_types(self):
        with self.assertRaises(media.UnsupportedMediaVersion):
            media._dto_value({"Id": 1, "id": True}, "Id", "id")
