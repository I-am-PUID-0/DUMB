import base64
import copy
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from utils import arrangement_external_provider as adapter
from utils.arrangement_http import ApiResponse


class NativeProvider:
    def __init__(self):
        self.calls = []
        self.settings = {
            "arr.instances": json.dumps(
                {
                    "SonarrInstances": [
                        {
                            "Host": "https://user.example.com",
                            "ApiKey": "user-key",
                            "Instance": "User",
                            "Category": "user",
                        }
                    ],
                    "QueueRules": [{"preserve": True}],
                }
            ),
            "api.categories": "user",
        }
        self.document = {
            "usenet": {"providers": []},
            "arrs": [],
            "categories": ["user"],
            "user_policy": {"enabled": False},
        }
        self.live_categories = {"user": {"name": "user"}}
        self.drop_update = False
        self.version_doc = {"version": "4.5.0"}
        self.mask_infini = False
        self.stale_sab = False
        self.nonce = 0

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, copy.deepcopy(kwargs)))
        value = {}
        if (kwargs.get("params") or {}).get("mode") == "get_config":
            categories = ["user"] if self.stale_sab else self.document["categories"]
            value = {
                "config": {
                    "categories": [{"name": category} for category in categories]
                }
            }
        elif (kwargs.get("params") or {}).get("mode") == "version":
            value = self.version_doc
        elif path == "/api/get-config":
            settings = dict(self.settings)
            if self.mask_infini:
                document = json.loads(settings["arr.instances"])
                for rows in document.values():
                    if not isinstance(rows, list):
                        continue
                    for row in rows:
                        if not isinstance(row, dict) or "ApiKey" not in row:
                            continue
                        self.nonce += 1
                        encode = (
                            lambda raw: base64.urlsafe_b64encode(raw)
                            .decode()
                            .rstrip("=")
                        )
                        nonce = encode(self.nonce.to_bytes(16, "big"))
                        message = (
                            chr(0)
                            .join(("arr.instances", "ApiKey", nonce, row["ApiKey"]))
                            .encode()
                        )
                        signature = hmac.new(
                            b"provider-private-key", message, hashlib.sha256
                        ).digest()
                        row["ApiKey"] = (
                            adapter._MASK_PREFIX + nonce + "." + encode(signature)
                        )
                settings["arr.instances"] = json.dumps(document)
            value = {
                "configItems": [
                    {"configName": key, "configValue": settings[key]}
                    for key in kwargs["data"]["config-keys"]
                ]
            }
        elif path == "/api/update-config":
            if not self.drop_update:
                self.settings.update(kwargs["data"])
        elif path == "/api/config":
            if method == "POST" and not self.drop_update:
                self.document.update(copy.deepcopy(kwargs["json"]))
            value = copy.deepcopy(self.document)
        elif path.startswith("/api/config/"):
            section = path.rsplit("/", 1)[1]
            if not self.drop_update:
                # Native PATCH deep-copies current config, then updates supplied
                # fields only; masked fallback keys are never sent back by DUMB.
                self.document[section].update(copy.deepcopy(kwargs["json"][section]))
            value = copy.deepcopy(self.document)
        elif path == "/api/v2/auth/login":
            body = kwargs["data"]
            allowed = any(
                row.get("host") == body["username"]
                and row.get("token") == body["password"]
                for row in self.document["arrs"]
            )
            return ApiResponse(
                200 if allowed else 401, b"Ok." if allowed else b"Fails."
            )
        elif path == "/api/v2/torrents/categories":
            value = copy.deepcopy(self.live_categories)
        elif path == "/api/v2/torrents/createCategory":
            category = kwargs["data"]["category"]
            self.live_categories[category] = {"name": category}
            value = None
        return ApiResponse(200, json.dumps(value).encode())


class ExternalProviderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.native = NativeProvider()
        self.target = {
            "id": "external.radarr.movie",
            "family": "radarr",
            "url": "https://arr.example.com/base",
            "key": "arr-private-key",
        }
        self.config = {
            "infinidysk": {
                "enabled": True,
                "env": {"FRONTEND_BACKEND_API_KEY": "provider-private-key"},
            },
            "altmount": {
                "enabled": True,
                "config_dir": self.directory.name,
                "config_file": str(self.root / "altmount.yaml"),
                "env": {"ALTMOUNT_API_KEY": "provider-private-key"},
            },
            "decypharr": {
                "enabled": True,
                "config_dir": self.directory.name,
                "config_file": str(self.root / "decypharr.json"),
            },
        }
        (self.root / "decypharr.json").write_text(
            json.dumps({"url_base": "/decypharr"})
        )
        self.payload = {"arrangement": {"values": {}}}

    def call(self, provider, operation="inspect", approved=None):
        with patch.object(adapter, "_client", return_value=self.native):
            return adapter.external_arr_provider_credentials_and_category(
                self.config,
                self.payload,
                provider,
                self.target,
                "dumb-movies",
                operation,
                client_factory=lambda *args, **kwargs: self.native,
                ownership_directory=self.root / "ownership",
                approved_provider_preview=approved,
            )

    def alt(self):
        self.native.document = {
            "providers": [],
            "arrs": {
                "enabled": True,
                "radarr_instances": [],
                "sonarr_instances": [
                    {
                        "name": "User",
                        "url": "https://user.example.com",
                        "api_key": "user-key",
                        "enabled": False,
                    }
                ],
                "queue_cleanup_enabled": False,
            },
            "sabnzbd": {
                "enabled": True,
                "categories": [{"name": "user", "dir": "user"}],
                "fallback_api_key": "********",
                "history_retention_minutes": 999,
            },
            "user_policy": {"enabled": False},
        }
        (self.root / "altmount.yaml").write_text(
            "providers: []\narrs:\n  enabled: true\n  radarr_instances: []\nsabnzbd:\n  enabled: true\n  categories: []\n"
        )
        self.payload["arrangement"]["values"][
            "altmount::settings_token"
        ] = "session-placeholder"

    def test_inspect_returns_private_fields_without_provisioning_or_files(self):
        result = self.call("infinidysk")
        self.assertEqual("Sabnzbd", result["implementation"])
        self.assertEqual("provider-private-key", result["fields"]["apiKey"])
        self.assertEqual("planned", result["evidence"]["provider_registration"])
        self.assertEqual(
            [("GET", "/api"), ("POST", "/api/get-config")],
            [(row[0], row[1]) for row in self.native.calls],
        )
        self.assertFalse((self.root / "ownership").exists())
        self.assertNotIn("private-key", json.dumps(result["evidence"]))

    def test_infinidysk_adds_exact_registration_and_category_preserving_other_rows(
        self,
    ):
        previous = json.loads(self.native.settings["arr.instances"])
        result = self.call("infinidysk", "apply")
        self.assertEqual("passed", result["evidence"]["provider_registration"])
        saved = json.loads(self.native.settings["arr.instances"])
        self.assertEqual(previous["SonarrInstances"], saved["SonarrInstances"])
        self.assertEqual(previous["QueueRules"], saved["QueueRules"])
        self.assertEqual(self.target["url"], saved["RadarrInstances"][0]["Host"])
        self.native.calls.clear()
        self.assertEqual(
            "passed",
            self.call("infinidysk", "apply")["evidence"]["provider_registration"],
        )
        self.assertFalse(
            any(row[1] == "/api/update-config" for row in self.native.calls)
        )
        ledger = next((self.root / "ownership").glob("*.json"))
        self.assertEqual(0o600, ledger.stat().st_mode & 0o777)
        self.assertNotIn("arr-private-key", ledger.read_text())

    def test_user_registration_with_different_category_is_preserved(self):
        self.native.settings["arr.instances"] = json.dumps(
            {
                "RadarrInstances": [
                    {
                        "Host": self.target["url"],
                        "ApiKey": "user-key",
                        "Category": "my-category",
                        "Instance": "User",
                    }
                ]
            }
        )
        before = dict(self.native.settings)
        result = self.call("infinidysk", "apply")
        self.assertEqual("waiting_input", result["execution"])
        self.assertEqual(before, self.native.settings)

    def test_user_registration_can_be_adopted_only_from_exact_fresh_preview(self):
        self.native.settings["arr.instances"] = json.dumps(
            {
                "RadarrInstances": [
                    {
                        "Host": self.target["url"],
                        "ApiKey": "user-key",
                        "Category": "old",
                        "Instance": "User",
                        "Unrelated": True,
                    }
                ]
            }
        )
        preview = self.call("infinidysk")["_provider_adoption"]
        self.assertTrue(preview["requires_consent"])
        self.assertEqual("user-key", preview["before"]["registration.ApiKey"])
        self.assertEqual(self.target["key"], preview["after"]["registration.ApiKey"])
        before = copy.deepcopy(self.native.settings)
        self.assertEqual(
            "waiting_input", self.call("infinidysk", "apply", "stale")["execution"]
        )
        self.assertEqual(before, self.native.settings)
        result = self.call("infinidysk", "apply", preview["fingerprint"])
        self.assertEqual("Sabnzbd", result["implementation"])
        row = json.loads(self.native.settings["arr.instances"])["RadarrInstances"][0]
        self.assertTrue(row["Unrelated"])
        self.assertEqual("dumb-movies", row["Category"])

    def test_native_edits_invalidate_accepted_adoption_preview(self):
        self.alt()
        self.native.document["arrs"]["radarr_instances"] = [
            {
                "name": "dumb-movies",
                "url": self.target["url"],
                "api_key": "user-key",
                "enabled": False,
            }
        ]
        preview = self.call("altmount")["_provider_adoption"]
        self.native.document["arrs"]["radarr_instances"][0]["api_key"] = "new-user-key"
        before = copy.deepcopy(self.native.document)
        result = self.call("altmount", "apply", preview["fingerprint"])
        self.assertEqual("waiting_input", result["execution"])
        self.assertEqual(before, self.native.document)
        self.assertNotEqual(
            preview["fingerprint"], result["_provider_adoption"]["fingerprint"]
        )

    def test_reused_external_identifier_or_provider_auth_does_not_reuse_ownership(self):
        for changed_identity in ("arr-key", "arr-origin", "provider-key"):
            with self.subTest(identity=changed_identity):
                self.setUp()
                self.call("decypharr", "apply")
                if changed_identity == "arr-key":
                    self.target["key"] = "rotated-arr-key"
                elif changed_identity == "arr-origin":
                    self.target["url"] = "https://other-arr.example.com/base"
                else:
                    self.config["decypharr"]["api_key"] = "rotated-provider-key"
                    self.native.document["arrs"][0]["token"] = "edited-outside-dumb"
                before = copy.deepcopy(self.native.document)
                preview = self.call("decypharr")["_provider_adoption"]
                self.assertTrue(preview["requires_consent"])
                self.assertEqual(
                    "waiting_input", self.call("decypharr", "apply")["execution"]
                )
                self.assertEqual(before, self.native.document)

    def test_altmount_updates_only_arrs_and_category_sections_not_masked_secrets(self):
        self.alt()
        before = copy.deepcopy(self.native.document)
        result = self.call("altmount", "apply")
        self.assertEqual("/sabnzbd", result["fields"]["urlBase"])
        self.assertEqual(
            before["arrs"]["sonarr_instances"],
            self.native.document["arrs"]["sonarr_instances"],
        )
        self.assertFalse(self.native.document["arrs"]["queue_cleanup_enabled"])
        self.assertEqual(
            999, self.native.document["sabnzbd"]["history_retention_minutes"]
        )
        patches = [row for row in self.native.calls if row[0] == "PATCH"]
        self.assertEqual(
            {"/api/config/arrs", "/api/config/sabnzbd"}, {row[1] for row in patches}
        )
        self.assertNotIn("fallback_api_key", json.dumps([row[2] for row in patches]))
        self.assertEqual(before["user_policy"], self.native.document["user_policy"])

    def test_altmount_disabled_sab_needs_explicit_choice(self):
        self.alt()
        self.native.document["sabnzbd"]["enabled"] = False
        self.assertEqual("waiting_input", self.call("altmount", "apply")["execution"])
        self.payload["arrangement"]["values"][
            self.target["id"] + "::altmount::enable_api"
        ] = True
        preview = self.call("altmount")["_provider_adoption"]
        self.assertTrue(preview["requires_consent"])
        self.assertFalse(preview["before"]["api.enabled"])
        result = self.call("altmount", "apply", preview["fingerprint"])
        self.assertEqual("Sabnzbd", result["implementation"])
        self.assertTrue(self.native.document["sabnzbd"]["enabled"])

    def test_global_callback_enable_requires_fresh_review_of_other_enabled_rows(self):
        self.alt()
        self.native.document["arrs"]["enabled"] = False
        self.native.document["arrs"]["sonarr_instances"][0]["enabled"] = True
        self.assertEqual("waiting_input", self.call("altmount")["execution"])
        self.payload["arrangement"]["values"][
            self.target["id"] + "::altmount::enable_callbacks"
        ] = True
        preview = self.call("altmount")["_provider_adoption"]
        self.assertTrue(preview["requires_consent"])
        self.assertEqual(
            "User",
            preview["before"]["callbacks.existing_enabled_registrations"][0]["name"],
        )
        self.native.document["arrs"]["sonarr_instances"][0][
            "url"
        ] = "https://changed.example.com"
        self.assertEqual(
            "waiting_input",
            self.call("altmount", "apply", preview["fingerprint"])["execution"],
        )
        self.assertFalse(self.native.document["arrs"]["enabled"])
        current = self.call("altmount")["_provider_adoption"]
        self.assertEqual(
            "Sabnzbd",
            self.call("altmount", "apply", current["fingerprint"])["implementation"],
        )
        self.assertTrue(self.native.document["arrs"]["enabled"])

    def test_altmount_local_preview_and_api_defaults_have_identical_adoption_scope(
        self,
    ):
        self.alt()
        self.native.document["arrs"]["enabled"] = False
        self.native.document["arrs"]["sonarr_instances"] = []
        self.native.document["arrs"].update(max_workers=5, readarr_instances=None)
        (self.root / "altmount.yaml").write_text(
            "arrs:\n  enabled: false\n  radarr_instances: []\nsabnzbd:\n  enabled: true\n  categories: []\n"
        )
        # Native API expands default fields and empty families absent from YAML;
        # those unrelated defaults do not invalidate an identical callback plan.
        self.payload["arrangement"]["values"] = {
            self.target["id"] + "::altmount::enable_callbacks": True,
            "altmount::admin_username": "operator",
            "altmount::admin_password": "temporary-password",
        }
        preview = self.call("altmount")["_provider_adoption"]
        result = self.call("altmount", "apply", preview["fingerprint"])
        self.assertEqual("Sabnzbd", result["implementation"])
        self.assertEqual("passed", result["evidence"]["provider_registration"])

    def test_altmount_preview_can_read_native_file_without_signing_in(self):
        self.alt()
        self.payload["arrangement"]["values"] = {
            "altmount::admin_username": "operator",
            "altmount::admin_password": "temporary-password",
        }
        result = self.call("altmount")
        self.assertEqual("Sabnzbd", result["implementation"])
        self.assertEqual(["GET"], [call[0] for call in self.native.calls])

    def test_decypharr_callback_credentials_and_live_category_are_verified(self):
        previous = copy.deepcopy(self.native.document)
        result = self.call("decypharr", "apply")
        self.assertEqual("QBittorrent", result["implementation"])
        self.assertEqual(
            {
                "username": self.target["url"],
                "password": "arr-private-key",
                "urlBase": "/decypharr",
            },
            result["fields"],
        )
        self.assertEqual(previous["user_policy"], self.native.document["user_policy"])
        self.assertIn("dumb-movies", self.native.document["categories"])
        self.assertIn("dumb-movies", self.native.live_categories)
        self.assertIn(
            ("POST", "/api/v2/auth/login"),
            [(row[0], row[1]) for row in self.native.calls],
        )
        self.assertFalse(
            any(
                "torrent" in row[1] and ("add" in row[1] or "delete" in row[1])
                for row in self.native.calls
            )
        )
        self.native.calls.clear()
        self.assertEqual(
            "QBittorrent", self.call("decypharr", "apply")["implementation"]
        )
        self.assertFalse(
            any(
                row[1] == "/api/config" and row[0] == "POST"
                for row in self.native.calls
            )
        )

    def test_infini_signed_random_masks_verify_known_key_without_database_read(self):
        self.native.mask_infini = True
        with patch.object(
            adapter,
            "_private_infini_registration",
            side_effect=AssertionError("known keys need no DB read"),
        ):
            self.assertEqual(
                "passed",
                self.call("infinidysk", "apply")["evidence"]["provider_registration"],
            )
            self.native.calls.clear()
            self.assertEqual(
                "passed",
                self.call("infinidysk", "apply")["evidence"]["provider_registration"],
            )
        self.assertFalse(
            any(row[1] == "/api/update-config" for row in self.native.calls)
        )

    def test_unknown_infini_key_uses_verified_private_snapshot_and_rejects_drift(self):
        self.native.settings["arr.instances"] = json.dumps(
            {
                "RadarrInstances": [
                    {
                        "Host": self.target["url"],
                        "ApiKey": "existing-private-key",
                        "Instance": "User",
                        "Category": "old",
                    }
                ]
            }
        )
        self.native.mask_infini = True
        with patch.object(
            adapter,
            "_private_infini_registration",
            side_effect=lambda cfg: json.loads(self.native.settings["arr.instances"]),
        ):
            preview = self.call("infinidysk")["_provider_adoption"]
            self.assertEqual(preview, self.call("infinidysk")["_provider_adoption"])
            self.assertEqual(
                "existing-private-key", preview["before"]["registration.ApiKey"]
            )
            document = json.loads(self.native.settings["arr.instances"])
            document["RadarrInstances"][0]["ApiKey"] = "edited-private-key"
            self.native.settings["arr.instances"] = json.dumps(document)
            before = dict(self.native.settings)
            refused = self.call("infinidysk", "apply", preview["fingerprint"])
            self.assertEqual("waiting_input", refused["execution"])
            self.assertEqual(before, self.native.settings)
            current = refused["_provider_adoption"]
            self.assertNotEqual(current["fingerprint"], preview["fingerprint"])
            self.assertEqual(
                "passed",
                self.call("infinidysk", "apply", current["fingerprint"])["evidence"][
                    "provider_registration"
                ],
            )

    def test_infini_mask_and_selected_native_store_mismatch_preserves_state(self):
        self.native.settings["arr.instances"] = json.dumps(
            {
                "RadarrInstances": [
                    {
                        "Host": self.target["url"],
                        "ApiKey": "existing-private-key",
                        "Instance": "User",
                        "Category": "old",
                    }
                ]
            }
        )
        self.native.mask_infini = True
        with patch.object(
            adapter,
            "_private_infini_registration",
            return_value={"RadarrInstances": []},
        ):
            self.assertEqual(
                "waiting_input", self.call("infinidysk", "apply")["execution"]
            )
        self.assertFalse(
            any(row[1] == "/api/update-config" for row in self.native.calls)
        )

    def test_native_sab_rejection_is_not_success_and_prevents_provider_mutation(self):
        self.native.version_doc = {"status": False, "error": "Invalid API key"}
        before = copy.deepcopy(self.native.settings)
        result = self.call("infinidysk", "apply")
        self.assertEqual("waiting_input", result["execution"])
        self.assertEqual(before, self.native.settings)
        self.assertNotIn("/api/update-config", [row[1] for row in self.native.calls])

    def test_decypharr_nntp_exposes_private_sab_alongside_callback_bound_qbit(self):
        self.native.document["usenet"]["providers"] = [{"host": "news.example.com"}]
        (self.root / "auth.json").write_text(
            json.dumps({"api_token": "private-native-sab-token"})
        )
        inspected = self.call("decypharr")
        additional = inspected["additional_clients"]
        self.assertEqual(
            [
                {
                    "implementation": "Sabnzbd",
                    "name_suffix": "usenet",
                    "fields": {
                        "apiKey": "private-native-sab-token",
                        "urlBase": "/decypharr/sabnzbd",
                    },
                }
            ],
            additional,
        )
        self.assertFalse(
            any(
                row[0] in {"PATCH", "PUT"}
                or (row[0] == "POST" and row[1] != "/api/get-config")
                for row in self.native.calls
            )
        )
        self.assertNotIn("private-native-sab-token", json.dumps(inspected["evidence"]))
        self.assertEqual(
            additional, self.call("decypharr", "apply")["additional_clients"]
        )
        self.assertIn(
            ("GET", "/decypharr/sabnzbd/api"),
            [(row[0], row[1]) for row in self.native.calls],
        )

    def test_decypharr_sab_startup_cache_requires_explicit_restart_before_client_ready(
        self,
    ):
        self.native.document["usenet"]["providers"] = [{"host": "news.example.com"}]
        (self.root / "auth.json").write_text(
            json.dumps({"api_token": "private-native-sab-token"})
        )
        self.native.stale_sab = True
        result = self.call("decypharr", "apply")
        self.assertEqual("waiting_input", result["execution"])
        self.assertFalse(result["retryable"])
        self.assertEqual("decypharr", result["evidence"]["restart_required"])
        self.assertEqual("planned", result["evidence"]["provider_sab_category"])
        self.assertEqual("Sabnzbd", result["additional_clients"][0]["implementation"])
        self.native.calls.clear()
        self.assertEqual("waiting_input", self.call("decypharr", "apply")["execution"])
        self.assertFalse(
            any(
                row[0] == "POST" and row[1] == "/api/config"
                for row in self.native.calls
            )
        )
        # A controlled lifecycle restart rebuilds the native cache. Reuse the
        # persisted category and keys without rewriting other provider settings.
        self.native.stale_sab = False
        verified = self.call("decypharr", "apply")
        self.assertNotIn("execution", verified)
        self.assertEqual("passed", verified["evidence"]["provider_sab_category"])

    def test_exact_native_registration_has_no_repeated_adoption_delta(self):
        for provider in ("infinidysk", "altmount", "decypharr"):
            with self.subTest(provider=provider):
                self.setUp()
                if provider == "altmount":
                    self.alt()
                result = self.call(provider, "apply")
                self.assertNotIn("execution", result)
                current = self.call(provider)["_provider_adoption"]
                self.assertEqual(current["before"], current["after"])
                self.assertFalse(current["requires_consent"])

    def test_ignored_native_write_is_not_reported_as_success(self):
        self.native.drop_update = True
        result = self.call("infinidysk", "apply")
        self.assertEqual("waiting_input", result["execution"])
        self.assertIn("did not retain", result["reason"])
