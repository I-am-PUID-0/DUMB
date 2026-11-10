import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from utils.arrangement_http import ApiError, ApiResponse
from utils import arrangement_streams as adapter


class Native:
    def __init__(self):
        self.profile = None
        self.calls = []
        self.fail_readback = False
        self.reads = 0
        self.reject = False
        self.lose_creation_response = False
        self.version = "2.36.0"

    def factory(self, url, **options):
        native = self

        class Client:
            def request(self, method, path, **kwargs):
                native.calls.append((url, options, method, path, copy.deepcopy(kwargs)))
                data = {}
                if path == "/api/v1/status":
                    data = {
                        "version": native.version,
                        "commit": "same-build",
                        "settings": {"baseUrl": "https://streams.example.com"},
                        "presets": [
                            {
                                "ID": "prowlarr",
                                "SUPPORTED_SERVICES": [
                                    "nzbdav",
                                    "realdebrid",
                                    "altmount",
                                ],
                            },
                            {"ID": "zilean", "SUPPORTED_SERVICES": ["realdebrid"]},
                        ],
                    }
                elif path == "/api/v1/system/status":
                    return ApiResponse(200, json.dumps({"version": "2.3"}).encode())
                elif path in {"/api", "/sabnzbd/api"}:
                    return ApiResponse(200, json.dumps({"version": "4.5"}).encode())
                elif path == "/api/v1/auth/login":
                    data = {"isAdmin": True}
                elif path == "/api/v1/user":
                    if native.reject:
                        raise ApiError(status_code=401)
                    if method == "GET":
                        native.reads += 1
                        if native.fail_readback and native.reads >= 1:
                            raise ApiError(status_code=503)
                        expected = (
                            "Basic "
                            + base64.b64encode(
                                b"profile-uuid:profile-password"
                            ).decode()
                        )
                        if kwargs.get("headers", {}).get("Authorization") != expected:
                            raise ApiError(status_code=401)
                        data = {"userData": copy.deepcopy(native.profile)}
                    elif method == "POST":
                        supplied = kwargs["json"]["config"]
                        if not isinstance(
                            supplied.get("sortCriteria", {}).get("global"), list
                        ) or not isinstance(
                            supplied.get("formatter", {}).get("id"), str
                        ):
                            raise ApiError(status_code=400)
                        if any("." in row["instanceId"] for row in supplied["presets"]):
                            raise ApiError(status_code=400)
                        native.profile = copy.deepcopy(kwargs["json"]["config"])
                        if native.lose_creation_response:
                            raise ApiError(status_code=503)
                        data = {"uuid": "profile-uuid"}
                    elif method == "PUT":
                        native.profile = copy.deepcopy(kwargs["json"]["config"])
                        data = {"uuid": "profile-uuid"}
                if path == "/api/v1/status":
                    data["settings"]["presets"] = data.pop("presets")
                return ApiResponse(
                    200, json.dumps({"success": True, "data": data}).encode()
                )

        return Client()


class StreamsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.native = Native()
        self.config = {
            "aiostreams": {
                "enabled": True,
                "config_dir": self.directory.name,
                "port": 3006,
                "base_url": "https://streams.example.com",
                "auth_username": "operator",
                "auth_password": "operator-password",
            },
            "infinidysk": {
                "enabled": True,
                "backend_port": 8080,
                "env": {"FRONTEND_BACKEND_API_KEY": "provider-private-key"},
            },
            "prowlarr": {
                "instances": {
                    "Main": {
                        "enabled": True,
                        "port": 9696,
                        "api_key": "indexer-private-key",
                        "config_dir": self.directory.name,
                    }
                }
            },
            "zilean": {"enabled": True, "port": 8181},
        }
        self.action = {
            "id": "aiostreams::backend_presets",
            "source": "aiostreams",
            "service_key": "aiostreams",
            "action": "backend_presets",
        }
        self.values = {
            "aiostreams::backend_presets": ["prowlarr.Main"],
            "aiostreams::streaming_engine": "infinidysk",
            "aiostreams::stream_profile_password": "profile-password",
            "aiostreams::create_stream_profile": True,
            "aiostreams::engine_webdav_username": "dav-user",
            "aiostreams::engine_webdav_password": "dav-private-password",
            "aiostreams::engine_public_url": "https://dav.example.com",
        }

    def call(self, apply=True):
        return (adapter.apply_streams if apply else adapter.inspect_streams)(
            self.action,
            self.config,
            {"arrangement": {"values": self.values}},
            client_factory=self.native.factory,
        )

    def existing(self):
        self.values["aiostreams::stream_profile_uuid"] = "profile-uuid"
        self.native.profile = {
            "presets": [
                {
                    "instanceId": "user-custom",
                    "type": "other",
                    "enabled": True,
                    "options": {"keep": "value"},
                }
            ],
            "services": [],
            "sortCriteria": ["user-choice"],
            "autoRemoveDownloads": False,
            "custom": {"keep": [1, 2]},
        }

    def test_create_readback_and_retry_reuse_selected_private_profile(self):
        result = self.call()
        self.assertEqual(
            ("applied", "passed"), (result["execution"], result["verification"])
        )
        self.assertEqual(
            {"aiostreams::stream_profile_uuid": "profile-uuid"},
            result["_private_values"],
        )
        self.assertNotIn("provider-private-key", json.dumps(result))
        self.assertNotIn("dav-private-password", json.dumps(result))
        self.assertEqual(
            ["nzbdav"], self.native.profile["presets"][0]["options"]["services"]
        )
        self.assertEqual(
            "http://127.0.0.1:9696",
            self.native.profile["presets"][0]["options"]["prowlarrUrl"],
        )
        self.values.update(result["_private_values"])
        self.native.calls.clear()
        self.assertEqual("already_present", self.call()["execution"])
        self.assertFalse(any(call[2] in {"PUT", "POST"} for call in self.native.calls))
        ledger = Path(self.directory.name) / ".dumb-stream-profile-ownership.json"
        self.assertEqual(0o600, ledger.stat().st_mode & 0o777)
        self.assertNotIn("provider-private-key", ledger.read_text())

    def test_altmount_readiness_uses_native_sab_prefix_and_selected_service(self):
        self.config["altmount"] = {
            "enabled": True,
            "port": 8088,
            "env": {"ALTMOUNT_API_KEY": "fixture-altmount-api-key"},
        }
        self.values["aiostreams::streaming_engine"] = "altmount"
        result = self.call()
        self.assertEqual("applied", result["execution"])
        calls = [
            call for call in self.native.calls if call[0] == "http://127.0.0.1:8088"
        ]
        self.assertEqual(["/sabnzbd/api"], [call[3] for call in calls])
        self.assertEqual("version", calls[0][4]["params"]["mode"])
        self.assertEqual(
            ["altmount"], self.native.profile["presets"][0]["options"]["services"]
        )

    def test_selected_altmount_defaults_only_from_live_verified_native_credentials(
        self,
    ):
        self.config["altmount"] = {
            "enabled": True,
            "port": 8088,
            "env": {"ALTMOUNT_API_KEY": "fixture-alt-key"},
        }
        self.values["aiostreams::streaming_engine"] = "altmount"
        self.values.pop("aiostreams::engine_webdav_username")
        self.values.pop("aiostreams::engine_webdav_password")
        with patch(
            "utils.arrangement_altmount_account.verified_webdav_credentials",
            return_value={
                "username": "native-user",
                "password": "native-private-password",
            },
        ) as verified:
            self.assertEqual("applied", self.call()["execution"])
            verified.assert_called_once()
        credentials = self.native.profile["services"][0]["credentials"]
        self.assertEqual("native-user", credentials["username"])
        self.assertEqual("native-private-password", credentials["password"])

    def test_explicit_consumer_credentials_are_never_replaced_by_native_defaults(self):
        self.config["altmount"] = {
            "enabled": True,
            "port": 8088,
            "env": {"ALTMOUNT_API_KEY": "fixture-alt-key"},
        }
        self.values["aiostreams::streaming_engine"] = "altmount"
        with patch(
            "utils.arrangement_altmount_account.verified_webdav_credentials",
            side_effect=AssertionError("explicit overrides must be preserved"),
        ):
            self.assertEqual("applied", self.call()["execution"])
        self.assertEqual(
            "dav-user", self.native.profile["services"][0]["credentials"]["username"]
        )

    def test_preview_never_logs_in_creates_or_writes_private_state(self):
        self.assertEqual("planned", self.call(False)["execution"])
        self.assertFalse(any(call[2] in {"PUT", "POST"} for call in self.native.calls))
        self.assertEqual([], list(Path(self.directory.name).iterdir()))

    def test_existing_preferences_preserved(self):
        self.existing()
        original = copy.deepcopy(self.native.profile)
        self.assertEqual("applied", self.call()["execution"])
        for key in ("sortCriteria", "autoRemoveDownloads", "custom"):
            self.assertEqual(original[key], self.native.profile[key])
        self.assertEqual(original["presets"][0], self.native.profile["presets"][0])

    def test_existing_user_owned_service_is_not_overwritten(self):
        self.existing()
        self.native.profile["services"] = [
            {
                "id": "nzbdav",
                "enabled": True,
                "credentials": {
                    "url": "https://user.example.com",
                    "apiKey": "user-private-key",
                },
            }
        ]
        before = copy.deepcopy(self.native.profile)
        self.assertEqual("waiting_input", self.call()["execution"])
        self.assertEqual(before, self.native.profile)
        self.assertFalse(any(call[2] in {"PUT", "POST"} for call in self.native.calls))

    def test_managed_user_edit_blocks_followup_update(self):
        result = self.call()
        self.values.update(result["_private_values"])
        self.native.profile["presets"][0]["options"]["name"] = "My choice"
        self.config["prowlarr"]["instances"]["Main"]["port"] = 9697
        before = copy.deepcopy(self.native.profile)
        self.assertEqual("waiting_input", self.call()["execution"])
        self.assertEqual(before, self.native.profile)

    def test_lost_creation_response_blocks_duplicate_and_can_recover_existing_uuid(
        self,
    ):
        self.native.lose_creation_response = True
        self.assertEqual("waiting_input", self.call()["execution"])
        self.assertIsNotNone(self.native.profile)
        self.native.lose_creation_response = False
        self.assertEqual("waiting_input", self.call()["execution"])
        self.assertEqual(
            1,
            sum(
                call[2] == "POST" and call[3] == "/api/v1/user"
                for call in self.native.calls
            ),
        )
        self.values["aiostreams::stream_profile_uuid"] = "profile-uuid"
        self.assertEqual("already_present", self.call()["execution"])
        ledger = json.loads(
            (
                Path(self.directory.name) / ".dumb-stream-profile-ownership.json"
            ).read_text()
        )
        self.assertNotIn("_pending_creation", ledger)

    def test_post_creation_pending_readback_keeps_uuid_for_retry(self):
        self.native.fail_readback = True
        result = self.call()
        self.assertEqual("waiting_dependency", result["execution"])
        self.assertEqual(
            "profile-uuid", result["_private_values"]["aiostreams::stream_profile_uuid"]
        )
        self.values.update(result["_private_values"])
        self.native.fail_readback = False
        self.assertEqual("already_present", self.call()["execution"])
        self.assertEqual(
            1,
            sum(
                call[2] == "POST" and call[3] == "/api/v1/user"
                for call in self.native.calls
            ),
        )

    def test_wrong_profile_password_preserves_everything(self):
        self.existing()
        self.values["aiostreams::stream_profile_password"] = "wrong-password"
        before = copy.deepcopy(self.native.profile)
        self.assertEqual("waiting_input", self.call()["execution"])
        self.assertEqual(before, self.native.profile)

    def test_zilean_requires_debrid_instead_of_implicitly_using_usenet(self):
        self.values["aiostreams::backend_presets"] = ["zilean"]
        result = self.call()
        self.assertEqual("waiting_input", result["execution"])
        self.assertIn("Zilean", result["reason"])
        self.assertIsNone(self.native.profile)

    def test_disabled_backend_and_inherited_profile_are_not_modified(self):
        self.config["prowlarr"]["instances"]["Main"]["enabled"] = False
        self.assertEqual("waiting_input", self.call()["execution"])
        self.config["prowlarr"]["instances"]["Main"]["enabled"] = True
        self.existing()
        self.native.profile["parentConfig"] = {"uuid": "parent-private"}
        self.assertEqual("waiting_input", self.call()["execution"])

    def test_public_route_check_sends_no_profile_or_operator_credentials(self):
        self.action["action"] = "public_origin"
        result = self.call(False)
        self.assertEqual("passed", result["verification"])
        public = [call for call in self.native.calls if call[0].startswith("https:")]
        self.assertEqual(1, len(public))
        self.assertEqual({}, public[0][4])
        self.assertTrue(public[0][1]["verify_tls"])
        self.assertFalse(public[0][1]["allow_private"])

    def test_public_origin_requires_https_and_excludes_embedded_address(self):
        self.action["action"] = "public_origin"
        for url in (
            "http://streams.example.com",
            "https://streams.example.com/ui/aiostreams",
            "https://user:secret@streams.example.com",
        ):
            with self.subTest(url=url):
                self.config["aiostreams"]["base_url"] = url
                self.assertEqual("waiting_input", self.call()["execution"])

    def test_malformed_status_never_writes(self):
        self.native.version = "1.99.0"
        self.assertEqual("unsupported", self.call()["execution"])
        self.assertIsNone(self.native.profile)

    def test_xml_base_path_and_generated_api_key_are_used(self):
        path = Path(self.directory.name) / "config.xml"
        path.write_text(
            "<Config><ApiKey>xml-private-key</ApiKey><UrlBase>/indexers</UrlBase></Config>"
        )
        self.assertEqual("applied", self.call()["execution"])
        options = self.native.profile["presets"][0]["options"]
        self.assertEqual("http://127.0.0.1:9696/indexers", options["prowlarrUrl"])
        self.assertEqual("xml-private-key", options["prowlarrApiKey"])

    def test_schema_advertises_private_handoff_and_explicit_profile_choice(self):
        fields = {
            field["key"]: field
            for field in adapter.schema_fields(
                {"id": "aiostreams", "service_key": "aiostreams"}, self.config
            )
        }
        self.assertEqual(
            "password", fields["aiostreams::stream_profile_password"]["type"]
        )
        self.assertEqual("boolean", fields["aiostreams::create_stream_profile"]["type"])
        self.assertIn("aiostreams::stream_profile_uuid", fields)
