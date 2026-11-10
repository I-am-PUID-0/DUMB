import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from utils import arrangement_arr_providers as providers
from utils.arrangement_http import ApiResponse


class ExternalProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.action = {
            "source": "external_arr.movies",
            "service_key": "radarr",
            "action": "provider.altmount",
        }
        self.payload = {
            "arrangement": {
                "values": {
                    "setup::external_arrs": [
                        {
                            "id": "movies",
                            "service_key": "radarr",
                            "url": "https://arr.example.invalid",
                            "api_key": "arr-private",
                            "authorize_changes": True,
                        }
                    ],
                    "external_arr.movies::providers": ["altmount"],
                    "external_arr.movies::new_root_folder": "/library/movies",
                    "external_arr.movies::altmount::client_url": "https://provider.example.invalid/altmount",
                    "external_arr.movies::altmount::remote_path": "/provider/completed",
                    "external_arr.movies::altmount::local_path": "/mounted/completed",
                }
            }
        }
        self.config = {"altmount": {"enabled": True}}
        self.schema = {
            "name": "Sabnzbd",
            "implementation": "Sabnzbd",
            "configContract": "SabnzbdSettings",
            "fields": [
                {"name": key, "value": value}
                for key, value in (
                    ("host", ""),
                    ("port", 8080),
                    ("urlBase", ""),
                    ("useSsl", False),
                    ("apiKey", ""),
                    ("category", ""),
                )
            ],
        }
        self.schemas = [self.schema]
        self.dual = False
        self.fail_implementation = None
        self.clients, self.roots, self.mappings, self.calls, self.provider_calls = (
            [],
            [],
            [],
            [],
            [],
        )
        self.fail_test = False
        self.provider_adoption = None
        self.provider_drift = False

    def factory(self, host, **kwargs):
        fake = self

        class Client:
            def request(self, method, path, **options):
                fake.calls.append((method, path, copy.deepcopy(options)))
                if path.endswith("/system/status"):
                    value = {"appName": "Radarr", "version": "6.4.4"}
                elif path.endswith("/downloadclient/schema"):
                    value = fake.schemas
                elif path.endswith("/downloadclient/test"):
                    failed = (
                        fake.fail_test
                        or options.get("json", {}).get("implementation")
                        == fake.fail_implementation
                    )
                    return ApiResponse(
                        400 if failed else 200,
                        json.dumps(
                            [{"errorMessage": "private-fixture"}] if failed else "{}"
                        ).encode(),
                    )
                else:
                    kind = (
                        "downloadclient"
                        if "/downloadclient" in path
                        else (
                            "rootfolder"
                            if "/rootfolder" in path
                            else "remotepathmapping"
                        )
                    )
                    rows = (
                        fake.clients
                        if kind == "downloadclient"
                        else fake.roots if kind == "rootfolder" else fake.mappings
                    )
                    if method == "GET":
                        value = copy.deepcopy(rows)
                        if kind == "downloadclient":
                            for row in value:
                                for field in row.get("fields") or []:
                                    if field["name"] == "apiKey":
                                        field["value"] = "********"
                    else:
                        row = copy.deepcopy(options["json"])
                        if method == "POST":
                            row["id"] = len(rows) + 1
                            if kind == "rootfolder":
                                row["accessible"] = True
                            rows.append(row)
                        else:
                            row["id"] = int(path.rsplit("/", 1)[1])
                            rows[:] = [
                                row if old["id"] == row["id"] else old for old in rows
                            ]
                        value = row
                return ApiResponse(200, json.dumps(value).encode())

        return Client()

    def provider(
        self,
        config,
        payload,
        provider,
        target,
        category,
        operation,
        factory,
        directory,
        **kwargs,
    ):
        self.provider_calls.append((operation, target["id"], category))
        if operation == "apply" and self.provider_adoption:
            accepted = kwargs.get("approved_provider_preview")
            if accepted != self.provider_adoption["fingerprint"] or self.provider_drift:
                return {
                    "execution": "waiting_input",
                    "verification": "not_checked",
                    "retryable": False,
                    "reason": "Fresh provider registration consent is required.",
                    "fields": {"apiKey": "private-native-key"},
                    "_provider_adoption": self.provider_adoption,
                }
        if self.dual:
            return {
                "implementation": "QBittorrent",
                "fields": {
                    "username": target["url"],
                    "password": target["key"],
                    "urlBase": "/dec",
                },
                "additional_clients": [
                    {
                        "implementation": "Sabnzbd",
                        "fields": {
                            "apiKey": "native-usenet-key",
                            "urlBase": "/dec/sabnzbd",
                        },
                        "name_suffix": "usenet",
                    }
                ],
                **(
                    {"_provider_adoption": self.provider_adoption}
                    if self.provider_adoption
                    else {}
                ),
            }
        return {
            "implementation": "Sabnzbd",
            "fields": {"apiKey": "native-provider-key"},
            "evidence": {"category_ready": True},
            **(
                {"_provider_adoption": self.provider_adoption}
                if self.provider_adoption
                else {}
            ),
        }

    def apply(self, operation="apply", action=None):
        with patch.object(providers, "validate_endpoint"):
            return providers.apply_arr_provider(
                action or self.action,
                self.config,
                self.payload,
                operation,
                client_factory=self.factory,
                ownership_directory=self.temp.name,
                provider_adapter=self.provider,
            )

    def test_fresh_external_provider_root_and_mapping_are_real_native_upserts_and_idempotent(
        self,
    ):
        proposal = self.apply("preview")
        self.assertTrue(proposal["evidence"]["changes"])
        self.assertEqual(self.clients, [])
        self.assertTrue(all(c[0] == "inspect" for c in self.provider_calls))
        result = self.apply()
        self.assertEqual(result["verification"], "passed", result)
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.roots[0]["path"], "/library/movies")
        self.assertEqual(self.mappings[0]["localPath"], "/mounted/completed")
        self.assertFalse(self.clients[0]["removeCompletedDownloads"])
        self.assertFalse(self.clients[0]["removeFailedDownloads"])
        writes = len(
            [c for c in self.calls if c[0] != "GET" and not c[1].endswith("/test")]
        )
        self.assertEqual(self.apply()["execution"], "already_present")
        self.assertEqual(
            writes,
            len(
                [c for c in self.calls if c[0] != "GET" and not c[1].endswith("/test")]
            ),
        )
        self.assertTrue(any(c[0] == "apply" for c in self.provider_calls))

    def test_external_write_consent_is_required_before_provider_or_arr_mutations(self):
        self.payload["arrangement"]["values"]["setup::external_arrs"][0][
            "authorize_changes"
        ] = False
        self.assertEqual(self.apply()["execution"], "waiting_input")
        self.assertEqual(self.clients, [])
        self.assertFalse(any(c[0] == "apply" for c in self.provider_calls))

    def test_existing_matching_client_adoption_requires_exact_preview_and_preserves_other_rows(
        self,
    ):
        self.clients = [
            {
                **copy.deepcopy(self.schema),
                "id": 8,
                "name": "DUMB altmount movies",
                "enable": True,
                "tags": [9],
                "removeCompletedDownloads": True,
                "removeFailedDownloads": False,
            },
            {"id": 10, "name": "personal", "fields": []},
        ]
        self.assertEqual(self.apply()["execution"], "waiting_input")
        proposal = self.apply("preview")
        values = self.payload["arrangement"]["values"]
        values["external_arr.movies::adopt_existing"] = True
        values["external_arr.movies::adoption_preview_id"] = proposal["evidence"][
            "preview_id"
        ]
        result = self.apply()
        self.assertEqual(result["verification"], "passed", result)
        self.assertEqual(self.clients[0]["tags"], [9])
        self.assertTrue(self.clients[0]["removeCompletedDownloads"])
        self.assertEqual(self.clients[1]["name"], "personal")

    def test_explicit_opt_out_retires_only_owned_client_preserving_root_and_mapping(
        self,
    ):
        self.assertEqual(self.apply()["verification"], "passed")
        self.payload["arrangement"]["values"]["external_arr.movies::providers"] = []
        action = {**self.action, "retirement_pending": True}
        preview = self.apply("recheck", action)
        self.assertEqual(preview["execution"], "waiting_input")
        result = self.apply("apply", action)
        self.assertTrue(result["evidence"]["retired"], result)
        self.assertFalse(self.clients[0]["enable"])
        self.assertEqual(len(self.roots), 1)
        self.assertEqual(len(self.mappings), 1)

    def test_external_endpoint_reuse_cannot_inherit_owned_client_identity(self):
        self.assertEqual(self.apply()["verification"], "passed")
        self.payload["arrangement"]["values"]["setup::external_arrs"][0][
            "url"
        ] = "https://different-arr.example.invalid"
        before = copy.deepcopy(self.clients)
        self.assertEqual(self.apply()["execution"], "waiting_input")
        self.assertEqual(self.clients, before)
        self.payload["arrangement"]["values"]["external_arr.movies::providers"] = []
        result = self.apply("apply", {**self.action, "retirement_pending": True})
        self.assertEqual(result["execution"], "waiting_input")
        self.assertTrue(self.clients[0]["enable"])

    def test_native_registration_adoption_is_bound_to_redacted_exact_preview(self):
        self.provider_adoption = {
            "before": {
                "registration.url": "https://old.example.invalid",
                "registration.api_key": "old-private",
            },
            "after": {
                "registration.url": "https://arr.example.invalid",
                "registration.api_key": "new-private",
            },
            "fingerprint": "private-native-scope",
            "requires_consent": True,
        }
        refused = self.apply()
        self.assertEqual(refused["execution"], "waiting_input", refused)
        self.assertEqual(self.clients, [])
        proposal = self.apply("preview")
        self.assertTrue(proposal["evidence"]["requires_consent"])
        self.assertNotIn("old-private", json.dumps(proposal))
        self.assertNotIn("new-private", json.dumps(proposal))
        values = self.payload["arrangement"]["values"]
        values["external_arr.movies::adopt_existing"] = True
        values["external_arr.movies::adoption_preview_id"] = proposal["evidence"][
            "preview_id"
        ]
        self.provider_drift = True
        refused = self.apply()
        self.assertEqual(refused["execution"], "waiting_input", refused)
        self.assertNotIn("private-native-key", json.dumps(refused))
        self.assertNotIn("_provider_adoption", refused)
        self.assertEqual(self.clients, [])
        self.provider_drift = False
        self.assertEqual(self.apply()["verification"], "passed")

    def dual_setup(self):
        self.dual = True
        self.action["action"] = "provider.decypharr"
        self.config["decypharr"] = {"enabled": True}
        values = self.payload["arrangement"]["values"]
        values["external_arr.movies::providers"] = ["decypharr"]
        for suffix in ("client_url", "remote_path", "local_path"):
            values["external_arr.movies::decypharr::" + suffix] = values[
                "external_arr.movies::altmount::" + suffix
            ]
        values["external_arr.movies::decypharr::client_url"] = (
            "https://provider.example.invalid/dec"
        )
        qbit = copy.deepcopy(self.schema)
        qbit.update(implementation="QBittorrent", configContract="QBittorrentSettings")
        qbit["fields"] = [
            field for field in qbit["fields"] if field["name"] != "apiKey"
        ]
        qbit["fields"].extend(
            [{"name": "username", "value": ""}, {"name": "password", "value": ""}]
        )
        self.schemas.append(qbit)

    def test_decypharr_native_usenet_creates_separate_clients_with_one_preview_and_retries_no_duplicate(
        self,
    ):
        self.dual_setup()
        self.fail_implementation = "Sabnzbd"
        proposal = self.apply("preview")
        self.assertTrue(
            any(
                row["field"].startswith("torrent.")
                for row in proposal["evidence"]["changes"]
            )
        )
        self.assertTrue(
            any(
                row["field"].startswith("usenet.")
                for row in proposal["evidence"]["changes"]
            )
        )
        self.assertEqual(self.clients, [])
        failed = self.apply()
        self.assertNotEqual(failed["verification"], "passed")
        self.assertEqual(
            [row["implementation"] for row in self.clients], ["QBittorrent"]
        )
        self.fail_implementation = None
        result = self.apply("retry")
        self.assertEqual(result["verification"], "passed", result)
        self.assertEqual(
            {row["implementation"] for row in self.clients}, {"QBittorrent", "Sabnzbd"}
        )
        self.assertEqual(len({row["id"] for row in self.clients}), 2)
        self.assertEqual(
            {
                row["implementation"]: providers._fields(row)["urlBase"]
                for row in self.clients
            },
            {"QBittorrent": "/dec", "Sabnzbd": "/dec/sabnzbd"},
        )
        self.assertEqual(
            len(
                [
                    call
                    for call in self.calls
                    if call[0] == "POST" and call[1].endswith("/downloadclient")
                ]
            ),
            2,
        )
        self.assertEqual(self.apply()["execution"], "already_present")
        self.payload["arrangement"]["values"]["external_arr.movies::providers"] = []
        retired = self.apply(action={**self.action, "retirement_pending": True})
        self.assertTrue(retired["evidence"]["retired"], retired)
        self.assertTrue(all(not row["enable"] for row in self.clients))
        self.assertEqual(len(self.roots), 1)
        self.assertEqual(len(self.mappings), 1)

    def test_decypharr_existing_both_clients_use_one_exact_combined_adoption(self):
        self.dual_setup()
        for number, template in enumerate(self.schemas, 1):
            self.clients.append(
                {
                    **copy.deepcopy(template),
                    "id": number,
                    "name": "DUMB decypharr movies"
                    + (" usenet" if template["implementation"] == "Sabnzbd" else ""),
                    "enable": True,
                    "tags": [9],
                    "removeCompletedDownloads": True,
                }
            )
        proposal = self.apply("preview")
        self.assertTrue(proposal["evidence"]["requires_consent"])
        values = self.payload["arrangement"]["values"]
        values["external_arr.movies::adopt_existing"] = True
        values["external_arr.movies::adoption_preview_id"] = proposal["evidence"][
            "preview_id"
        ]
        result = self.apply()
        self.assertEqual(result["verification"], "passed", result)
        self.assertEqual(len(self.clients), 2)
        self.assertTrue(
            all(
                row["tags"] == [9] and row["removeCompletedDownloads"]
                for row in self.clients
            )
        )
        self.assertEqual(self.apply()["execution"], "already_present")

    def test_failed_native_test_never_saves_external_client_or_root(self):
        self.fail_test = True
        result = self.apply()
        self.assertNotEqual(result["verification"], "passed")
        self.assertNotIn("private-fixture", json.dumps(result))
        self.assertEqual(self.clients, [])
        self.assertEqual(self.roots, [])
