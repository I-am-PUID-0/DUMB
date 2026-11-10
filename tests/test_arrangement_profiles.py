import copy
import json
from pathlib import Path
import tempfile
import unittest

from utils import arrangement_profiles as profiles
from utils.arrangement_http import ApiResponse


def response(value, status=200):
    return ApiResponse(status, json.dumps(value).encode())


def flatten(value):
    result = []

    def add(item):
        index = len(result)
        result.append(None)
        if isinstance(item, dict):
            result[index] = {key: add(child) for key, child in item.items()}
        elif isinstance(item, list):
            result[index] = [add(child) for child in item]
        else:
            result[index] = item
        return index

    add(value)
    return result


class FakeProfilarr:
    def __init__(self, version):
        self.version = version
        self.rows = [
            {
                "id": 1,
                "name": "Movies",
                "type": "radarr",
                "url": "http://127.0.0.1:7878",
                "arrServer": "http://127.0.0.1:7878",
                "apiKey": "arr-key",
                "tags": ["personal"],
                "data_to_sync": {"profiles": ["Personal"], "customFormats": ["Keep"]},
                "sync_method": "schedule",
                "sync_interval": 60,
                "import_as_unique": False,
            }
        ]
        self.selection = [{"databaseId": 7, "profileName": "Personal"}]
        self.arr_profiles = [{"id": 1, "name": "Personal"}]
        self.calls = []
        self.fail_form = False
        self.sync_pending = False

    def factory(self, host, **kwargs):
        fake = self

        class Client:
            base_url = host
            cookies = {}

            def request(self, method, path, **options):
                fake.calls.append((method, path, copy.deepcopy(options)))
                if ":7878" in host:
                    if path.endswith("system/status"):
                        return response({"appName": "Radarr", "version": "6.4.4"})
                    if path.endswith("qualityprofile"):
                        return response(fake.arr_profiles)
                    if path.endswith("rootfolder"):
                        return response([])
                if path == "/api/v1/status":
                    synced = any(
                        profile.get("name") == "HD" for profile in fake.arr_profiles
                    )
                    return response(
                        {
                            "arr": [
                                {
                                    "id": 1,
                                    "sync": {
                                        "qualityProfiles": {
                                            "status": (
                                                "pending"
                                                if fake.sync_pending
                                                else "idle"
                                            ),
                                            "lastSyncedAt": (
                                                "2026-10-14T12:00:00Z"
                                                if synced
                                                else None
                                            ),
                                        }
                                    },
                                }
                            ]
                        }
                    )
                if path == "/api/v1/arr":
                    return response(
                        fake.rows if fake.version == 2 else {},
                        200 if fake.version == 2 else 404,
                    )
                if path == "/api/arr/config":
                    return response({"success": True, "data": fake.rows})
                if path == "/api/settings":
                    return response({"gitRepo": "https://example.invalid/database"})
                if path == "/api/data/profile":
                    return response(
                        [{"content": {"name": "HD"}}, {"content": {"name": "Personal"}}]
                    )
                if path == "/api/arr/config/1" and method == "PUT":
                    fake.rows[0] = copy.deepcopy(options["json"])
                    return response({"success": True})
                if path == "/api/import/profile":
                    if not fake.sync_pending:
                        fake.arr_profiles.append({"id": 2, "name": "HD"})
                    return response({"success": True})
                if path == "/arr/1/sync/__data.json":
                    data = {
                        "instance": fake.rows[0],
                        "databases": [
                            {
                                "id": 7,
                                "name": "Curated",
                                "qualityProfiles": [
                                    {"name": "HD"},
                                    {"name": "Personal"},
                                ],
                            }
                        ],
                        "syncData": {
                            "qualityProfiles": {
                                "selections": fake.selection,
                                "config": {"trigger": "schedule", "cron": "0 4 * * *"},
                            }
                        },
                    }
                    return response(
                        {
                            "type": "data",
                            "nodes": [{"type": "data", "data": flatten(data)}],
                        }
                    )
                if path.startswith("/arr/1/sync?/"):
                    if fake.fail_form:
                        return response(
                            {"type": "failure", "status": 400, "data": "[]"}
                        )
                    if path.endswith("saveQualityProfiles"):
                        fake.selection = json.loads(options["data"]["selections"])
                    elif path.endswith("syncQualityProfiles") and not fake.sync_pending:
                        fake.arr_profiles.append({"id": 2, "name": "HD"})
                    return response(
                        {
                            "type": "success",
                            "status": 200,
                            "data": json.dumps(flatten({"success": True})),
                        }
                    )
                if path == "/auth/login":
                    self.cookies["session"] = "private-session"
                    return response(
                        {"type": "redirect", "status": 303, "location": "/"}
                    )
                return response({}, 404)

        return Client()


class ProfilarrProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        xml = Path(self.temp.name) / "config.xml"
        xml.write_text("<Config><ApiKey>arr-key</ApiKey></Config>")
        self.config = {
            "profilarr": {"instances": {"Profiles": {"port": 6868}}},
            "radarr": {
                "instances": {
                    "Movies": {"enabled": True, "port": 7878, "config_file": str(xml)}
                }
            },
        }
        self.action = {
            "source": "profilarr.Profiles",
            "service_key": "profilarr",
            "action": "profiles",
        }

    def payload(self, version):
        return {
            "arrangement": {
                "values": {
                    "profilarr.Profiles::api_key": "fixture-key",
                    "profilarr.Profiles::arr_instances": ["radarr.Movies"],
                    "profilarr.Profiles::radarr.Movies::profiles": [
                        "7:HD" if version == 2 else "HD"
                    ],
                    "profilarr.Profiles::sync_now": True,
                }
            }
        }

    def apply(self, api, payload, operation="apply"):
        return profiles.apply_profiles(
            self.action,
            self.config,
            payload,
            operation,
            client_factory=api.factory,
            ownership_directory=self.temp.name,
        )

    def test_v1_and_v2_preview_apply_readback_preserve_other_policies(self):
        for version in (1, 2):
            with self.subTest(version=version):
                api, payload = FakeProfilarr(version), self.payload(version)
                fields = profiles.discover_fields(
                    self.action, self.config, payload, client_factory=api.factory
                )
                selected = next(
                    f for f in fields if f["key"].endswith("::radarr.Movies::profiles")
                )
                self.assertTrue(selected["options"])
                first = self.apply(api, payload)
                self.assertEqual(first["execution"], "waiting_input")
                proposal = self.apply(api, payload, "preview")
                self.assertNotIn("arr-key", json.dumps(proposal))
                values = payload["arrangement"]["values"]
                values["profilarr.Profiles::adopt_existing"] = True
                values["profilarr.Profiles::adoption_preview_id"] = proposal[
                    "evidence"
                ]["preview_id"]
                result = self.apply(api, payload)
                self.assertEqual(result["verification"], "passed", result)
                self.assertEqual(api.rows[0]["tags"], ["personal"])
                self.assertEqual(api.rows[0]["sync_method"], "schedule")
                self.assertEqual(api.rows[0]["data_to_sync"]["customFormats"], ["Keep"])
                if version == 2:
                    save = next(
                        c for c in api.calls if c[1].endswith("saveQualityProfiles")
                    )
                    self.assertEqual(save[2]["data"]["trigger"], "schedule")
                    self.assertEqual(save[2]["data"]["cron"], "0 4 * * *")
                sync_count = len(
                    [
                        c
                        for c in api.calls
                        if c[1].endswith("syncQualityProfiles")
                        or c[1] == "/api/import/profile"
                    ]
                )
                self.assertEqual(self.apply(api, payload)["verification"], "passed")
                self.assertEqual(
                    sync_count,
                    len(
                        [
                            c
                            for c in api.calls
                            if c[1].endswith("syncQualityProfiles")
                            or c[1] == "/api/import/profile"
                        ]
                    ),
                )

    def test_changed_native_profiles_invalidates_consent_without_write(self):
        api, payload = FakeProfilarr(2), self.payload(2)
        proposal = self.apply(api, payload, "preview")
        payload["arrangement"]["values"].update(
            {
                "profilarr.Profiles::adopt_existing": True,
                "profilarr.Profiles::adoption_preview_id": proposal["evidence"][
                    "preview_id"
                ],
            }
        )
        api.arr_profiles.append({"id": 9, "name": "Added outside"})
        api.calls.clear()
        self.assertEqual(self.apply(api, payload)["execution"], "waiting_input")
        self.assertFalse(any(method != "GET" for method, _, _ in api.calls))

    def test_async_sync_is_waiting_and_retry_does_not_queue_twice(self):
        api, payload = FakeProfilarr(2), self.payload(2)
        api.sync_pending = True
        proposal = self.apply(api, payload, "preview")
        payload["arrangement"]["values"].update(
            {
                "profilarr.Profiles::adopt_existing": True,
                "profilarr.Profiles::adoption_preview_id": proposal["evidence"][
                    "preview_id"
                ],
            }
        )
        result = self.apply(api, payload)
        self.assertEqual(result["execution"], "waiting_dependency", result)
        result = self.apply(api, payload, "retry")
        self.assertEqual(result["execution"], "waiting_dependency", result)
        self.assertEqual(
            len([c for c in api.calls if c[1].endswith("syncQualityProfiles")]), 1
        )

    def test_existing_profile_name_does_not_verify_a_pending_native_sync(self):
        api, payload = FakeProfilarr(2), self.payload(2)
        api.arr_profiles.append({"id": 9, "name": "HD"})
        api.sync_pending = True
        proposal = self.apply(api, payload, "preview")
        payload["arrangement"]["values"].update(
            {
                "profilarr.Profiles::adopt_existing": True,
                "profilarr.Profiles::adoption_preview_id": proposal["evidence"][
                    "preview_id"
                ],
            }
        )
        result = self.apply(api, payload)
        self.assertEqual(result["execution"], "waiting_dependency", result)
        self.assertNotEqual(result["verification"], "passed")

    def test_native_form_error_never_reports_success(self):
        api, payload = FakeProfilarr(2), self.payload(2)
        proposal = self.apply(api, payload, "preview")
        payload["arrangement"]["values"].update(
            {
                "profilarr.Profiles::adopt_existing": True,
                "profilarr.Profiles::adoption_preview_id": proposal["evidence"][
                    "preview_id"
                ],
            }
        )
        api.fail_form = True
        result = self.apply(api, payload)
        self.assertEqual(result["execution"], "waiting_input")
        self.assertNotEqual(result["verification"], "passed")
