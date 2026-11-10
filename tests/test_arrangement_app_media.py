"""Native application API-shaped media connection fixtures, without services."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from utils import arrangement_app_media as app
from utils.arrangement_http import ApiResponse


class FakeApp:
    def __init__(self, service, media_type):
        self.service, self.media_type = service, media_type
        self.calls = []
        self.main = {
            "mediaServerType": {"plex": 1, "jellyfin": 2, "emby": 3}[media_type],
            "existing_policy": True,
        }
        self.user = {"id": 1, "plexId": 123}
        self.seerr = {
            "ip": "",
            "port": 32400 if media_type == "plex" else 8096,
            "useSsl": False,
            "libraries": [{"id": "preserved", "enabled": False}],
            "future_setting": True,
        }
        if media_type != "plex":
            self.seerr.update(apiKey="", serverId="", urlBase="")
        else:
            self.seerr["machineId"] = ""
        self.settings = {
            "media_server_type": None,
            "plex_hostname": "",
            "plex_port": 32400,
            "plex_ssl": 0,
            "plex_name": "",
            "plex_machine_id": "",
            "plex_manual_mode": 0,
            "existing_retention_policy": True,
            "rules_handler_job_cron": "0 0 * * *",
        }
        self.specific = {media_type + "_url": "", media_type + "_api_key": ""}
        self.fail_test = False
        self.wrong_identity = False
        self.companions = {
            "seerr": {"url": "", "api_key": ""},
            "tautulli": {"url": "", "api_key": ""},
        }
        self.ignore_companion_write = False

    @staticmethod
    def response(value):
        return ApiResponse(200, json.dumps(value).encode())

    def factory(self, url, **options):
        outer = self

        class Client:
            def request(self, method, path, **kwargs):
                outer.calls.append(
                    (url, method, path, copy.deepcopy(kwargs), copy.deepcopy(options))
                )
                if ":32400" in url:
                    return outer.response(
                        {
                            "MediaContainer": {
                                "machineIdentifier": "media-id-fixture",
                                "friendlyName": "Fixture Media",
                            }
                        }
                    )
                if ":8096" in url:
                    return outer.response(
                        {"Id": "media-id-fixture", "ServerName": "Fixture Media"}
                    )
                return outer.request(method, path, **kwargs)

        return Client()

    def request(self, method, path, **kwargs):
        if path == "/api/v1/settings/main":
            return self.response(self.main)
        if path == "/api/v1/auth/me":
            return self.response(self.user)
        if path.startswith("/api/v1/settings/"):
            if method == "POST":
                self.seerr.update(kwargs["json"])
                self.seerr["machineId" if self.media_type == "plex" else "serverId"] = (
                    "wrong" if self.wrong_identity else "media-id-fixture"
                )
            return self.response(self.seerr)
        if path in {"/api/settings/test/seerr", "/api/settings/test/tautulli"}:
            return self.response(
                {
                    "status": "NOK" if self.fail_test else "OK",
                    "code": 0 if self.fail_test else 1,
                }
            )
        if path in {"/api/settings/seerr", "/api/settings/tautulli"}:
            family = path.rsplit("/", 1)[1]
            if method == "POST" and not self.ignore_companion_write:
                self.companions[family].update(kwargs["json"])
            return self.response(self.companions[family])
        if path == "/api/settings":
            if method == "PATCH":
                self.settings.update(kwargs["json"])
            return self.response(self.settings)
        if path == "/api/settings/plex/token":
            self.settings["plex_auth_token"] = "masked"
            return self.response({"status": "OK", "code": 1})
        if path in {"/api/settings/jellyfin/test", "/api/settings/emby/test"}:
            return self.response(
                {
                    "status": "NOK" if self.fail_test else "OK",
                    "code": 0 if self.fail_test else 1,
                }
            )
        if path in {"/api/settings/jellyfin", "/api/settings/emby"}:
            if method == "POST":
                self.specific.update(kwargs["json"])
                self.settings["media_server_type"] = self.media_type
            return self.response(self.specific)
        if path == "/api/media-server":
            return self.response(
                {
                    "machineId": (
                        "wrong-id" if self.wrong_identity else "media-id-fixture"
                    ),
                    "version": "fixture",
                }
            )
        raise AssertionError((method, path))


class AppMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, service="seerr", media_type="plex"):
        directory = self.root / service
        directory.mkdir(exist_ok=True)
        (directory / "settings.json").write_text(
            '{"main":{"apiKey":"private-source-fixture"}}'
        )
        preferences = self.root / "Preferences.xml"
        preferences.write_text('<Preferences PlexOnlineToken="private-plex-fixture"/>')
        source = "seerr.Default" if service == "seerr" else service
        cfg = {
            "enabled": True,
            "port": 5055 if service == "seerr" else 6246,
            "config_dir": str(directory),
        }
        config = {
            service: {"instances": {"Default": cfg}} if service == "seerr" else cfg,
            media_type: {
                "enabled": True,
                "port": 32400 if media_type == "plex" else 8096,
                "config_file": str(preferences),
            },
        }
        action = {
            "source": source,
            "service_key": service,
            "action": "media_connection",
        }
        payload = {
            "arrangement": {
                "values": {
                    source + "::media_server": media_type,
                    media_type + "::api_key": "private-media-fixture",
                }
            }
        }
        return config, action, payload, FakeApp(service, media_type)

    def invoke(self, fixture, *, inspect=False):
        config, action, payload, fake = fixture
        handler = app.inspect_app_media if inspect else app.apply_app_media
        return handler(
            action,
            config,
            payload,
            client_factory=fake.factory,
            ownership_directory=self.root / "ownership",
        )

    def test_seerr_selected_plex_and_jellyfin_links_create_and_read_back_without_library_activation(
        self,
    ):
        for kind in ("plex", "jellyfin", "emby"):
            with self.subTest(kind=kind):
                fixture = self.fixture(media_type=kind)
                self.assertEqual(
                    self.invoke(fixture, inspect=True)["execution"], "planned"
                )
                self.assertFalse(any(call[1] == "POST" for call in fixture[3].calls))
                result = self.invoke(fixture)
                self.assertEqual(result["execution"], "applied")
                self.assertEqual(result["verification"], "passed")
                self.assertFalse(result["evidence"]["library_sync_started"])
                self.assertEqual(
                    fixture[3].seerr["libraries"],
                    [{"id": "preserved", "enabled": False}],
                )
                self.assertTrue(fixture[3].seerr["future_setting"])
                self.assertFalse(
                    any(call[2].endswith("/sync") for call in fixture[3].calls)
                )
                self.assertNotIn("private-", json.dumps(result))
                self.assertEqual(self.invoke(fixture)["execution"], "already_present")

    def test_seerr_plex_owner_authorization_is_a_real_manual_prerequisite(self):
        fixture = self.fixture()
        fixture[3].user.pop("plexId")
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "waiting_input")
        self.assertIn("Plex owner", result["reason"])
        self.assertFalse(any(call[1] == "POST" for call in fixture[3].calls))

    def test_established_media_type_is_never_switched(self):
        fixture = self.fixture()
        fixture[3].main["mediaServerType"] = 2
        self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")
        self.assertFalse(any(call[1] == "POST" for call in fixture[3].calls))
        fixture = self.fixture("maintainerr", "jellyfin")
        fixture[3].settings["media_server_type"] = "plex"
        self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")
        self.assertFalse(any(call[1] == "POST" for call in fixture[3].calls))

    def test_unowned_endpoint_and_recorded_external_edits_are_preserved(self):
        fixture = self.fixture()
        fixture[3].seerr["ip"] = "custom.example.invalid"
        original = copy.deepcopy(fixture[3].seerr)
        self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")
        self.assertEqual(fixture[3].seerr, original)
        fixture = self.fixture("maintainerr", "plex")
        self.assertEqual(self.invoke(fixture)["execution"], "applied")
        fixture[3].settings["plex_hostname"] = "custom.example.invalid"
        self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")

    def test_existing_server_identity_mismatch_requires_migration(self):
        fixture = self.fixture()
        fixture[3].seerr["machineId"] = "different-server"
        self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")
        self.assertFalse(any(call[1] == "POST" for call in fixture[3].calls))

    def test_wrong_readback_identity_never_passes_verification(self):
        fixture = self.fixture()
        fixture[3].wrong_identity = True
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "failed")
        self.assertEqual(result["verification"], "failed")

    def test_maintainerr_plex_jellyfin_emby_preserve_policies_and_verify_actual_connector(
        self,
    ):
        for kind in ("plex", "jellyfin", "emby"):
            with self.subTest(kind=kind):
                fixture = self.fixture("maintainerr", kind)
                result = self.invoke(fixture)
                self.assertEqual(result["execution"], "applied")
                self.assertEqual(result["verification"], "passed")
                self.assertTrue(fixture[3].settings["existing_retention_policy"])
                self.assertEqual(
                    fixture[3].settings["rules_handler_job_cron"], "0 0 * * *"
                )
                self.assertFalse(result["evidence"]["policies_activated"])
                self.assertEqual(self.invoke(fixture)["execution"], "already_present")
                self.assertFalse(
                    any(
                        "collection" in call[2]
                        or "rules" in call[2]
                        or "/switch" in call[2]
                        for call in fixture[3].calls
                    )
                )

    def test_native_application_rejection_never_saves_target(self):
        fixture = self.fixture("maintainerr", "jellyfin")
        fixture[3].fail_test = True
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "waiting_input")
        self.assertFalse(
            any(
                call[1] == "POST" and call[2] == "/api/settings/jellyfin"
                for call in fixture[3].calls
            )
        )

    def test_active_maintainerr_connector_identity_must_match_selected_server(self):
        fixture = self.fixture("maintainerr", "plex")
        fixture[3].wrong_identity = True
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "waiting_dependency")
        self.assertNotEqual(result["verification"], "passed")

    def test_external_credentials_are_never_replaced_with_managed_credentials(self):
        fixture = self.fixture()
        source = fixture[1]["source"]
        fixture[2]["arrangement"]["values"].update(
            {
                source + "::media_server": "external",
                source
                + "::external_media": {
                    "service_key": "jellyfin",
                    "url": "https://media.example.invalid/base",
                    "api_key": "private-external-fixture",
                    "allow_private": False,
                    "verify_tls": True,
                },
            }
        )
        fixture[3].main["mediaServerType"] = 2
        fixture[3].media_type = "jellyfin"
        fixture[3].seerr.update(port=8096, urlBase="", apiKey="", serverId="")
        original = fixture[3].factory

        def factory(url, **options):
            if url.startswith("https://media"):
                self.assertIn(
                    'Token="private-external-fixture"',
                    options["headers"]["Authorization"],
                )
                self.assertFalse(options["allow_private"])
                self.assertTrue(options["verify_tls"])
                return type(
                    "Client",
                    (),
                    {
                        "request": lambda self, *args, **kwargs: FakeApp.response(
                            {"Id": "media-id-fixture", "ServerName": "Fixture"}
                        )
                    },
                )()
            return original(url, **options)

        result = app.apply_app_media(
            fixture[1],
            fixture[0],
            fixture[2],
            client_factory=factory,
            ownership_directory=self.root / "owned",
        )
        self.assertEqual(result["execution"], "applied")
        self.assertEqual(fixture[3].seerr["urlBase"], "/base")

    def test_pulsarr_and_aiostreams_manual_authorization_is_explicit(self):
        for service in ("pulsarr", "aiostreams"):
            result = app.apply_app_media(
                {
                    "service_key": service,
                    "source": service,
                    "action": "media_connection",
                },
                {},
                {},
                client_factory=mock.Mock(),
            )
            self.assertEqual(result["execution"], "waiting_input")
            self.assertEqual(result["verification"], "unavailable")

    def test_missing_selected_server_never_defaults_to_first_enabled_server(self):
        fixture = self.fixture()
        fixture[2]["arrangement"]["values"] = {}
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "waiting_input")
        self.assertFalse(fixture[3].calls)

    def companion_fixture(self, family="seerr"):
        fixture = self.fixture("maintainerr", "plex")
        config, action, payload, fake = fixture
        action["action"] = family + "_connection"
        if family == "seerr":
            directory = self.root / "selected-seerr"
            directory.mkdir(exist_ok=True)
            (directory / "settings.json").write_text(
                '{"main":{"apiKey":"private-selected-seerr"}}'
            )
            config["seerr"] = {
                "instances": {
                    "Unselected": {"enabled": True, "port": 5055},
                    "Chosen": {
                        "enabled": True,
                        "port": 5056,
                        "base_path": "/chosen",
                        "config_dir": str(directory),
                    },
                }
            }
            payload["arrangement"]["values"][
                "maintainerr::seerr_instance"
            ] = "seerr.Chosen"
        else:
            ini = self.root / "tautulli.ini"
            ini.write_text(
                "[General]\napi_key = private-selected-tautulli\nhttp_root = /stats\n"
            )
            config["tautulli"] = {
                "enabled": True,
                "port": 8182,
                "config_file": str(ini),
            }
            payload["arrangement"]["values"]["maintainerr::connect_tautulli"] = True
        return fixture

    def test_selected_companions_authenticate_and_read_back_without_policy_activation(
        self,
    ):
        for family, url in (
            ("seerr", "http://127.0.0.1:5056/chosen"),
            ("tautulli", "http://127.0.0.1:8182/stats"),
        ):
            with self.subTest(family=family):
                fixture = self.companion_fixture(family)
                self.assertEqual(
                    self.invoke(fixture, inspect=True)["execution"], "planned"
                )
                self.assertFalse(any(call[1] == "POST" for call in fixture[3].calls))
                result = self.invoke(fixture)
                self.assertEqual(result["execution"], "applied")
                self.assertEqual(result["verification"], "passed")
                self.assertEqual(fixture[3].companions[family]["url"], url)
                self.assertFalse(result["evidence"]["policies_activated"])
                self.assertTrue(fixture[3].settings["existing_retention_policy"])
                self.assertNotIn("private-", json.dumps(result))
                self.assertEqual(self.invoke(fixture)["execution"], "already_present")
                ledgers = list((self.root / "ownership").glob("*.json"))
                self.assertTrue(
                    all(path.stat().st_mode & 0o777 == 0o600 for path in ledgers)
                )

    def test_companion_selection_is_explicit_and_never_defaults_to_first_instance(self):
        for family, key in (
            ("seerr", "seerr_instance"),
            ("tautulli", "connect_tautulli"),
        ):
            with self.subTest(family=family):
                fixture = self.companion_fixture(family)
                fixture[2]["arrangement"]["values"].pop("maintainerr::" + key)
                self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")
                self.assertFalse(fixture[3].calls)

    def test_companion_existing_user_connection_and_manual_credential_changes_are_preserved(
        self,
    ):
        fixture = self.companion_fixture()
        fixture[3].companions["seerr"] = {
            "url": "https://user.example.invalid",
            "api_key": "private-user",
        }
        before = copy.deepcopy(fixture[3].companions)
        self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")
        self.assertEqual(fixture[3].companions, before)
        fixture = self.companion_fixture("tautulli")
        self.assertEqual(self.invoke(fixture)["execution"], "applied")
        fixture[3].companions["tautulli"]["api_key"] = "manually-replaced"
        self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")
        self.assertEqual(
            fixture[3].companions["tautulli"]["api_key"], "manually-replaced"
        )

    def test_companion_native_auth_failure_prevents_save_and_bad_readback_never_passes(
        self,
    ):
        fixture = self.companion_fixture()
        fixture[3].fail_test = True
        self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")
        self.assertFalse(
            any(
                call[1] == "POST" and call[2] == "/api/settings/seerr"
                for call in fixture[3].calls
            )
        )
        fixture = self.companion_fixture()
        fixture[3].ignore_companion_write = True
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "failed")
        self.assertEqual(result["verification"], "failed")

    def test_companion_schema_lists_exact_enabled_and_selected_instances(self):
        fixture = self.companion_fixture()
        fixture[0]["seerr"]["instances"]["Disabled"] = {"enabled": False}
        fields = app.schema_fields(
            {"service_key": "maintainerr", "id": "maintainerr"},
            fixture[0],
            {"core_services": [{"name": "seerr", "instance_name": "New"}]},
        )
        field = next(
            item for item in fields if item["key"] == "maintainerr::seerr_instance"
        )
        choices = [item["value"] for item in field["options"]]
        self.assertIn("seerr.Chosen", choices)
        self.assertIn("seerr.New", choices)
        self.assertNotIn("seerr.Disabled", choices)

    def test_external_header_control_characters_are_rejected_before_any_request(self):
        for key in (
            "line" + chr(10) + "break",
            "line" + chr(13) + "break",
            "quote" + chr(34),
            "backslash" + chr(92),
        ):
            fixture = self.fixture()
            source = fixture[1]["source"]
            fixture[2]["arrangement"]["values"].update(
                {
                    source + "::media_server": "external",
                    source
                    + "::external_media": {
                        "service_key": "jellyfin",
                        "url": "https://media.example.invalid",
                        "api_key": key,
                    },
                }
            )
            self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")
            self.assertFalse(fixture[3].calls)

    def test_jellyfin12_identity_aliases_are_supported_and_conflicting_aliases_are_rejected(
        self,
    ):
        for document, expected in (
            ({"id": "media-id-fixture", "serverName": "Fixture Media"}, "applied"),
            ({"Id": "old", "id": "new", "serverName": "Fixture Media"}, "unsupported"),
        ):
            fixture = self.fixture(media_type="jellyfin")
            original = fixture[3].factory

            def factory(url, **options):
                if ":8096" in url:
                    return type(
                        "Client",
                        (),
                        {
                            "request": lambda self, *args, **kwargs: FakeApp.response(
                                document
                            )
                        },
                    )()
                return original(url, **options)

            result = app.apply_app_media(
                fixture[1],
                fixture[0],
                fixture[2],
                client_factory=factory,
                ownership_directory=self.root / "owned-alias",
            )
            self.assertEqual(result["execution"], expected)
