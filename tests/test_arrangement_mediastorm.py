import copy
import json
from pathlib import Path
import tempfile
import unittest

from utils import arrangement_mediastorm as adapter
from utils.arrangement_http import ApiError, ApiResponse
from utils.arrangement_inputs import resolve_provider_inputs
from utils.arrangement_providers import validate_connection_budgets


class Native:
    def __init__(self):
        self.version = "1.5.0"
        self.defaults = True
        self.master = True
        self.test_ok = True
        self.after_test = None
        self.calls = []
        self.native = {
            "server": {"port": 7777},
            "metadata": {"tmdbApiKey": "", "tvdbApiKey": ""},
            "streaming": {"debridProviders": []},
            "usenet": [],
            "usenetEngines": [],
            "indexers": [],
            "torrentScrapers": [],
            "scheduledTasks": {"enabled": False},
            "ui": {"existingPreference": "keep"},
        }
        self.indexers = [
            {
                "id": 3,
                "name": "Existing Usenet",
                "protocol": "usenet",
                "enable": True,
                "supportsSearch": True,
            },
            {
                "id": 7,
                "name": "Disabled",
                "protocol": "usenet",
                "enable": False,
                "supportsSearch": True,
            },
        ]

    def factory(self, url, **options):
        native = self

        class Client:
            def __init__(self):
                self.headers = dict(options.get("headers") or {})
                self.cookies = {}

            def request(self, method, path, **kwargs):
                native.calls.append((method, path, copy.deepcopy(kwargs)))
                if path == "/api/version":
                    data = {"version": native.version, "buildId": "20261013"}
                elif path == "/api/auth/default-password":
                    data = {"hasDefaultPassword": native.defaults}
                elif path == "/api/auth/login":
                    body = kwargs["json"]
                    if native.defaults and not body.get("newPassword"):
                        raise ApiError(status_code=428)
                    native.defaults = False
                    data = {"isMaster": native.master, "token": "a" * 43 + "="}
                elif path == "/api/auth/me":
                    if "Authorization" not in self.headers:
                        raise ApiError(status_code=401)
                    data = {"isMaster": native.master}
                elif path == "/api/settings":
                    if "Authorization" not in self.headers:
                        raise ApiError(status_code=401)
                    if method == "PUT":
                        native.native = copy.deepcopy(kwargs["json"])
                    data = copy.deepcopy(native.native)

                    def mask(value):
                        if isinstance(value, dict):
                            return {
                                k: (
                                    adapter.REDACTED
                                    if adapter.re.search(
                                        r"password|apiKey|token", k, adapter.re.I
                                    )
                                    and v
                                    else mask(v)
                                )
                                for k, v in value.items()
                            }
                        if isinstance(value, list):
                            return [mask(v) for v in value]
                        return value

                    data = mask(data)
                elif path.startswith("/admin/api/test/"):
                    if not self.cookies.get("strmr_admin_session"):
                        raise ApiError(status_code=401)
                    data = {"success": native.test_ok}
                    if native.after_test:
                        native.after_test()
                elif path == "/api/v1/indexer":
                    data = copy.deepcopy(native.indexers)
                else:
                    raise AssertionError((method, path))
                return ApiResponse(200, json.dumps(data).encode())

        return Client()


class MediaStormTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.native = Native()
        self.config = {
            "mediastorm": {"enabled": True, "port": 7777, "config_dir": self.temp.name},
            "zilean": {"enabled": True, "port": 8182},
            "prowlarr": {
                "instances": {
                    "Default": {
                        "enabled": True,
                        "port": 9696,
                        "api_key": "lab-prowlarr",
                    }
                }
            },
        }
        self.values = {
            "mediastorm::settings_session": "a" * 43 + "=",
            "mediastorm::authorize_source_tests": True,
        }
        self.account = {
            "service_key": "mediastorm",
            "source": "mediastorm",
            "action": "account",
        }
        self.sources = {**self.account, "action": "sources"}
        self.metadata = {**self.account, "action": "metadata"}

    def invoke(self, action=None, operation="apply"):
        return adapter.apply_mediastorm(
            action or self.sources,
            self.config,
            {"arrangement": {"values": self.values}},
            operation,
            client_factory=self.native.factory,
            nntp_authenticator=lambda *_args, **_kwargs: None,
        )

    def writes(self):
        return [
            (method, path)
            for method, path, _ in self.native.calls
            if method in {"POST", "PUT"}
        ]

    def test_first_login_requires_explicit_replacement_and_clears_only_temporary_credentials(
        self,
    ):
        self.values = {
            "mediastorm::admin_username": "admin",
            "mediastorm::admin_password": "admin",
            "mediastorm::new_admin_password": "Replacement-password-123",
        }
        result = self.invoke(self.account)
        self.assertEqual(result["execution"], "waiting_input")
        self.assertEqual(self.writes(), [])
        self.values["mediastorm::authorize_initial_account"] = True
        result = self.invoke(self.account)
        self.assertEqual(result["verification"], "passed")
        self.assertEqual(
            set(result["_clear_inputs"]),
            {"mediastorm::admin_password", "mediastorm::new_admin_password"},
        )
        self.assertIn("mediastorm::settings_session", result["_private_values"])
        self.assertFalse(self.native.defaults)

    def test_existing_account_password_is_never_replaced(self):
        self.native.defaults = False
        self.values = {
            "mediastorm::admin_username": "admin",
            "mediastorm::admin_password": "current-password",
            "mediastorm::new_admin_password": "Replacement-password-123",
            "mediastorm::authorize_initial_account": True,
        }
        self.assertEqual(self.invoke(self.account)["execution"], "waiting_input")
        self.assertEqual(self.writes(), [])

    def test_preview_never_creates_a_session(self):
        self.values = {
            "mediastorm::admin_username": "admin",
            "mediastorm::admin_password": "admin",
        }
        self.assertEqual(
            self.invoke(self.account, "preview")["execution"], "waiting_input"
        )
        self.assertEqual(self.writes(), [])

    def test_non_master_session_cannot_write_settings(self):
        self.native.master = False
        self.values["mediastorm::torrent_sources"] = ["zilean"]
        self.assertEqual(self.invoke()["execution"], "waiting_input")
        self.assertEqual(self.writes(), [])

    def test_unknown_version_preserves_settings_and_reports_unknown_contract(self):
        self.native.version = "1.6.0"
        self.values["mediastorm::torrent_sources"] = ["zilean"]
        result = self.invoke()
        self.assertEqual(result["automation"], "unknown")
        self.assertEqual(result["verification"], "unavailable")
        self.assertEqual(self.writes(), [])

    def test_source_preview_is_read_only_and_masks_keys(self):
        self.values["mediastorm::indexer_sources"] = ["prowlarr.Default"]
        result = self.invoke(operation="preview")
        self.assertTrue(result["evidence"]["preview_id"])
        self.assertNotIn("lab-prowlarr", json.dumps(result))
        self.assertEqual(self.writes(), [])
        self.assertEqual(
            len(
                [
                    change
                    for change in result["evidence"]["changes"]
                    if change["field"].endswith(".name")
                ]
            ),
            1,
        )

    def test_selected_source_creation_preserves_unrelated_settings_and_repeat_is_idempotent(
        self,
    ):
        self.values["mediastorm::torrent_sources"] = ["zilean"]
        result = self.invoke()
        self.assertEqual(result["verification"], "passed")
        self.assertFalse(self.native.native["scheduledTasks"]["enabled"])
        self.assertEqual(self.native.native["ui"]["existingPreference"], "keep")
        self.assertEqual(len(self.native.native["torrentScrapers"]), 1)
        self.native.calls.clear()
        result = self.invoke()
        self.assertEqual(result["execution"], "already_present")
        self.assertNotIn(("PUT", "/api/settings"), self.writes())
        self.assertEqual(len(self.native.native["torrentScrapers"]), 1)

    def test_unowned_source_needs_exact_preview_approval_and_native_drift_invalidates_it(
        self,
    ):
        self.native.native["torrentScrapers"] = [
            {
                "name": "Existing source",
                "type": "zilean",
                "url": "http://127.0.0.1:8182",
                "enabled": False,
                "options": "keep",
            }
        ]
        self.values["mediastorm::torrent_sources"] = ["zilean"]
        self.assertEqual(self.invoke()["execution"], "waiting_input")
        preview = self.invoke(operation="preview")
        self.assertTrue(preview["evidence"]["requires_consent"])
        self.values.update(
            {
                "mediastorm::adopt_existing": True,
                "mediastorm::adoption_preview_id": preview["evidence"]["preview_id"],
            }
        )
        self.native.native["torrentScrapers"][0]["options"] = "changed-after-preview"
        self.assertEqual(self.invoke()["execution"], "waiting_input")
        self.assertNotIn(("PUT", "/api/settings"), self.writes())

    def test_explicit_unowned_adoption_preserves_name_order_and_unrelated_fields(self):
        self.native.native["torrentScrapers"] = [
            {
                "name": "Existing",
                "type": "zilean",
                "url": "http://127.0.0.1:8182",
                "enabled": False,
                "options": "keep",
            },
            {
                "name": "Other",
                "type": "other",
                "url": "https://example.test",
                "enabled": False,
            },
        ]
        self.values["mediastorm::torrent_sources"] = ["zilean"]
        preview = self.invoke(operation="preview")
        self.values.update(
            {
                "mediastorm::adopt_existing": True,
                "mediastorm::adoption_preview_id": preview["evidence"]["preview_id"],
            }
        )
        result = self.invoke()
        self.assertEqual(result["execution"], "applied")
        self.assertEqual(
            [row["name"] for row in self.native.native["torrentScrapers"]],
            ["Existing", "Other"],
        )
        self.assertEqual(self.native.native["torrentScrapers"][0]["options"], "keep")

    def test_recheck_does_not_apply_pending_configuration(self):
        self.values["mediastorm::torrent_sources"] = ["zilean"]
        self.assertEqual(self.invoke(operation="recheck")["execution"], "waiting_input")
        self.assertEqual(self.writes(), [])

    def test_native_test_requires_deliberate_authorization_and_rejection_preserves_config(
        self,
    ):
        self.values["mediastorm::torrent_sources"] = ["zilean"]
        self.values["mediastorm::authorize_source_tests"] = False
        self.assertEqual(self.invoke()["execution"], "waiting_input")
        self.assertEqual(self.writes(), [])
        self.values["mediastorm::authorize_source_tests"] = True
        self.native.test_ok = False
        self.assertEqual(self.invoke()["execution"], "waiting_input")
        self.assertNotIn(("PUT", "/api/settings"), self.writes())

    def test_metadata_test_and_readback_preserve_source_configuration(self):
        self.values["mediastorm::tmdb_api_key"] = "lab-metadata-key"
        result = self.invoke(self.metadata)
        self.assertEqual(result["verification"], "passed")
        self.assertEqual(
            self.native.native["metadata"]["tmdbApiKey"], "lab-metadata-key"
        )
        self.assertEqual(self.native.native["indexers"], [])
        self.assertEqual(result["evidence"]["workflow_tested"], False)

    def test_metadata_recheck_reauthenticates_retained_key_without_rewriting_it(self):
        self.values["mediastorm::tmdb_api_key"] = "lab-metadata-key"
        self.assertEqual(self.invoke(self.metadata)["verification"], "passed")
        self.native.calls.clear()
        self.native.test_ok = False
        result = self.invoke(self.metadata, "recheck")
        self.assertEqual(result["execution"], "waiting_input")
        self.assertIn(("POST", "/admin/api/test/metadata"), self.writes())
        self.assertNotIn(("PUT", "/api/settings"), self.writes())

    def test_repeat_source_readback_detects_changes_during_native_connection_test(self):
        self.values["mediastorm::torrent_sources"] = ["zilean"]
        self.assertEqual(self.invoke()["verification"], "passed")
        self.native.calls.clear()
        self.native.after_test = lambda: self.native.native["torrentScrapers"][
            0
        ].update(enabled=False)
        result = self.invoke(operation="recheck")
        self.assertNotEqual(result["verification"], "passed")
        self.assertNotIn(("PUT", "/api/settings"), self.writes())

    def test_omitted_source_does_not_delete_existing_entry(self):
        self.native.native["torrentScrapers"] = [
            {
                "name": "Keep",
                "type": "other",
                "url": "https://example.test",
                "enabled": False,
            }
        ]
        self.values["mediastorm::torrent_sources"] = ["zilean"]
        self.assertEqual(self.invoke()["execution"], "applied")
        self.values["mediastorm::torrent_sources"] = []
        self.assertEqual(self.invoke()["execution"], "waiting_input")
        self.assertEqual(len(self.native.native["torrentScrapers"]), 2)

    def test_schema_secrets_are_typed_and_internal_session_is_hidden(self):
        fields = adapter.schema_fields(
            {"id": "mediastorm", "service_key": "mediastorm"}, self.config
        )
        session = next(
            field for field in fields if field["key"].endswith("::settings_session")
        )
        self.assertTrue(session["internal"])
        self.assertTrue(session["secret"])
        for field in fields:
            if "password" in field["key"] or field["key"].endswith("_api_key"):
                self.assertEqual(field["type"], "password")

    def test_shared_native_account_resolves_privately_with_an_independent_budget(self):
        payload = {
            "core_services": [{"name": "infinidysk"}],
            "optional_services": ["mediastorm"],
            "arrangement": {
                "values": {
                    "infinidysk::nntp": [
                        {
                            "name": "Usenet",
                            "host": "news.example.test",
                            "port": 563,
                            "username": "account",
                            "password": "private",
                            "tls": True,
                            "connections": 5,
                            "account_connection_limit": 10,
                        }
                    ],
                    "mediastorm::nntp_source": "infinidysk",
                    "mediastorm::nntp_connections": 5,
                }
            },
        }
        result = resolve_provider_inputs(payload, strict=True)
        self.assertEqual(
            result["arrangement"]["values"]["mediastorm::nntp"][0]["password"],
            "private",
        )
        self.assertNotIn("mediastorm::nntp", payload["arrangement"]["values"])
        self.assertEqual(
            validate_connection_budgets(result)["requested_connections"], 10
        )
        payload["arrangement"]["values"]["mediastorm::nntp_connections"] = 6
        with self.assertRaisesRegex(ValueError, "exceed"):
            validate_connection_budgets(resolve_provider_inputs(payload, strict=True))

    def test_shared_native_account_rejects_cycles_and_unselected_sources(self):
        payload = {
            "core_services": [{"name": "infinidysk"}],
            "optional_services": ["mediastorm"],
            "arrangement": {
                "values": {
                    "infinidysk::nntp_source": "mediastorm",
                    "mediastorm::nntp_source": "infinidysk",
                }
            },
        }
        with self.assertRaisesRegex(ValueError, "cycle"):
            resolve_provider_inputs(payload, strict=True)
        payload["core_services"] = []
        with self.assertRaisesRegex(ValueError, "selected provider"):
            resolve_provider_inputs(payload, strict=True)


if __name__ == "__main__":
    unittest.main()
