import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from utils import arrangement_helpers as helpers
from utils import neutarr_settings
from utils.arrangement_http import ApiResponse


class Manager:
    def __init__(self, config):
        self.config, self.saved = config, []

    def get(self, key, default=None):
        return self.config.get(key, default)

    def save_config(self):
        self.saved.append(copy.deepcopy(self.config))


class CompanionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        xml = self.root / "arr.xml"
        xml.write_text(
            "<Config><ApiKey>fixture-arr-key</ApiKey><UrlBase>/native</UrlBase></Config>"
        )
        self.config = {
            "puid": os.getuid(),
            "pgid": os.getgid(),
            "radarr": {
                "instances": {
                    "Movies": {
                        "enabled": True,
                        "use_neutarr": False,
                        "use_profilarr": False,
                        "port": 7878,
                        "config_file": str(xml),
                    }
                }
            },
            "neutarr": {
                "instances": {
                    "Automation": {
                        "enabled": True,
                        "port": 9705,
                        "config_dir": str(self.root / "selected"),
                        "env": {
                            "NEUTARR_CONFIG_DIR": str(self.root / "selected" / "config")
                        },
                    },
                    "Unrelated": {
                        "enabled": True,
                        "port": 9706,
                        "config_dir": str(self.root / "unrelated"),
                    },
                }
            },
        }
        self.manager = Manager(self.config)
        self.action = {
            "source": "neutarr.Automation",
            "service_key": "neutarr",
            "instance_key": "Automation",
            "action": "arr_connections",
        }
        self.payload = {
            "arrangement": {
                "values": {"neutarr.Automation::arr_instances": ["radarr.Movies"]}
            }
        }
        self.owner_patch = mock.patch.object(
            neutarr_settings, "_OWNERSHIP_DIRECTORY", str(self.root / "ownership")
        )
        self.owner_patch.start()
        self.addCleanup(self.owner_patch.stop)

    def apply(self):
        return helpers.apply_helper(
            self.action, self.config, self.payload, manager=self.manager
        )

    def test_explicit_assignment_overrides_legacy_optout_without_enabling_search(self):
        outcome = self.apply()
        self.assertEqual(outcome["execution"], "waiting_input", outcome)
        saved = json.loads((self.root / "selected/config/radarr.json").read_text())
        self.assertEqual(len(saved["instances"]), 1)
        self.assertEqual(
            saved["instances"][0]["api_url"], "http://127.0.0.1:7878/native"
        )
        self.assertFalse(saved["instances"][0]["enabled"])
        self.assertEqual(
            self.config["neutarr"]["instances"]["Automation"]["arr_instances"],
            ["radarr.Movies"],
        )
        self.assertTrue(self.manager.saved)
        self.assertFalse((self.root / "unrelated").exists())

    def test_native_ui_enables_connection_and_recheck_reports_readback_only(self):
        self.apply()
        path = self.root / "selected/config/radarr.json"
        native = json.loads(path.read_text())
        native["instances"][0] = {
            key: value
            for key, value in native["instances"][0].items()
            if not key.startswith("_dumb")
        }
        native["instances"][0]["enabled"] = True
        path.write_text(json.dumps(native))
        result = self.apply()
        self.assertEqual(result["execution"], "already_present", result)
        self.assertEqual(result["verification"], "unavailable")
        self.assertIn("configuration_readback", json.dumps(result["evidence"]))
        self.assertNotIn("fixture-arr-key", json.dumps(result))

    def test_explicit_empty_assignment_retires_only_owned_connections(self):
        self.apply()
        path = self.root / "selected/config/radarr.json"
        native = json.loads(path.read_text())
        native["instances"][0]["enabled"] = True
        personal = {
            "name": "Personal",
            "enabled": True,
            "api_url": "https://example.invalid",
            "api_key": "fixture-personal",
        }
        native["instances"].append(personal)
        path.write_text(json.dumps(native))
        self.payload["arrangement"]["values"]["neutarr.Automation::arr_instances"] = []
        result = self.apply()
        self.assertEqual(result["execution"], "already_present", result)
        saved = json.loads(path.read_text())["instances"]
        self.assertFalse(saved[0]["enabled"])
        self.assertEqual(saved[1], personal)

    def test_missing_or_unsupported_assignment_requires_input(self):
        self.payload["arrangement"]["values"]["neutarr.Automation::arr_instances"] = [
            "radarr.Missing"
        ]
        self.assertEqual(self.apply()["execution"], "waiting_input")
        self.assertFalse(self.manager.saved)
        self.assertFalse((self.root / "selected").exists())

    def test_readonly_inspection_never_creates_missing_native_configuration(self):
        result = helpers.inspect_helper(self.action, self.config, self.payload)
        self.assertEqual(result["execution"], "waiting_dependency")
        self.assertFalse(self.manager.saved)
        self.assertFalse((self.root / "selected").exists())

    def test_prowlarr_readback_and_supported_connection_test_are_distinct(self):
        xml = self.root / "prowlarr.xml"
        xml.write_text("<Config><ApiKey>fixture-source-key</ApiKey></Config>")
        self.config["prowlarr"] = {
            "instances": {
                "Indexer": {"enabled": True, "port": 9696, "config_file": str(xml)}
            }
        }
        action = {
            "source": "prowlarr.Indexer",
            "service_key": "prowlarr",
            "action": "arr_connections",
        }
        payload = {
            "arrangement": {
                "values": {"prowlarr.Indexer::arr_instances": ["radarr.Movies"]}
            }
        }
        row = {
            "id": 7,
            "implementation": "Radarr",
            "enable": True,
            "fields": [{"name": "baseUrl", "value": "http://127.0.0.1:7878/native"}],
        }
        calls = []

        class Client:
            def request(self, method, path, **kwargs):
                calls.append((method, path))
                return (
                    ApiResponse(200, json.dumps([row]).encode())
                    if method == "GET"
                    else ApiResponse(204)
                )

        factory = lambda *args, **kwargs: Client()
        inspected = helpers.inspect_helper(
            action, self.config, payload, "inspect", client_factory=factory
        )
        self.assertEqual(inspected["verification"], "unavailable")
        self.assertEqual(calls, [("GET", "/api/v1/applications")])
        checked = helpers.inspect_helper(
            action, self.config, payload, "recheck", client_factory=factory
        )
        self.assertEqual(checked["verification"], "passed", checked)
        self.assertEqual(calls[-1], ("POST", "/api/v1/applications/test"))
        row["syncLevel"] = "disabled"
        calls.clear()
        disabled = helpers.inspect_helper(
            action, self.config, payload, "recheck", client_factory=factory
        )
        self.assertEqual(disabled["execution"], "waiting_input")
        self.assertEqual(calls, [("GET", "/api/v1/applications")])
        row.pop("syncLevel")
        calls.clear()

        class V3Client(Client):
            def request(self, method, path, **kwargs):
                if path.startswith("/api/v1"):
                    calls.append((method, path))
                    return ApiResponse(404)
                return super().request(method, path, **kwargs)

        migrated = helpers.inspect_helper(
            action,
            self.config,
            payload,
            "recheck",
            client_factory=lambda *args, **kwargs: V3Client(),
        )
        self.assertEqual(migrated["verification"], "passed", migrated)
        self.assertEqual(
            calls,
            [
                ("GET", "/api/v1/applications"),
                ("GET", "/api/v3/applications"),
                ("POST", "/api/v3/applications/test"),
            ],
        )
        calls.clear()

        class DeniedClient(Client):
            def request(self, method, path, **kwargs):
                calls.append((method, path))
                return ApiResponse(401)

        denied = helpers.inspect_helper(
            action,
            self.config,
            payload,
            "recheck",
            client_factory=lambda *args, **kwargs: DeniedClient(),
        )
        self.assertEqual(denied["execution"], "waiting_input")
        self.assertEqual(calls, [("GET", "/api/v1/applications")])

    def test_foreign_configuration_manager_is_not_mutated(self):
        foreign = Manager(copy.deepcopy(self.config))
        outcome = helpers.apply_helper(
            self.action, self.config, self.payload, manager=foreign
        )
        self.assertEqual(outcome["execution"], "failed")
        self.assertFalse(foreign.saved)
        self.assertFalse((self.root / "selected").exists())
