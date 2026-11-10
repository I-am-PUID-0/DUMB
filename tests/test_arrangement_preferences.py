import copy
import json
from pathlib import Path
import tempfile
import unittest

from utils import arrangement_preferences as preferences
from utils.arrangement_http import ApiResponse


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        xml = Path(self.temp.name) / "config.xml"
        xml.write_text("<Config><ApiKey>fixture-key</ApiKey></Config>")
        self.config = {
            "lidarr": {"instances": {"Music": {"port": 8686, "config_file": str(xml)}}},
            "bazarr": {"port": 6767},
        }
        self.root = {
            "id": 1,
            "path": "/music",
            "defaultQualityProfileId": 1,
            "defaultMetadataProfileId": 1,
            "defaultTags": [9],
            "defaultMonitorOption": "none",
        }
        self.settings = {
            "general": {"enabled_providers": ["personal"]},
            "opensubtitlescom": {"username": "", "password": "", "use_hash": True},
            "personal": {"token": "keep-secret"},
        }
        self.calls = []
        self.provider_status = [{"name": "opensubtitlescom"}, {"name": "personal"}]

    def factory(self, host, **kwargs):
        fake = self

        class Client:
            def request(self, method, path, **options):
                fake.calls.append((method, path, copy.deepcopy(options)))
                if path.endswith("/rootfolder"):
                    value = [fake.root]
                elif path.endswith("/qualityprofile") or path.endswith(
                    "/metadataprofile"
                ):
                    value = [{"id": 1, "name": "Old"}, {"id": 2, "name": "Selected"}]
                elif "/rootfolder/" in path:
                    fake.root = copy.deepcopy(options["json"])
                    value = fake.root
                elif path == "/api/providers":
                    value = {"data": fake.provider_status}
                elif path == "/api/system/settings":
                    if method == "POST":
                        for key, value in options["data"].items():
                            _, section, field = key.split("-", 2)
                            fake.settings[section][field] = value
                    value = fake.settings
                else:
                    return ApiResponse(404, b"{}")
                return ApiResponse(200, json.dumps(value).encode())

        return Client()

    def apply(self, action, payload, operation):
        return preferences.apply_preferences(
            action,
            self.config,
            payload,
            operation,
            client_factory=self.factory,
            ownership_directory=self.temp.name,
        )

    def accept(self, action, payload):
        proposal = self.apply(action, payload, "preview")
        source = action["source"]
        payload["arrangement"]["values"][source + "::adopt_existing"] = True
        payload["arrangement"]["values"][source + "::adoption_preview_id"] = proposal[
            "evidence"
        ]["preview_id"]
        return proposal

    def test_lidarr_selected_profiles_preserve_root_policy_and_normalize_select_strings(
        self,
    ):
        action = {
            "source": "lidarr.Music",
            "service_key": "lidarr",
            "action": "arr_preferences",
        }
        payload = {
            "arrangement": {
                "values": {
                    "lidarr.Music::root_folder": "/music",
                    "lidarr.Music::quality_profile": "2",
                    "lidarr.Music::metadata_profile": "2",
                }
            }
        }
        fields = preferences.discover_fields(
            action, self.config, payload, client_factory=self.factory
        )
        self.assertTrue(fields[1]["options"])
        self.assertEqual(
            self.apply(action, payload, "apply")["execution"], "waiting_input"
        )
        self.accept(action, payload)
        result = self.apply(action, payload, "apply")
        self.assertEqual(result["verification"], "passed", result)
        self.assertEqual(self.root["defaultQualityProfileId"], 2)
        self.assertEqual(self.root["defaultMetadataProfileId"], 2)
        self.assertEqual(self.root["defaultTags"], [9])
        self.assertEqual(self.root["defaultMonitorOption"], "none")
        self.assertEqual(
            self.apply(action, payload, "apply")["execution"], "already_present"
        )

    def test_bazarr_provider_credentials_preserve_other_providers_and_do_not_claim_login(
        self,
    ):
        action = {
            "source": "bazarr",
            "service_key": "bazarr",
            "action": "subtitle_providers",
        }
        payload = {
            "arrangement": {
                "values": {
                    "bazarr::api_key": "bazarr-key",
                    "bazarr::providers": [
                        {
                            "id": "opensubtitlescom",
                            "enable": True,
                            "authorize_changes": True,
                        }
                    ],
                    "bazarr::provider::opensubtitlescom::username": "fixture-user",
                    "bazarr::provider::opensubtitlescom::password": "private-provider",
                }
            }
        }
        fields = preferences.discover_fields(
            action, self.config, payload, client_factory=self.factory
        )
        self.assertTrue(
            next(f for f in fields if f["key"].endswith("::password"))["secret"]
        )
        proposal = self.accept(action, payload)
        self.assertNotIn("private-provider", json.dumps(proposal))
        result = self.apply(action, payload, "apply")
        self.assertEqual(result["execution"], "applied", result)
        self.assertEqual(result["verification"], "unavailable")
        self.assertFalse(result["evidence"]["provider_login_verified"])
        self.assertEqual(self.settings["personal"]["token"], "keep-secret")
        self.assertEqual(
            set(self.settings["general"]["enabled_providers"]),
            {"personal", "opensubtitlescom"},
        )
        self.assertEqual(
            self.apply(action, payload, "apply")["execution"], "already_present"
        )

    def test_fresh_bazarr_empty_throttle_list_discovers_native_credentials_without_leaking_defaults(
        self,
    ):
        self.provider_status = []
        self.settings["auth"] = {
            "username": "private-admin",
            "password": "private-admin-password",
        }
        self.settings["opensubtitlescom"]["username"] = "private-existing-user"
        action = {
            "source": "bazarr",
            "service_key": "bazarr",
            "action": "subtitle_providers",
        }
        payload = {
            "arrangement": {
                "values": {
                    "bazarr::api_key": "bazarr-key",
                    "bazarr::providers": [
                        {
                            "id": "opensubtitlescom",
                            "enable": False,
                            "authorize_changes": True,
                        }
                    ],
                    "bazarr::provider::opensubtitlescom::password": "new-private-secret",
                }
            }
        }
        fields = preferences.discover_fields(
            action, self.config, payload, client_factory=self.factory
        )
        options = fields[0]["item_fields"][0]["options"]
        self.assertIn(
            {"value": "opensubtitlescom", "label": "opensubtitlescom"}, options
        )
        self.assertNotIn("auth", {row["value"] for row in options})
        username = next(
            field for field in fields if field["key"].endswith("::username")
        )
        self.assertTrue(username["secret"])
        self.assertNotIn("default", username)
        self.assertNotIn("private-existing-user", json.dumps(fields))
        proposal = self.accept(action, payload)
        self.assertNotIn("new-private-secret", json.dumps(proposal))
        result = self.apply(action, payload, "apply")
        self.assertEqual(result["execution"], "applied", result)
        self.assertEqual(
            self.settings["opensubtitlescom"]["password"], "new-private-secret"
        )
        self.assertEqual(self.settings["general"]["enabled_providers"], ["personal"])

    def test_unknown_quality_profile_never_writes(self):
        action = {
            "source": "lidarr.Music",
            "service_key": "lidarr",
            "action": "arr_preferences",
        }
        payload = {
            "arrangement": {
                "values": {
                    "lidarr.Music::root_folder": "/music",
                    "lidarr.Music::quality_profile": 999,
                    "lidarr.Music::metadata_profile": 2,
                }
            }
        }
        self.assertEqual(
            self.apply(action, payload, "apply")["execution"], "waiting_input"
        )
        self.assertFalse(any(method != "GET" for method, _, _ in self.calls))
