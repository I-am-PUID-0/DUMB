import copy
import json
from pathlib import Path
import tempfile
import unittest

from utils import arrangement_external_links as links
from utils.arrangement_http import ApiResponse


class ExactHelperPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        xml = root / "config.xml"
        xml.write_text("<Config><ApiKey>private-source-key</ApiKey></Config>")
        native = root / "config"
        native.mkdir()
        self.neut_file = native / "radarr.json"
        self.neut_file.write_text(
            json.dumps(
                {
                    "instances": [
                        {
                            "name": "Default",
                            "api_url": "",
                            "api_key": "",
                            "enabled": False,
                            "search_missing": False,
                            "minimum_download_queue_size": 2,
                        }
                    ]
                }
            )
        )
        self.config = {
            "prowlarr": {"port": 9696, "config_file": str(xml)},
            "neutarr": {
                "port": 9705,
                "config_dir": str(root),
                "env": {"NEUTARR_CONFIG_DIR": str(native)},
            },
        }
        self.payload = {
            "arrangement": {
                "values": {
                    "setup::external_arrs": [
                        {
                            "id": "movies",
                            "service_key": "radarr",
                            "url": "https://arr.example.invalid",
                            "api_key": "private-target-key",
                            "authorize_changes": True,
                        }
                    ],
                    "prowlarr::arr_instances": ["external_arr.movies"],
                    "neutarr::arr_instances": ["external_arr.movies"],
                }
            }
        }
        self.apps, self.calls = [], []

    def factory(self, host, **kwargs):
        fake = self

        class Client:
            def request(self, method, path, **options):
                fake.calls.append((method, path, copy.deepcopy(options)))
                if path.endswith("/system/status"):
                    value = {"appName": "Radarr", "version": "6.4.4"}
                elif path.endswith("/applications/schema"):
                    value = [
                        {
                            "implementation": "Radarr",
                            "name": "Radarr",
                            "fields": [
                                {"name": name, "value": ""}
                                for name in ("baseUrl", "apiKey", "prowlarrUrl")
                            ],
                        }
                    ]
                elif path.endswith("/applications/test"):
                    value = "{}"
                elif "/applications" in path:
                    if method == "GET":
                        value = copy.deepcopy(fake.apps)
                        for row in value:
                            for field in row["fields"]:
                                if field["name"] == "apiKey":
                                    field["value"] = "********"
                    else:
                        value = copy.deepcopy(options["json"])
                        if method == "POST":
                            value["id"] = len(fake.apps) + 1
                            fake.apps.append(value)
                        else:
                            fake.apps[:] = [
                                value if row["id"] == value["id"] else row
                                for row in fake.apps
                            ]
                else:
                    return ApiResponse(404, b"{}")
                return ApiResponse(200, json.dumps(value).encode())

        return Client()

    def apply(self, family, operation="apply"):
        return links.apply_external_link(
            {
                "source": family,
                "service_key": family,
                "action": "arr_connection",
                "target": "external_arr.movies",
            },
            self.config,
            self.payload,
            operation,
            client_factory=self.factory,
            ownership_directory=self.temp.name,
        )

    def test_prowlarr_explicit_sync_policy_is_idempotent_and_endpoint_scoped(self):
        self.payload["arrangement"]["values"]["prowlarr::sync_level"] = "addOnly"
        result = self.apply("prowlarr")
        self.assertEqual(result["verification"], "passed", result)
        self.assertEqual(self.apps[0]["syncLevel"], "addOnly")
        self.assertEqual(self.apply("prowlarr")["execution"], "already_present")
        self.payload["arrangement"]["values"]["setup::external_arrs"][0][
            "api_key"
        ] = "rotated-private"
        result = self.apply("prowlarr")
        self.assertEqual(result["execution"], "waiting_input", result)
        self.assertNotIn("rotated-private", json.dumps(result))
        self.assertEqual(len(self.apps), 1)

    def test_stored_assignment_survives_missing_private_selection_on_preview(self):
        values = self.payload["arrangement"]["values"]
        self.config["prowlarr"]["arr_instances"] = values.pop("prowlarr::arr_instances")
        values["prowlarr::sync_level"] = "addOnly"
        self.assertEqual(self.apply("prowlarr")["verification"], "passed")
        result = self.apply("prowlarr", "preview")
        self.assertEqual(result["execution"], "already_present", result)
        self.assertFalse(result["evidence"]["changes"])

    def test_neutarr_consumes_empty_default_and_only_explicitly_enables(self):
        result = self.apply("neutarr")
        self.assertEqual(result["execution"], "applied", result)
        rows = json.loads(self.neut_file.read_text())["instances"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["enabled"])
        self.assertFalse(rows[0]["search_missing"])
        self.assertEqual(rows[0]["minimum_download_queue_size"], 2)
        self.payload["arrangement"]["values"]["neutarr::enable_integrations"] = True
        self.assertEqual(self.apply("neutarr")["execution"], "applied")
        self.assertTrue(
            json.loads(self.neut_file.read_text())["instances"][0]["enabled"]
        )
        self.assertEqual(self.apply("neutarr")["execution"], "already_present")

    def test_external_activation_requires_external_write_permission(self):
        self.payload["arrangement"]["values"]["setup::external_arrs"][0][
            "authorize_changes"
        ] = False
        self.payload["arrangement"]["values"]["prowlarr::sync_level"] = "fullSync"
        self.assertEqual(self.apply("prowlarr")["execution"], "waiting_input")
        self.assertEqual(self.apps, [])
        self.payload["arrangement"]["values"]["neutarr::enable_integrations"] = True
        self.assertEqual(self.apply("neutarr")["execution"], "waiting_input")
        self.assertEqual(
            json.loads(self.neut_file.read_text())["instances"][0]["name"], "Default"
        )
