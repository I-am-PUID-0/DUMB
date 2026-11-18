import copy
import json
import unittest

from utils.infinidysk_v2_provider_diagnostics import sanitize_provider_diagnostics


class ProviderDiagnosticsTests(unittest.TestCase):
    def sample(self):
        return {
            "schema_version": 1,
            "providers": [
                {
                    "provider_ordinal": 1,
                    "requests": 7,
                    "bytes_received": 800,
                    "retries": 2,
                    "failures": {"timeout": 2, "article_missing": 1},
                }
            ],
            "unbound_failures": {"bridge_handshake": 1},
        }

    def test_schema_copies_only_fixed_keys_and_failure_codes(self):
        value = self.sample()
        value.update(host="private.example.invalid", exception="private-message")
        value["providers"][0].update(password="private-password", article="private-id")
        value["providers"][0]["failures"]["private-error-message"] = 9
        value["unbound_failures"]["private-message"] = 3
        result = sanitize_provider_diagnostics(value)
        self.assertEqual(result, self.sample())
        self.assertNotIn("private", json.dumps(result))
        value["providers"][0]["failures"]["timeout"] = 8
        self.assertEqual(result["providers"][0]["failures"]["timeout"], 2)

    def test_malformed_protocol_identity_or_counts_are_not_public_diagnostics(self):
        for changed in (
            {"schema_version": True},
            {"schema_version": 2},
            {"providers": {}},
            {"providers": self.sample()["providers"] * 2},
            {"providers": self.sample()["providers"] * 65},
            {"final": "yes"},
            {"recorded_at": -1},
        ):
            self.assertIsNone(
                sanitize_provider_diagnostics({**self.sample(), **changed})
            )
        for key in ("provider_ordinal", "requests", "bytes_received", "retries"):
            for invalid in (True, -1, "2", 2**53):
                value = self.sample()
                value["providers"][0][key] = invalid
                self.assertIsNone(sanitize_provider_diagnostics(value), (key, invalid))
        for location in ("providers", "unbound_failures"):
            value = copy.deepcopy(self.sample())
            failures = (
                value["providers"][0]["failures"]
                if location == "providers"
                else value["unbound_failures"]
            )
            failures["timeout"] = True
            self.assertIsNone(sanitize_provider_diagnostics(value))

    def test_final_observation_is_distinct_from_migration_completion(self):
        value = {**self.sample(), "final": True, "recorded_at": 1234}
        self.assertEqual(sanitize_provider_diagnostics(value), value)


if __name__ == "__main__":
    unittest.main()
