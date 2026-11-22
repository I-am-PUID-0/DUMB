import copy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from utils.infinidysk_v2_sab import (
    NativeSabKeyError,
    SAB_ENV_NAME,
    adopt_prepared_sab_key,
    adopt_running_sab_key,
    canonical_sab_environment,
    configured_sab_key,
)


class NativeSabConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.key = "fixture-native-download-credential"
        self.service = {
            "enabled": True,
            "runtime_generation": 2,
            "process_name": "InfiniDysk",
            "env": {"TZ": "UTC", "FRONTEND_BACKEND_API_KEY": "legacy-fixture"},
        }
        self.manager = SimpleNamespace(
            config={"infinidysk": self.service}, save_config=Mock()
        )

    def test_existing_effective_key_is_adopted_once_without_legacy_alias(self):
        self.assertEqual(
            adopt_running_sab_key(self.manager, self.service, self.key), self.key
        )
        self.assertEqual(self.service["env"], {"TZ": "UTC", SAB_ENV_NAME: self.key})
        adopt_running_sab_key(self.manager, self.service, self.key)
        self.manager.save_config.assert_called_once_with("InfiniDysk")

    def test_configured_key_is_authoritative_until_restart_applies_it(self):
        self.service["env"][SAB_ENV_NAME] = "fixture-explicit-download-credential"
        original = copy.deepcopy(self.service)
        with self.assertRaises(NativeSabKeyError) as error:
            adopt_running_sab_key(self.manager, self.service, self.key)
        self.assertNotIn(self.key, str(error.exception))
        self.assertEqual(self.service, original)
        self.manager.save_config.assert_not_called()

    def test_save_failure_rolls_back_in_memory_and_sanitizes_error(self):
        original = copy.deepcopy(self.service)
        self.manager.save_config.side_effect = OSError(self.key)
        with self.assertRaises(NativeSabKeyError) as error:
            adopt_running_sab_key(self.manager, self.service, self.key)
        self.assertNotIn(self.key, str(error.exception))
        self.assertEqual(self.service, original)

    def test_stale_disabled_or_v1_service_is_never_saved(self):
        for case in ("stale", "disabled", "v1"):
            with self.subTest(case=case):
                service = copy.deepcopy(self.service)
                if case == "disabled":
                    service["enabled"] = False
                elif case == "v1":
                    service["runtime_generation"] = 1
                self.manager.config["infinidysk"] = (
                    copy.deepcopy(service) if case == "stale" else service
                )
                with self.assertRaises(NativeSabKeyError):
                    adopt_running_sab_key(self.manager, service, self.key)
        self.manager.save_config.assert_not_called()

    def test_empty_leaf_does_not_override_running_native_key(self):
        self.service["env"][SAB_ENV_NAME] = ""
        self.assertIsNone(configured_sab_key(self.service))
        self.assertNotIn(SAB_ENV_NAME, canonical_sab_environment(self.service))
        adopt_running_sab_key(self.manager, self.service, self.key)
        self.assertEqual(self.service["env"][SAB_ENV_NAME], self.key)

    def test_aliases_are_canonical_and_conflicts_require_review(self):
        self.service["env"]["INFINIDYSK__sab__api_key"] = self.key
        self.assertEqual(
            canonical_sab_environment(self.service)[SAB_ENV_NAME], self.key
        )
        adopt_running_sab_key(self.manager, self.service, self.key)
        self.assertNotIn("INFINIDYSK__sab__api_key", self.service["env"])
        self.service["env"]["INFINIDYSK__sab__api_key"] = "other-fixture"
        with self.assertRaises(NativeSabKeyError):
            canonical_sab_environment(self.service)

    def test_ignored_lowercase_prefix_cannot_rotate_native_key(self):
        self.service["env"]["infinidysk__SAB__API_KEY"] = "ignored-fixture-key"
        self.assertIsNone(configured_sab_key(self.service))
        adopt_running_sab_key(self.manager, self.service, self.key)
        self.assertEqual(self.service["env"][SAB_ENV_NAME], self.key)

    def test_in_place_edit_during_native_read_is_not_overwritten(self):
        before = copy.deepcopy(self.service)
        self.service["env"]["TZ"] = "Etc/UTC"
        with self.assertRaises(NativeSabKeyError):
            adopt_running_sab_key(
                self.manager, self.service, self.key, expected_service=before
            )
        self.assertEqual(self.service["env"]["TZ"], "Etc/UTC")
        self.assertNotIn(SAB_ENV_NAME, self.service["env"])
        self.manager.save_config.assert_not_called()

    def test_whole_sab_override_is_not_silently_replaced(self):
        self.service["env"]["INFINIDYSK__sab"] = json.dumps({"api_key": self.key})
        original = copy.deepcopy(self.service)
        self.assertEqual(canonical_sab_environment(self.service), original["env"])
        with self.assertRaises(NativeSabKeyError):
            adopt_running_sab_key(self.manager, self.service, self.key)
        self.assertEqual(self.service, original)
        self.manager.save_config.assert_not_called()

    def test_missing_masked_or_invalid_reveal_is_not_adopted(self):
        for value in (None, {}, "", "\nfixture", " " * 4, "x" * 8193):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(NativeSabKeyError):
                    adopt_running_sab_key(self.manager, self.service, value)
        self.manager.save_config.assert_not_called()

    def test_fresh_explicit_key_does_not_require_matching_native_stored_value(self):
        for stored in (None, "", "fixture-older-stored-key"):
            with (
                self.subTest(stored=stored),
                tempfile.TemporaryDirectory() as temporary,
            ):
                path = Path(temporary) / "infinidysk.sqlite"
                with sqlite3.connect(path) as database:
                    database.execute("CREATE TABLE config(key TEXT,value TEXT)")
                    database.execute(
                        "INSERT INTO config VALUES('sab',?)",
                        (json.dumps({} if stored is None else {"api_key": stored}),),
                    )
                self.service["env"][SAB_ENV_NAME] = self.key
                adopt_prepared_sab_key(self.service, path, require_stored_match=False)
                self.assertEqual(self.service["env"][SAB_ENV_NAME], self.key)
                with self.assertRaises(NativeSabKeyError):
                    adopt_prepared_sab_key(
                        self.service, path, require_stored_match=True
                    )

    def test_prepared_candidate_key_is_copied_before_activation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "infinidysk.sqlite"
            with sqlite3.connect(path) as database:
                database.execute("CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT)")
                database.execute(
                    "INSERT INTO config VALUES('sab',?)",
                    (json.dumps({"api_key": self.key}),),
                )
            adopt_prepared_sab_key(self.service, path)
            self.assertEqual(self.service["env"][SAB_ENV_NAME], self.key)
            self.assertNotIn("FRONTEND_BACKEND_API_KEY", self.service["env"])
            with sqlite3.connect(path) as database:
                self.assertEqual(
                    json.loads(
                        database.execute("SELECT value FROM config").fetchone()[0]
                    )["api_key"],
                    self.key,
                )


if __name__ == "__main__":
    unittest.main()
