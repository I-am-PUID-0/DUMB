import copy
import json
import unittest

from utils.arrangement_inputs import resolve_provider_inputs
from utils.arrangement_providers import validate_connection_budgets


class SharedProviderInputsTests(unittest.TestCase):
    def payload(self):
        return {
            "core_services": [{"name": "infinidysk"}, {"name": "decypharr"}],
            "arrangement": {
                "values": {
                    "infinidysk::nntp": [
                        {
                            "host": "news.example.com",
                            "username": "example-user",
                            "password": "example-password",
                            "port": 563,
                            "tls": True,
                            "connections": 5,
                            "account_connection_limit": 10,
                        }
                    ],
                    "decypharr::nntp_source": "infinidysk",
                    "decypharr::nntp_connections": 5,
                }
            },
        }

    def test_credentials_are_referenced_once_but_both_allocations_are_counted(self):
        payload = self.payload()
        original = copy.deepcopy(payload)
        expanded = resolve_provider_inputs(payload, strict=True)
        self.assertEqual(payload, original)
        self.assertNotIn("decypharr::nntp", payload["arrangement"]["values"])
        self.assertEqual(json.dumps(payload).count("example-password"), 1)
        self.assertEqual(
            expanded["arrangement"]["values"]["decypharr::nntp"][0]["password"],
            "example-password",
        )
        self.assertEqual(
            validate_connection_budgets(expanded)["requested_connections"], 10
        )
        self.assertEqual(validate_connection_budgets(expanded)["shared_accounts"], 1)

    def test_reused_allocation_cannot_exceed_declared_ceiling(self):
        payload = self.payload()
        payload["arrangement"]["values"]["decypharr::nntp_connections"] = 6
        with self.assertRaises(ValueError):
            validate_connection_budgets(resolve_provider_inputs(payload))

    def test_reference_cycles_unselected_sources_and_boolean_allocations_rejected(self):
        for key, value in (
            ("infinidysk::nntp_source", "decypharr"),
            ("decypharr::nntp_source", "altmount"),
            ("decypharr::nntp_connections", True),
        ):
            with self.subTest(key=key, value=value):
                payload = self.payload()
                payload["arrangement"]["values"][key] = value
                with self.assertRaises(ValueError):
                    resolve_provider_inputs(payload)

    def test_missing_source_accounts_stay_pending_for_planning(self):
        payload = self.payload()
        del payload["arrangement"]["values"]["infinidysk::nntp"]
        self.assertEqual(
            resolve_provider_inputs(payload)["arrangement"]["values"][
                "decypharr::nntp"
            ],
            [],
        )
        with self.assertRaises(ValueError):
            resolve_provider_inputs(payload, strict=True)

    def test_private_authorization_follows_account_but_copies_are_independent(self):
        payload = self.payload()
        payload["arrangement"]["values"]["infinidysk::allow_private_nntp"] = True
        expanded = resolve_provider_inputs(payload)
        values = expanded["arrangement"]["values"]
        self.assertTrue(values["decypharr::allow_private_nntp"])
        values["decypharr::nntp"][0]["password"] = "replacement"
        self.assertEqual(values["infinidysk::nntp"][0]["password"], "example-password")


if __name__ == "__main__":
    unittest.main()
