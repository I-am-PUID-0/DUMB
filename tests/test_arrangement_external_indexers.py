import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from utils.arrangement_http import ApiResponse
from utils import arrangement_external as external
from utils import arrangement_indexers as indexers
from utils import arrangement_profiles as profiles
from utils import arrangement_adoption as adoption


def response(value, status=200):
    return ApiResponse(status, json.dumps(value).encode())


class ExternalTests(unittest.TestCase):
    def payload(self, **kwargs):
        return {
            "arrangement": {
                "values": {
                    "setup::external_arrs": [
                        {
                            "id": "films",
                            "label": "Existing movies",
                            "service_key": "radarr",
                            "url": "https://example.invalid/radarr",
                            "api_key": "private-key",
                            **kwargs,
                        }
                    ]
                }
            }
        }

    def test_public_resources_have_no_credentials_or_endpoint_and_unique_ids(self):
        payload = self.payload()
        self.assertEqual(
            external.external_resources(payload)[0]["id"], "external_arr.films"
        )
        serialized = json.dumps(external.external_resources(payload))
        self.assertNotIn("private-key", serialized)
        self.assertNotIn("example.invalid", serialized)
        payload["arrangement"]["values"]["setup::external_arrs"] *= 2
        with self.assertRaises(ValueError):
            external.external_resources(payload)

    def test_readiness_and_external_write_consent_are_independent(self):
        calls = []

        class Client:
            def request(self, method, path, **kwargs):
                calls.append((method, path))
                return response({"appName": "Radarr", "version": "6.2.0"})

        factory = lambda *args, **kwargs: Client()
        action = {"source": "external_arr.films", "action": "external_ready"}
        result = external.apply_external(
            action, {}, self.payload(), client_factory=factory
        )
        self.assertEqual(result["verification"], "passed", result)
        self.assertEqual(calls, [("GET", "/api/v3/system/status")])
        with self.assertRaises(ValueError):
            external.resolve_target(
                self.payload(),
                "external_arr.films",
                client_factory=factory,
                require_write=True,
            )

    def test_application_mismatch_fails_closed(self):
        class Client:
            def request(self, *args, **kwargs):
                return response({"appName": "Sonarr", "version": "4.0.0"})

        result = external.inspect_external(
            {"source": "external_arr.films", "action": "external_ready"},
            {},
            self.payload(),
            client_factory=lambda *a, **k: Client(),
        )
        self.assertEqual(result["execution"], "waiting_input")


class AdoptionTests(unittest.TestCase):
    def test_opaque_preview_is_redacted_and_invalidated_by_native_or_desired_changes(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview.json"
            before, after = {"apiKey": "old", "host": "old"}, {
                "apiKey": "new",
                "host": "new",
            }
            first = adoption.preview(path, "source::target", before, after)
            self.assertNotIn(
                "old", json.dumps([c for c in first["changes"] if c["secret"]])
            )
            self.assertNotIn("new", first["preview_id"])
            accepted = adoption.preview(
                path,
                "source::target",
                before,
                after,
                operation="apply",
                consent=True,
                accepted_id=first["preview_id"],
            )
            self.assertTrue(accepted["approved"])
            stale = adoption.preview(
                path,
                "source::target",
                {**before, "host": "drift"},
                after,
                operation="apply",
                consent=True,
                accepted_id=first["preview_id"],
            )
            self.assertFalse(stale["approved"])
            stale = adoption.preview(
                path,
                "other::target",
                before,
                after,
                operation="apply",
                consent=True,
                accepted_id=first["preview_id"],
            )
            self.assertFalse(stale["approved"])


class FakeIndexers:
    def __init__(self):
        self.template = {
            "implementation": "Newznab",
            "configContract": "NewznabSettings",
            "definitionName": "Generic",
            "name": "Generic",
            "appProfileId": 1,
            "protocol": "usenet",
            "redirect": True,
            "tags": [],
            "fields": [
                {
                    "name": "baseUrl",
                    "type": "textbox",
                    "value": "https://example.invalid",
                },
                {
                    "name": "apiKey",
                    "type": "password",
                    "privacy": "apiKey",
                    "value": "",
                },
                {
                    "name": "categories",
                    "type": "select",
                    "selectOptions": [{"value": 2000, "name": "Movies"}],
                    "value": 2000,
                },
            ],
        }
        self.rows, self.calls = [], []
        self.fail_test = False
        self.mask_credentials = False

    def factory(self, host, **kwargs):
        fake = self

        class Client:
            def request(self, method, path, **options):
                fake.calls.append((method, path, copy.deepcopy(options)))
                if path.endswith("/schema"):
                    return response([fake.template])
                if path.endswith("/appprofile"):
                    return response([{"id": 1, "name": "Default"}])
                if path.endswith("/test"):
                    if any(
                        row.get("name") == (options.get("json") or {}).get("name")
                        and row.get("id") != (options.get("json") or {}).get("id")
                        for row in fake.rows
                    ):
                        return response([{"propertyName": "Name"}], 400)
                    return response(
                        [{"errorMessage": "fixture-secret"}] if fake.fail_test else []
                    )
                if method == "GET":
                    rows = copy.deepcopy(fake.rows)
                    if fake.mask_credentials:
                        for row in rows:
                            for field in row.get("fields") or []:
                                if field.get("name") == "apiKey":
                                    field["value"] = "********"
                    return response(rows)
                row = copy.deepcopy(options["json"])
                if method == "POST":
                    row["id"] = len(fake.rows) + 1
                    fake.rows.append(row)
                else:
                    row["id"] = int(path.rsplit("/", 1)[1])
                    fake.rows = [
                        row if old["id"] == row["id"] else old for old in fake.rows
                    ]
                return response(row)

        return Client()


class IndexerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = FakeIndexers()
        self.action = {
            "source": "prowlarr.Search",
            "service_key": "prowlarr",
            "action": "indexers",
        }
        xml = Path(self.temp.name) / "config.xml"
        xml.write_text("<Config><ApiKey>source-key</ApiKey></Config>")
        self.config = {
            "prowlarr": {
                "instances": {"Search": {"port": 9696, "config_file": str(xml)}}
            }
        }
        self.payload = {
            "arrangement": {
                "values": {
                    "prowlarr.Search::indexers": [
                        {
                            "id": "news",
                            "name": "My News",
                            "definition": indexers._definition(self.api.template),
                            "enable": False,
                            "app_profile": "1",
                            "authorize_changes": True,
                        }
                    ],
                    "prowlarr.Search::indexer::news::apiKey": "private-fixture",
                    "prowlarr.Search::indexer::news::categories": "2000",
                }
            }
        }

    def apply(self, operation="apply"):
        with patch.object(indexers, "validate_endpoint"):
            return indexers.apply_indexers(
                self.action,
                self.config,
                self.payload,
                operation,
                client_factory=self.api.factory,
                ownership_directory=self.temp.name,
            )

    def test_dynamic_schema_marks_keys_private_and_normalizes_browser_numbers(self):
        fields = indexers.discover_fields(
            self.action, self.config, self.payload, client_factory=self.api.factory
        )
        key = next(f for f in fields if f["key"].endswith("::apiKey"))
        self.assertTrue(key["secret"])
        self.assertNotIn("default", key)
        result = self.apply()
        self.assertEqual(result["verification"], "passed", result)
        self.assertEqual(indexers._fields(self.api.rows[0])["categories"], 2000)
        self.assertFalse(self.api.rows[0]["enable"])
        self.assertEqual(self.apply()["execution"], "already_present")
        self.assertEqual(len(self.api.rows), 1)

    def test_preview_adoption_is_explicit_and_preserves_tags_and_unrelated_indexers(
        self,
    ):
        self.api.rows = [
            {
                **copy.deepcopy(self.api.template),
                "id": 4,
                "name": "My News",
                "tags": [9],
                "enable": False,
                "priority": 25,
            },
            {"id": 8, "name": "Personal"},
        ]
        blocked = self.apply()
        self.assertEqual(blocked["execution"], "waiting_input")
        proposal = self.apply("preview")
        self.assertIsNotNone(proposal["evidence"]["preview_id"])
        self.assertNotIn("private-fixture", json.dumps(proposal))
        values = self.payload["arrangement"]["values"]
        values["prowlarr.Search::adopt_existing"] = True
        values["prowlarr.Search::adoption_preview_id"] = proposal["evidence"][
            "preview_id"
        ]
        self.assertEqual(self.apply()["verification"], "passed")
        self.assertEqual(self.api.rows[0]["tags"], [9])
        self.assertEqual(self.api.rows[1], {"id": 8, "name": "Personal"})

    def test_one_opaque_batch_preview_accepts_two_exact_existing_indexer_changes(self):
        first = {
            **copy.deepcopy(self.api.template),
            "id": 4,
            "name": "My News",
            "tags": [9],
            "enable": False,
            "priority": 25,
        }
        second = {**copy.deepcopy(first), "id": 5, "name": "Other News"}
        self.api.rows = [first, second]
        values = self.payload["arrangement"]["values"]
        values["prowlarr.Search::indexers"].append(
            {
                **values["prowlarr.Search::indexers"][0],
                "id": "other",
                "name": "Other News",
            }
        )
        values["prowlarr.Search::indexer::other::apiKey"] = "other-private-key"
        proposal = self.apply("preview")
        self.assertIsNotNone(proposal["evidence"]["preview_id"])
        self.assertTrue(
            any(c["field"].startswith("news.") for c in proposal["evidence"]["changes"])
        )
        self.assertTrue(
            any(
                c["field"].startswith("other.") for c in proposal["evidence"]["changes"]
            )
        )
        values["prowlarr.Search::adopt_existing"] = True
        values["prowlarr.Search::adoption_preview_id"] = proposal["evidence"][
            "preview_id"
        ]
        self.assertEqual(self.apply()["verification"], "passed")
        self.assertEqual(len(self.api.rows), 2)
        self.assertEqual(self.apply()["execution"], "already_present")

    def test_native_indexer_id_collision_after_endpoint_change_requires_new_adoption(
        self,
    ):
        self.assertEqual(self.apply()["verification"], "passed")
        self.config["prowlarr"]["instances"]["Search"]["port"] = 9797
        self.payload["arrangement"]["values"][
            "prowlarr.Search::indexer::news::apiKey"
        ] = "different-private-key"
        before = copy.deepcopy(self.api.rows)
        self.assertEqual(self.apply()["execution"], "waiting_input")
        self.assertEqual(self.api.rows, before)

    def test_failed_native_test_never_saves_or_leaks_findings(self):
        self.api.fail_test = True
        result = self.apply()
        self.assertEqual(result["execution"], "waiting_input")
        self.assertEqual(self.api.rows, [])
        self.assertNotIn("fixture-secret", json.dumps(result))

    def test_inspect_and_preview_never_native_write(self):
        self.apply("inspect")
        self.apply("preview")
        self.assertFalse(any(method != "GET" for method, _, _ in self.api.calls))


class SvelteDataTests(unittest.TestCase):
    def test_decoder_preserves_integer_values_and_rejects_cycles(self):
        self.assertEqual(
            profiles._unflatten([{"value": 1, "names": 2}, 42, [3], "HD"]),
            {"value": 42, "names": ["HD"]},
        )
        with self.assertRaises(profiles.UnsupportedContract):
            profiles._unflatten([{"loop": 0}])
