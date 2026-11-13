"""Retained SAB credentials survive migration without edits to client settings."""

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from utils.infinidysk_v2_credentials import (
    MigrationCredentialError,
    legacy_sab_environment,
    source_sab_key,
    validate_target_sab_override,
    verify_imported_sab_key,
)

KEY = "retained-fixture-download-key"


class CredentialContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.sqlite"
        self.target = self.root / "native.sqlite"
        with sqlite3.connect(self.source) as db:
            db.execute("CREATE TABLE ConfigItems(ConfigName TEXT,ConfigValue TEXT)")
            db.execute("INSERT INTO ConfigItems VALUES('api.key',?)", (KEY,))
        with sqlite3.connect(self.target) as db:
            db.execute("CREATE TABLE config(key TEXT,value TEXT)")
            db.execute(
                "INSERT INTO config VALUES('sab',?)",
                (json.dumps({"api_key": KEY, "categories": ["tv"]}),),
            )

    def source_value(self, value):
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE ConfigItems SET ConfigValue=?", (value,))

    def target_value(self, value):
        with sqlite3.connect(self.target) as db:
            db.execute("UPDATE config SET value=?", (value,))

    def proof(self, environment=None):
        return verify_imported_sab_key(self.target, KEY, environment or {})

    def test_stored_key_precedes_old_fallback_and_reads_preserve_both_databases(self):
        before = [p.read_bytes() for p in (self.source, self.target)]
        self.assertEqual(
            source_sab_key(self.source, {"FRONTEND_BACKEND_API_KEY": "stale-fallback"}),
            KEY,
        )
        proof = self.proof({"INFINIDYSK__AUTH__API_KEY": "private-controller-key"})
        self.assertEqual(proof, {"download_credentials_verified": True})
        self.assertNotIn(KEY, str(proof))
        self.assertEqual(before, [p.read_bytes() for p in (self.source, self.target)])

    def test_explicit_case_insensitive_source_overlay_is_forwarded_canonically(self):
        env = {"nzbdav_config__Api__Key": " overlay-fixture-key "}
        self.assertEqual(
            legacy_sab_environment(env),
            {"NZBDAV_CONFIG__API__KEY": "overlay-fixture-key"},
        )
        self.assertEqual(source_sab_key(self.source, env), "overlay-fixture-key")

    def test_blank_overlay_uses_old_fallback_instead_of_hidden_stored_key(self):
        env = {
            "NZBDAV_CONFIG__API__KEY": "",
            "FRONTEND_BACKEND_API_KEY": "fallback-key",
        }
        self.assertEqual(source_sab_key(self.source, env), "fallback-key")

    def test_missing_stored_key_uses_existing_fallback(self):
        self.source_value("")
        self.assertEqual(
            source_sab_key(self.source, {"FRONTEND_BACKEND_API_KEY": KEY}), KEY
        )

    def test_ambiguous_source_aliases_and_missing_credentials_fail_without_disclosure(
        self,
    ):
        env = {"NZBDAV_CONFIG__API__KEY": KEY, "nzbdav_config__api__key": "other-key"}
        for action in (
            lambda: source_sab_key(self.source, env),
            lambda: legacy_sab_environment(env),
        ):
            with self.assertRaises(MigrationCredentialError) as caught:
                action()
            self.assertNotIn(KEY, str(caught.exception))
            self.assertNotIn("other-key", str(caught.exception))
        self.source_value("")
        with self.assertRaisesRegex(
            MigrationCredentialError, "could not be established"
        ):
            source_sab_key(self.source, {})

    def test_duplicate_source_settings_are_not_silently_resolved(self):
        with sqlite3.connect(self.source) as db:
            db.execute("INSERT INTO ConfigItems VALUES('api.key','another-key')")
        with self.assertRaisesRegex(MigrationCredentialError, "ambiguous"):
            source_sab_key(self.source, {})

    def test_changed_native_key_blocks_without_modifying_either_database(self):
        self.target_value(json.dumps({"api_key": "unexpected-native-key"}))
        before = [p.read_bytes() for p in (self.source, self.target)]
        with self.assertRaisesRegex(
            MigrationCredentialError, "did not preserve"
        ) as caught:
            self.proof()
        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn("unexpected-native-key", str(caught.exception))
        self.assertEqual(before, [p.read_bytes() for p in (self.source, self.target)])

    def test_runtime_override_must_match_exactly_and_admin_key_is_separate(self):
        validate_target_sab_override(KEY, {"INFINIDYSK__SAB__API_KEY": KEY})
        self.proof({"INFINIDYSK__AUTH__API_KEY": "different-admin-key"})
        for override in ("changed", " " + KEY, KEY + " ", ""):
            with self.subTest(override_length=len(override)):
                with self.assertRaisesRegex(MigrationCredentialError, "override"):
                    self.proof({"INFINIDYSK__SAB__API_KEY": override})

    def test_parent_sab_overrides_cannot_shadow_a_verified_stored_or_leaf_key(self):
        for name in ("INFINIDYSK__SAB", "INFINIDYSK__Sab"):
            for value in (
                json.dumps({"api_key": "changed"}),
                json.dumps({"api_key": KEY}),
                "{}",
                "",
                "invalid",
            ):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(
                        MigrationCredentialError, "SAB section"
                    ) as caught:
                        self.proof({name: value, "INFINIDYSK__SAB__API_KEY": KEY})
                    self.assertNotIn(KEY, str(caught.exception))
                    self.assertNotIn("changed", str(caught.exception))

    def test_unrecognized_native_prefix_and_other_sab_fields_do_not_override_the_key(
        self,
    ):
        proof = self.proof(
            {
                "infinidysk__SAB": json.dumps({"api_key": "ignored"}),
                "INFINIDYSK__SAB__CATEGORIES": "[]",
            }
        )
        self.assertIs(proof["download_credentials_verified"], True)

    def test_native_stored_key_whitespace_is_significant(self):
        for key in (" " + KEY, KEY + " "):
            self.target_value(json.dumps({"api_key": key}))
            with self.assertRaisesRegex(MigrationCredentialError, "did not preserve"):
                self.proof()

    def test_native_alias_conflicts_are_blocked(self):
        with self.assertRaisesRegex(MigrationCredentialError, "Conflicting"):
            self.proof(
                {"INFINIDYSK__SAB__API_KEY": KEY, "INFINIDYSK__Sab__Api_Key": "changed"}
            )

    def test_malformed_missing_or_oversized_native_settings_are_refused(self):
        for value in (
            "not-json",
            "null",
            "[]",
            "{}",
            json.dumps({"api_key": ""}),
            "x" * 65537,
        ):
            self.target_value(value)
            with (
                self.subTest(size=len(value)),
                self.assertRaises(MigrationCredentialError),
            ):
                self.proof()

    def test_native_credential_must_have_string_shape_even_if_text_would_match(self):
        for value, expected in ((123, "123"), (True, "True"), (None, "None")):
            self.target_value(json.dumps({"api_key": value}))
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    MigrationCredentialError, "did not preserve"
                ):
                    verify_imported_sab_key(self.target, expected, {})

    def test_source_credential_shape_is_bounded_and_has_no_embedded_control_characters(
        self,
    ):
        for value in ("x" * 8193, "embedded\nline"):
            self.source_value(value)
            with (
                self.subTest(size=len(value)),
                self.assertRaises(MigrationCredentialError),
            ):
                source_sab_key(self.source, {})

    def test_symlink_or_multiple_link_database_is_not_accepted(self):
        alias = self.root / "linked.sqlite"
        alias.symlink_to(self.target)
        with self.assertRaisesRegex(MigrationCredentialError, "regular"):
            verify_imported_sab_key(alias, KEY, {})
        alias.unlink()
        os.link(self.target, alias)
        with self.assertRaisesRegex(MigrationCredentialError, "regular"):
            self.proof()

    def test_database_change_during_credential_read_is_refused(self):
        original = sqlite3.connect

        def changed(*args, **kwargs):
            connection = original(*args, **kwargs)
            current = self.source.stat()
            os.utime(
                self.source,
                ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000_000),
            )
            return connection

        with patch(
            "utils.infinidysk_v2_credentials.sqlite3.connect", side_effect=changed
        ):
            with self.assertRaisesRegex(MigrationCredentialError, "changed"):
                source_sab_key(self.source, {})


class ExecutionCredentialAcceptanceTests(unittest.TestCase):
    def setUp(self):
        from tests.test_infinidysk_v2_execution import ExecutionTests

        self.fixture = ExecutionTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_actual_executor_receipt_requires_preserved_download_key(self):
        f = self.fixture
        result = f.executor.cutover(f.plan, f.workspace, f.progress)
        self.assertIs(result["download_credentials_verified"], True)
        self.assertNotIn(KEY, str(result))

    def test_changed_key_refuses_acceptance_and_activation(self):
        f = self.fixture
        normal = f.candidate.wait_import.side_effect

        def changed(*args, **kwargs):
            result = normal(*args, **kwargs)
            with sqlite3.connect(
                Path(f.workspace["candidate_data"]) / "infinidysk.sqlite"
            ) as db:
                db.execute(
                    "UPDATE config SET value=?",
                    (json.dumps({"api_key": "unexpected"}),),
                )
            return result

        f.candidate.wait_import.side_effect = changed
        with self.assertRaisesRegex(MigrationCredentialError, "did not preserve"):
            f.executor.cutover(f.plan, f.workspace, f.progress)
        f.workspace["accept_import"].assert_not_called()
        f.workspace["activate_config"].assert_not_called()
        self.assertFalse(any(action == "start" for action, _ in f.handler.calls))


if __name__ == "__main__":
    unittest.main()
