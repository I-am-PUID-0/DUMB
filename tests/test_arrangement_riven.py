import copy
import json
from pathlib import Path
import tempfile
import unittest

from utils import arrangement_riven as adapter
from utils.arrangement_http import ApiResponse


class Native:
    def __init__(self, path):
        self.path = path
        self.document = {
            "api_key": "private-riven-key",
            "version": "0.23.6",
            "updaters": {
                "plex": {
                    "enabled": False,
                    "url": "http://localhost:32400",
                    "token": "",
                },
                "jellyfin": {
                    "enabled": False,
                    "url": "http://localhost:8096",
                    "api_key": "",
                },
                "emby": {
                    "enabled": False,
                    "url": "http://localhost:8096",
                    "api_key": "",
                },
                "library_path": "/existing/library",
            },
            "content": {"watchlist": {"enabled": True, "selection": ["preserve"]}},
            "scrapers": {"custom": {"enabled": True}},
        }
        self.schema = {
            "properties": {
                "api_key": {"type": "string"},
                "updaters": {"$ref": "#/$defs/UpdatersModel"},
            },
            "$defs": {
                "UpdatersModel": {
                    "properties": {
                        service: {"$ref": "#/$defs/" + service}
                        for service in ("plex", "jellyfin", "emby")
                    }
                },
                **{
                    service: {
                        "properties": {
                            "enabled": {"type": "boolean"},
                            "url": {"type": "string"},
                            "token" if service == "plex" else "api_key": {
                                "type": "string"
                            },
                        }
                    }
                    for service in ("plex", "jellyfin", "emby")
                },
            },
        }
        self.calls = []
        self.ignore_write = False
        self.skip_disk = False
        self.extra_mutation = False
        self.status = None
        self.identity_calls = 0
        self.change_identity = False
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.document))

    def response(self, doc, status=200):
        return ApiResponse(status, json.dumps(doc).encode())

    def factory(self, url, **options):
        native = self

        class Client:
            def request(self, method, path, **kwargs):
                native.calls.append((url, options, method, path, copy.deepcopy(kwargs)))
                if "8080" not in url:
                    native.identity_calls += 1
                    identifier = (
                        "different-id"
                        if native.change_identity and native.identity_calls > 1
                        else "media-id"
                    )
                    if path == "/":
                        return native.response(
                            {
                                "MediaContainer": {
                                    "machineIdentifier": identifier,
                                    "friendlyName": "Selected Plex",
                                }
                            }
                        )
                    if path == "/System/Info":
                        return native.response(
                            {"id": identifier, "serverName": "Selected Jellyfin"}
                        )
                    raise AssertionError((method, path))
                if native.status:
                    return native.response(
                        {"detail": "private-native-secret"}, native.status
                    )
                if options.get("headers") != {"X-Api-Key": "private-riven-key"}:
                    return native.response({}, 401)
                if path == "/api/v1/settings/schema":
                    return native.response(native.schema)
                if path == "/api/v1/settings/get/version":
                    return native.response({"version": native.document["version"]})
                if path.startswith("/api/v1/settings/get/updaters."):
                    key = path.split("updaters.", 1)[1]
                    return native.response(
                        {
                            "updaters."
                            + key: copy.deepcopy(native.document["updaters"][key])
                        }
                    )
                if method == "POST" and path == "/api/v1/settings/set/all":
                    if not native.ignore_write:
                        for service, change in kwargs["json"]["updaters"].items():
                            native.document["updaters"][service].update(change)
                            if native.extra_mutation:
                                native.document["updaters"][service]["enabled"] = True
                        if not native.skip_disk:
                            native.save()
                    return native.response(
                        {"message": "All settings updated successfully!"}
                    )
                raise AssertionError((method, path))

        return Client()


class RivenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "settings.json"
        self.native = Native(self.path)
        self.config = {
            "riven_backend": {
                "enabled": True,
                "port": 8080,
                "config_file": str(self.path),
            },
            "plex": {"enabled": True, "port": 32400},
        }
        self.values = {
            "riven_backend::media_server": "plex",
            "plex::api_key": "private-plex-key",
        }
        self.action = {
            "source": "riven_backend",
            "service_key": "riven_backend",
            "action": "backend_connection",
        }

    def call(self, apply=False, operation=None):
        return (adapter.apply_riven if apply else adapter.inspect_riven)(
            self.action,
            self.config,
            {"arrangement": {"values": self.values}},
            operation,
            client_factory=self.native.factory,
        )

    def writes(self):
        return [call for call in self.native.calls if call[2] != "GET"]

    def media(self):
        self.action["action"] = "media_connection"

    def test_standalone_backend_authenticates_without_frontend_or_media(self):
        self.config.pop("plex")
        self.values.clear()
        result = self.call()
        self.assertEqual(result["execution"], "already_present")
        self.assertEqual(result["verification"], "passed")
        self.assertTrue(result["evidence"]["standalone"])
        self.assertNotIn("_private_values", result)
        self.assertFalse(self.writes())
        result = self.call(apply=True)
        self.assertEqual(
            result["_private_values"], {"riven_backend::api_key": "private-riven-key"}
        )
        public = {
            key: value for key, value in result.items() if key != "_private_values"
        }
        self.assertNotIn("private-riven-key", json.dumps(public))

    def test_missing_generated_file_waits_and_does_not_probe(self):
        self.path.unlink()
        result = self.call()
        self.assertEqual(result["execution"], "waiting_dependency")
        self.assertTrue(result["retryable"])
        self.assertFalse(self.native.calls)

    def test_generated_key_missing_and_wrong_override_require_input(self):
        self.native.document["api_key"] = ""
        self.native.save()
        self.assertEqual(self.call()["execution"], "waiting_input")
        self.values["riven_backend::api_key"] = "wrong-private-key"
        result = self.call()
        self.assertEqual(result["execution"], "waiting_input")
        self.assertNotIn("wrong-private-key", json.dumps(result))

    def test_schema_and_version_must_be_recognized_before_writes(self):
        self.media()
        self.native.schema["$defs"]["plex"]["properties"].pop("token")
        result = self.call(apply=True)
        self.assertEqual(result["execution"], "unsupported")
        self.assertFalse(self.writes())
        self.native.document["version"] = 123
        self.assertEqual(self.call()["execution"], "unsupported")

    def test_fresh_plex_connection_preserves_all_policy_and_is_idempotent(self):
        self.media()
        original = copy.deepcopy(self.native.document)
        self.assertEqual(self.call(operation="preview")["execution"], "planned")
        self.assertFalse(self.writes())
        result = self.call(apply=True)
        self.assertEqual(result["execution"], "applied")
        self.assertEqual(result["verification"], "passed")
        self.assertFalse(result["evidence"]["updater_enabled"])
        self.assertEqual(self.native.document["content"], original["content"])
        self.assertEqual(self.native.document["scrapers"], original["scrapers"])
        self.assertEqual(
            self.native.document["updaters"]["library_path"], "/existing/library"
        )
        self.assertFalse(self.native.document["updaters"]["plex"]["enabled"])
        self.assertEqual(
            self.writes()[0][4]["json"],
            {
                "updaters": {
                    "plex": {
                        "url": "http://127.0.0.1:32400",
                        "token": "private-plex-key",
                    }
                }
            },
        )
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.call(apply=True)["execution"], "already_present")
        self.assertEqual(len(self.writes()), 1)
        for sidecar in self.path.parent.glob(".dumb-riven-*.json"):
            self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)

    def test_existing_connection_requires_opaque_exact_fresh_preview(self):
        self.media()
        self.native.document["updaters"]["plex"].update(
            enabled=True, url="http://plex.example.com", token="private-existing"
        )
        self.native.save()
        first = self.call(operation="preview")
        proposal = first["evidence"]["adoption"]
        self.assertTrue(proposal["requires_consent"])
        self.assertNotIn("private-existing", json.dumps(first))
        self.assertNotIn("private-plex-key", json.dumps(first))
        self.values["riven_backend::adopt_existing"] = True
        self.values["riven_backend::adoption_preview_id"] = proposal["preview_id"]
        self.native.document["updaters"]["plex"]["token"] = "private-concurrent-change"
        self.native.save()
        self.assertEqual(self.call(apply=True)["execution"], "waiting_input")
        self.assertFalse(self.writes())
        second = self.call(operation="preview")
        self.assertNotEqual(
            second["evidence"]["adoption"]["preview_id"], proposal["preview_id"]
        )
        self.values["riven_backend::adoption_preview_id"] = second["evidence"][
            "adoption"
        ]["preview_id"]
        self.assertEqual(self.call(apply=True)["execution"], "applied")
        self.assertTrue(self.native.document["updaters"]["plex"]["enabled"])

    def test_different_backend_auth_identity_does_not_reuse_old_ownership(self):
        self.media()
        self.assertEqual(self.call(apply=True)["execution"], "applied")
        # Unowned native field drift must not inherit the previously managed pair.
        self.native.document["updaters"]["plex"]["token"] = "user-owned-new-key"
        self.native.save()
        result = self.call(apply=True)
        self.assertEqual(result["execution"], "waiting_input")
        self.assertTrue(result["evidence"]["adoption"]["requires_consent"])
        self.assertEqual(len(self.writes()), 1)

    def test_selected_managed_plex_ignores_unrelated_global_external_override(self):
        self.media()
        self.config["dumb"] = {
            "plex_address": "http://unselected.example.com",
            "plex_token": "unselected-private",
        }
        self.assertEqual(self.call(apply=True)["execution"], "applied")
        self.assertFalse(any("unselected" in call[0] for call in self.native.calls))
        self.assertEqual(
            self.native.document["updaters"]["plex"]["token"], "private-plex-key"
        )

    def test_selected_external_jellyfin_and_emby_use_exact_api_key_fields(self):
        self.media()
        for service in ("jellyfin", "emby"):
            with self.subTest(service=service):
                self.values["riven_backend::media_server"] = "external"
                self.values["riven_backend::external_media"] = {
                    "service_key": service,
                    "url": "https://media.example.com/native",
                    "api_key": "external-private-key",
                    "allow_private": False,
                    "verify_tls": True,
                }
                result = self.call(apply=True)
                self.assertEqual(result["verification"], "passed")
                self.assertEqual(
                    self.native.document["updaters"][service]["api_key"],
                    "external-private-key",
                )
                self.assertFalse(self.native.document["updaters"][service]["enabled"])
                media_calls = [
                    call for call in self.native.calls if call[0].startswith("https://")
                ]
                self.assertFalse(media_calls[-1][1]["managed"])
                self.assertTrue(media_calls[-1][1]["verify_tls"])
                self.assertFalse(media_calls[-1][1]["allow_private"])

    def test_readback_requires_native_persistence_and_preserved_enabled_flag(self):
        self.media()
        for mode in ("ignore_write", "skip_disk", "extra_mutation"):
            with self.subTest(mode=mode):
                native = Native(self.path)
                self.native = native
                setattr(native, mode, True)
                result = self.call(apply=True)
                self.assertEqual(result["execution"], "failed")
                self.assertEqual(result["verification"], "failed")
                ledger = self.path.parent / ".dumb-riven-connection-ownership.json"
                self.assertFalse(ledger.exists())

    def test_changed_media_identity_fails_without_ownership(self):
        self.media()
        self.native.change_identity = True
        result = self.call(apply=True)
        self.assertEqual(result["execution"], "failed")
        self.assertFalse(
            (self.path.parent / ".dumb-riven-connection-ownership.json").exists()
        )

    def test_none_preserves_connections_and_missing_choice_requires_input(self):
        self.media()
        self.values["riven_backend::media_server"] = "none"
        self.assertEqual(self.call(apply=True)["execution"], "already_present")
        self.assertFalse(self.writes())
        self.values.pop("riven_backend::media_server")
        self.assertEqual(self.call(apply=True)["execution"], "waiting_input")

    def test_unready_api_retries_without_exposing_error_body(self):
        self.native.status = 503
        result = self.call()
        self.assertEqual(result["execution"], "waiting_dependency")
        self.assertTrue(result["retryable"])
        self.assertNotIn("private-native-secret", json.dumps(result))

    def test_native_file_symlink_preserved_and_no_network(self):
        real = self.path.with_name("real.json")
        self.path.rename(real)
        self.path.symlink_to(real)
        self.assertEqual(self.call()["execution"], "waiting_dependency")
        self.assertTrue(self.path.is_symlink())
        self.assertFalse(self.native.calls)

    def test_unknown_api_routes_preserve_native_settings_without_private_handoff(self):
        before = self.path.read_bytes()
        for status in (404, 405):
            with self.subTest(status=status):
                self.native.status = status
                result = self.call(apply=True)
                self.assertEqual(result["execution"], "unsupported")
                self.assertEqual(result["verification"], "unavailable")
                self.assertNotIn("_private_values", result)
                self.assertFalse(self.writes())
                self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_schema_without_selected_jellyfin_updater_is_honest(self):
        self.media()
        self.values["riven_backend::media_server"] = "external"
        self.values["riven_backend::external_media"] = {
            "service_key": "jellyfin",
            "url": "https://media.example.com",
            "api_key": "private-jellyfin-key",
        }
        self.native.schema["$defs"]["UpdatersModel"]["properties"].pop("jellyfin")
        before = self.path.read_bytes()
        result = self.call(apply=True)
        self.assertEqual(result["execution"], "unsupported")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.writes())

    def test_schema_uses_existing_frontend_types_and_secret_input(self):
        fields = adapter.schema_fields(
            {"id": "riven_backend", "service_key": "riven_backend"}, self.config
        )
        fields = {item["key"].partition("::")[2]: item for item in fields}
        self.assertEqual(fields["media_server"]["type"], "connection")
        self.assertEqual(fields["external_media"]["type"], "external_service")
        self.assertTrue(fields["api_key"]["secret"])
        self.assertNotIn("enable_updater", fields)
        self.action["service_key"] = "riven_frontend"
        self.assertIsNone(self.call(apply=True))


if __name__ == "__main__":
    unittest.main()
