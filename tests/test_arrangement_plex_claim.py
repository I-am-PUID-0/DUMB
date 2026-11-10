import json
from pathlib import Path
import tempfile
import unittest

from utils.arrangement_http import ApiResponse
from utils.arrangement_plex_claim import apply_claim, inspect_claim


class PlexClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.preferences = Path(self.temp.name) / "Preferences.xml"
        self.preferences.write_text(
            '<Preferences PlexOnlineToken="example-local-key"/>'
        )
        self.config = {
            "plex": {"port": 32400, "config_file": str(self.preferences)},
            "dumb": {
                "plex_address": "https://external.example.com",
                "plex_token": "example-external-key",
            },
        }
        self.action = {"source": "plex", "service_key": "plex", "action": "account"}
        self.payload = {
            "arrangement": {"values": {"plex::claim_token": "claim-example-fresh"}}
        }
        self.claimed, self.accepted = False, True
        self.calls, self.origins = [], []

        def factory(url, **kwargs):
            self.origins.append((url, kwargs))
            return self

        self.factory = factory

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == "/identity":
            body = {"MediaContainer": {"claimed": self.claimed}}
        elif path == "/myplex/claim":
            if not self.accepted:
                return ApiResponse(401, b"{}")
            self.assertEqual(kwargs["params"], {"token": "claim-example-fresh"})
            self.claimed = True
            body = {}
        elif path.startswith("/library/sections"):
            body = {"MediaContainer": {"Directory": []}}
        else:
            raise AssertionError(path)
        return ApiResponse(200, json.dumps(body).encode())

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def test_focused_native_claim_uses_managed_identity_and_private_key_handoff(self):
        before = self.preferences.read_bytes()
        result = apply_claim(
            self.action, self.config, self.payload, client_factory=self.factory
        )
        self.assertEqual(result["execution"], "applied")
        self.assertEqual(
            result["_private_values"], {"plex::api_key": "example-local-key"}
        )
        self.assertEqual(result["_clear_inputs"], ["plex::claim_token"])
        self.assertFalse(result["evidence"]["installer_repeated"])
        self.assertEqual(self.preferences.read_bytes(), before)
        self.assertTrue(
            all(
                url == "http://127.0.0.1:32400"
                and options["headers"]["X-Plex-Token"] == "example-local-key"
                for url, options in self.origins
            )
        )
        self.assertNotIn(
            "claim-example-fresh",
            json.dumps(
                {key: value for key, value in result.items() if not key.startswith("_")}
            ),
        )

    def test_already_claimed_and_read_only_checks_never_post_a_claim(self):
        result = inspect_claim(
            self.action, self.config, self.payload, client_factory=self.factory
        )
        self.assertEqual(result["execution"], "planned")
        self.assertFalse(any(method == "POST" for method, *_ in self.calls))
        self.claimed = True
        result = apply_claim(
            self.action, self.config, self.payload, client_factory=self.factory
        )
        self.assertEqual(result["execution"], "already_present")
        self.assertEqual(result["verification"], "passed")
        self.assertFalse(any(method == "POST" for method, *_ in self.calls))

    def test_expired_claim_is_editable_input_without_install_or_restart(self):
        self.accepted = False
        result = apply_claim(
            self.action, self.config, self.payload, client_factory=self.factory
        )
        self.assertEqual(result["execution"], "waiting_input")
        self.assertFalse(result["retryable"])
        self.assertEqual(sum(method == "POST" for method, *_ in self.calls), 1)


if __name__ == "__main__":
    unittest.main()
