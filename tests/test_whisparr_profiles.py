from copy import deepcopy
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from utils.whisparr_profiles import (
    plan_whisparr_profile,
    prepare_whisparr_profile,
    whisparr_profile_defaults,
)
from utils.whisparr_settings import WhisparrConfigurationError


class WhisparrProfileTests(unittest.TestCase):
    def _config(self, root, family="v2"):
        return {
            "whisparr_version": family,
            "config_dir": str(root),
            "config_file": str(root / "config.xml"),
            "log_file": str(root / "logs" / "whisparr.txt"),
            "install_dir": "/opt/whisparr/instances/default",
            "command": [
                "/opt/whisparr/instances/default/Whisparr/Whisparr",
                "--nobrowser",
                f"--data={root}",
            ],
            "release_version_enabled": False,
            "release_version": "latest",
            "pinned_version": "",
            "branch": "",
            "branch_enabled": False,
            "commit_sha": "",
            "repo_owner": "Whisparr",
            "repo_name": "Whisparr",
        }

    def _database(self, root, name, value):
        connection = sqlite3.connect(root / name)
        try:
            connection.execute("CREATE TABLE library (title TEXT)")
            connection.execute("INSERT INTO library VALUES (?)", (value,))
            connection.commit()
        finally:
            connection.close()

    def _rows(self, path):
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return connection.execute("SELECT title FROM library").fetchall()
        finally:
            connection.close()

    def _mixed(self, root, family="v3"):
        self._database(root, "whisparr2.db", "v2 library")
        self._database(root, "whisparr3.db", "eros library")
        (root / "config.xml").write_text(
            "<Config><ApiKey>example-key</ApiKey><Port>6969</Port></Config>"
        )
        original = self._config(root, family)
        return plan_whisparr_profile(original, original, instance_name="Default")

    def test_defaults_have_independent_fixed_family_objects(self):
        profiles = whisparr_profile_defaults()
        self.assertEqual({"v2", "v3"}, set(profiles))
        self.assertFalse(profiles["v2"]["initialized"])
        profiles["v2"]["command"].append("one")
        self.assertEqual([], profiles["v3"]["command"])
        self.assertEqual([], whisparr_profile_defaults()["v2"]["command"])

    def test_clean_existing_library_keeps_its_paths_and_no_files_are_created(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._database(root, "whisparr2.db", "existing")
            previous = self._config(root)
            original = deepcopy(previous)
            files_before = {path.name for path in root.iterdir()}
            planned = plan_whisparr_profile(previous, previous, instance_name="Default")
            self.assertEqual(str(root), planned["config_dir"])
            self.assertEqual(previous["command"], planned["command"])
            self.assertEqual(previous, planned)
            self.assertNotIn("whisparr_profiles", planned)
            self.assertEqual(files_before, {path.name for path in root.iterdir()})
            self.assertEqual(original, previous)

    def test_switch_creates_a_plan_only_and_return_restores_paths_and_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._database(root, "whisparr2.db", "existing")
            previous = self._config(root)
            previous["pinned_version"] = "2.2.0.231"
            previous["branch"] = "v2-develop"
            candidate = {**previous, "whisparr_version": "v3"}
            planned = plan_whisparr_profile(
                previous, candidate, instance_name="Default"
            )
            destination = root / "whisparr-profiles" / "v3"
            self.assertEqual(str(destination), planned["config_dir"])
            self.assertEqual(str(destination / "config.xml"), planned["config_file"])
            self.assertEqual(
                str(destination / "logs/whisparr.txt"), planned["log_file"]
            )
            self.assertEqual(
                "/opt/whisparr/instances/default/v3", planned["install_dir"]
            )
            self.assertEqual("", planned["pinned_version"])
            self.assertEqual("Whisparr-Eros", planned["repo_name"])
            self.assertEqual([], planned["command"])
            self.assertFalse(destination.exists())
            restored = plan_whisparr_profile(
                planned, {**planned, "whisparr_version": "v2"}, instance_name="Default"
            )
            for key in (
                "config_dir",
                "config_file",
                "command",
                "pinned_version",
                "branch",
                "install_dir",
            ):
                self.assertEqual(previous[key], restored[key], key)
            self.assertEqual(
                "2.2.0.231", restored["whisparr_profiles"]["v2"]["pinned_version"]
            )

    def test_saved_target_profile_and_explicit_new_release_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = self._config(root)
            profiles = whisparr_profile_defaults()
            profiles["v3"].update(
                {
                    "initialized": True,
                    "config_dir": str(root / "saved-eros"),
                    "config_file": str(root / "saved-eros/config.xml"),
                    "pinned_version": "3.6.4.1865",
                    "command": ["custom-Whisparr", f"--data={root / 'saved-eros'}"],
                    "repo_owner": "example",
                    "repo_name": "custom-eros",
                }
            )
            previous["whisparr_profiles"] = profiles
            candidate = {**previous, "whisparr_version": "v3"}
            restored = plan_whisparr_profile(
                previous, candidate, instance_name="Default"
            )
            self.assertEqual("3.6.4.1865", restored["pinned_version"])
            self.assertEqual("example", restored["repo_owner"])
            candidate["pinned_version"] = "3.6.5.1866"
            explicit = plan_whisparr_profile(
                previous, candidate, instance_name="Default"
            )
            self.assertEqual("3.6.5.1866", explicit["pinned_version"])

    def test_explicit_new_path_is_respected_instead_of_overridden(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = self._config(root)
            candidate = {**previous, "whisparr_version": "v3"}
            candidate["config_dir"] = str(root / "custom-eros")
            candidate["config_file"] = str(root / "custom-eros/custom.xml")
            planned = plan_whisparr_profile(
                previous, candidate, instance_name="Default"
            )
            self.assertEqual(candidate["config_dir"], planned["config_dir"])
            self.assertEqual(candidate["config_file"], planned["config_file"])
            self.assertFalse(Path(candidate["config_dir"]).exists())

    def test_same_family_updated_settings_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = self._config(root)
            previous["whisparr_profiles"] = whisparr_profile_defaults()
            previous["whisparr_profiles"]["v2"]["initialized"] = True
            candidate = {**previous, "pinned_version": "2.2.0.231"}
            planned = plan_whisparr_profile(
                previous, candidate, instance_name="Default"
            )
            self.assertEqual(
                "2.2.0.231", planned["whisparr_profiles"]["v2"]["pinned_version"]
            )

    def test_mixed_root_requires_explicit_family_and_plans_both_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._database(root, "whisparr2.db", "v2 library")
            self._database(root, "whisparr3.db", "eros library")
            previous = self._config(root, "")
            with self.assertRaisesRegex(WhisparrConfigurationError, "explicitly"):
                plan_whisparr_profile(previous, previous, instance_name="Default")
            planned = plan_whisparr_profile(
                previous,
                {**previous, "whisparr_version": "v3"},
                instance_name="Default",
            )
            for family in ("v2", "v3"):
                profile = planned["whisparr_profiles"][family]
                self.assertEqual(str(root), profile["source_config_dir"])
                self.assertEqual(
                    str(root / "whisparr-profiles" / family), profile["config_dir"]
                )
            self.assertFalse((root / "whisparr-profiles").exists())

    def test_ambiguous_legacy_and_modern_v2_cannot_be_auto_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._database(root, "whisparr.db", "old")
            self._database(root, "whisparr2.db", "new")
            previous = self._config(root)
            with self.assertRaisesRegex(WhisparrConfigurationError, "Both legacy"):
                plan_whisparr_profile(previous, previous)

    def test_postgres_switch_is_refused_and_inactive_sqlite_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._database(root, "whisparr3.db", "inactive")
            previous = {**self._config(root), "postgres_enabled": True}
            planned = plan_whisparr_profile(previous, previous)
            self.assertEqual(str(root), planned["config_dir"])
            with self.assertRaisesRegex(WhisparrConfigurationError, "PostgreSQL"):
                plan_whisparr_profile(previous, {**previous, "whisparr_version": "v3"})

    def test_profile_directories_cannot_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = self._config(root)
            profiles = whisparr_profile_defaults()
            profiles["v3"].update(
                {
                    "initialized": True,
                    "config_dir": str(root),
                    "config_file": str(root / "config.xml"),
                }
            )
            previous["whisparr_profiles"] = profiles
            with self.assertRaisesRegex(WhisparrConfigurationError, "separate"):
                plan_whisparr_profile(previous, previous)

    def test_invalid_instance_slug_and_foreign_release_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = self._config(root)
            candidate = {**previous, "whisparr_version": "v3"}
            with self.assertRaises(WhisparrConfigurationError):
                plan_whisparr_profile(previous, candidate, instance_name="../escape")
            candidate["pinned_version"] = "2.2.0.231"
            with self.assertRaisesRegex(WhisparrConfigurationError, "not a published"):
                plan_whisparr_profile(previous, candidate, instance_name="Default")

    def test_mixed_root_seed_uses_matching_database_preserves_originals_and_native_config(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = self._mixed(root)
            original = deepcopy(planned)
            before = {
                path.name: path.read_bytes()
                for path in root.iterdir()
                if path.is_file()
            }
            prepared = prepare_whisparr_profile(planned)
            target = Path(prepared["config_dir"])
            self.assertEqual([("eros library",)], self._rows(target / "whisparr3.db"))
            self.assertFalse((target / "whisparr2.db").exists())
            self.assertEqual(before["config.xml"], (target / "config.xml").read_bytes())
            self.assertEqual(
                "", prepared["whisparr_profiles"]["v3"]["source_config_dir"]
            )
            self.assertEqual(
                str(root), prepared["whisparr_profiles"]["v2"]["source_config_dir"]
            )
            self.assertEqual(original, planned)
            for name, content in before.items():
                self.assertEqual(content, (root / name).read_bytes(), name)
            self.assertEqual(0o600, (target / "whisparr3.db").stat().st_mode & 0o777)
            self.assertEqual(0o600, (target / "config.xml").stat().st_mode & 0o777)
            self.assertFalse(
                any(
                    path.name.startswith(".dumb-whisparr-seed-")
                    for path in target.parent.iterdir()
                )
            )

    def test_each_family_can_be_seeded_once_and_repeated_preparation_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = self._mixed(root)
            first = prepare_whisparr_profile(planned)
            snapshot = Path(first["config_dir"]) / "whisparr3.db"
            original = snapshot.read_bytes()
            retried = prepare_whisparr_profile(planned)
            self.assertEqual(
                "", retried["whisparr_profiles"]["v3"]["source_config_dir"]
            )
            self.assertEqual(original, snapshot.read_bytes())
            returned = plan_whisparr_profile(
                first, {**first, "whisparr_version": "v2"}, instance_name="Default"
            )
            prepared = prepare_whisparr_profile(returned)
            self.assertEqual(
                [("v2 library",)],
                self._rows(Path(prepared["config_dir"]) / "whisparr2.db"),
            )
            self.assertEqual(original, snapshot.read_bytes())

    def test_live_wal_content_is_included_without_raw_sidecar_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._database(root, "whisparr2.db", "v2 library")
            connection = sqlite3.connect(root / "whisparr3.db")
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA wal_autocheckpoint=0")
                connection.execute("CREATE TABLE library (title TEXT)")
                connection.execute("INSERT INTO library VALUES ('committed WAL row')")
                connection.commit()
                wal = root / "whisparr3.db-wal"
                self.assertTrue(wal.exists())
                database_before = (root / "whisparr3.db").read_bytes()
                wal_before = wal.read_bytes()
                previous = self._config(root, "v3")
                planned = plan_whisparr_profile(
                    previous, previous, instance_name="Default"
                )
                prepared = prepare_whisparr_profile(planned)
                target = Path(prepared["config_dir"])
                self.assertEqual(
                    [("committed WAL row",)], self._rows(target / "whisparr3.db")
                )
                self.assertFalse((target / "whisparr3.db-wal").exists())
                self.assertFalse((target / "whisparr3.db-shm").exists())
                self.assertEqual(database_before, (root / "whisparr3.db").read_bytes())
                self.assertEqual(wal_before, wal.read_bytes())
            finally:
                connection.close()

    def test_recovery_only_family_is_not_replaced_by_empty_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._database(root, "whisparr2.db", "v2 library")
            (root / "whisparr3.db-wal").write_bytes(b"recoverable original")
            previous = self._config(root, "v3")
            with self.assertRaisesRegex(WhisparrConfigurationError, "recovery files"):
                plan_whisparr_profile(previous, previous, instance_name="Default")
            self.assertEqual(
                b"recoverable original", (root / "whisparr3.db-wal").read_bytes()
            )
            self.assertFalse((root / "whisparr-profiles").exists())

    def test_missing_seed_source_does_not_create_a_fresh_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = self._mixed(root)
            (root / "whisparr3.db").unlink()
            with self.assertRaisesRegex(WhisparrConfigurationError, "missing"):
                prepare_whisparr_profile(planned)
            self.assertFalse(Path(planned["config_dir"]).exists())
            self.assertFalse((root / "whisparr3.db").exists())

    def test_populated_destination_and_symlink_destination_are_never_overwritten(self):
        for symlink in (False, True):
            with (
                self.subTest(symlink=symlink),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                planned = self._mixed(root)
                destination = Path(planned["config_dir"])
                destination.parent.mkdir(parents=True)
                real = root / "existing" if symlink else destination
                real.mkdir()
                (real / "unrelated").write_bytes(b"preserve")
                if symlink:
                    destination.symlink_to(real, target_is_directory=True)
                with self.assertRaises(WhisparrConfigurationError):
                    prepare_whisparr_profile(planned)
                self.assertEqual(b"preserve", (real / "unrelated").read_bytes())
                self.assertFalse((real / "whisparr3.db").exists())

    def test_invalid_sqlite_never_publishes_or_removes_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = self._mixed(root)
            (root / "whisparr3.db").write_bytes(b"not a SQLite library")
            with self.assertRaisesRegex(WhisparrConfigurationError, "snapshot"):
                prepare_whisparr_profile(planned)
            self.assertEqual(
                b"not a SQLite library", (root / "whisparr3.db").read_bytes()
            )
            self.assertFalse(Path(planned["config_dir"]).exists())
            self.assertFalse(
                any(Path(planned["config_dir"]).parent.glob(".dumb-whisparr-seed-*"))
            )

    def test_publish_failure_rolls_back_only_created_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = self._mixed(root)
            original = os.link
            calls = 0

            def fail_second(source, destination, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("fixture publication failure")
                return original(source, destination, **kwargs)

            with patch("utils.whisparr_profiles.os.link", side_effect=fail_second):
                with self.assertRaises(WhisparrConfigurationError):
                    prepare_whisparr_profile(planned)
            self.assertFalse(Path(planned["config_dir"]).exists())
            self.assertEqual([("eros library",)], self._rows(root / "whisparr3.db"))
            self.assertFalse(
                any(Path(planned["config_dir"]).parent.glob(".dumb-whisparr-seed-*"))
            )

    def test_restore_artifacts_and_symlink_source_are_refused(self):
        for symlink in (False, True):
            with (
                self.subTest(symlink=symlink),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                planned = self._mixed(root)
                if symlink:
                    (root / "whisparr3.db").rename(root / "original.db")
                    (root / "whisparr3.db").symlink_to(root / "original.db")
                else:
                    (root / "whisparr3.restore").write_bytes(b"recoverable")
                with self.assertRaises(WhisparrConfigurationError):
                    prepare_whisparr_profile(planned)
                self.assertFalse(Path(planned["config_dir"]).exists())

    def test_snapshot_opens_original_readonly_and_closes_all_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = self._mixed(root)
            original_connect = sqlite3.connect
            opened = []

            def capture(*args, **kwargs):
                connection = original_connect(*args, **kwargs)
                opened.append(connection)
                if kwargs.get("uri"):
                    self.assertIn("?mode=ro", args[0])
                    with self.assertRaises(sqlite3.OperationalError):
                        connection.execute("DELETE FROM library")
                return connection

            with patch("utils.whisparr_profiles.sqlite3.connect", side_effect=capture):
                prepare_whisparr_profile(planned)
            self.assertEqual(2, len(opened))
            for connection in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
            self.assertEqual([("eros library",)], self._rows(root / "whisparr3.db"))

    def test_timeout_removes_snapshot_workspace_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = self._mixed(root)
            with patch("utils.whisparr_profiles.time.monotonic", side_effect=[0, 11]):
                with self.assertRaisesRegex(WhisparrConfigurationError, "timed out"):
                    prepare_whisparr_profile(planned)
            self.assertFalse(Path(planned["config_dir"]).exists())
            self.assertEqual([("eros library",)], self._rows(root / "whisparr3.db"))
            self.assertFalse(
                any(Path(planned["config_dir"]).parent.glob(".dumb-whisparr-seed-*"))
            )

    def test_destination_added_during_snapshot_is_preserved(self):
        from utils.whisparr_profiles import _snapshot_sqlite

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = self._mixed(root)
            destination = Path(planned["config_dir"])

            def add_existing(source, target):
                _snapshot_sqlite(source, target)
                destination.mkdir()
                (destination / "unrelated").write_bytes(b"preserve this")

            with patch(
                "utils.whisparr_profiles._snapshot_sqlite", side_effect=add_existing
            ):
                with self.assertRaisesRegex(WhisparrConfigurationError, "changed"):
                    prepare_whisparr_profile(planned)
            self.assertEqual(b"preserve this", (destination / "unrelated").read_bytes())
            self.assertFalse((destination / "whisparr3.db").exists())

    def test_renamed_destination_cannot_redirect_publication_or_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = self._mixed(root)
            destination = Path(planned["config_dir"])
            replacement = root / "unrelated"
            replacement.mkdir()
            (replacement / "preserve").write_bytes(b"other data")
            relocated = destination.with_name("relocated")
            original_link = os.link
            changed = False

            def move_directory(source, target, **kwargs):
                nonlocal changed
                result = original_link(source, target, **kwargs)
                if not changed:
                    changed = True
                    destination.rename(relocated)
                    destination.symlink_to(replacement, target_is_directory=True)
                return result

            with patch("utils.whisparr_profiles.os.link", side_effect=move_directory):
                with self.assertRaisesRegex(WhisparrConfigurationError, "changed"):
                    prepare_whisparr_profile(planned)
            self.assertEqual(
                ["preserve"], [entry.name for entry in replacement.iterdir()]
            )
            self.assertEqual(b"other data", (replacement / "preserve").read_bytes())
            self.assertEqual([], list(relocated.iterdir()))
            self.assertEqual([("eros library",)], self._rows(root / "whisparr3.db"))

    def test_tampered_marker_is_not_trusted_or_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = self._mixed(root)
            prepared = prepare_whisparr_profile(planned)
            destination = Path(prepared["config_dir"])
            marker = destination / ".dumb-whisparr-profile.json"
            marker.write_text('{"family": "v2"}')
            before = (destination / "whisparr3.db").read_bytes()
            with self.assertRaisesRegex(WhisparrConfigurationError, "unrelated"):
                prepare_whisparr_profile(planned)
            self.assertEqual(before, (destination / "whisparr3.db").read_bytes())
            self.assertEqual('{"family": "v2"}', marker.read_text())

    def test_missing_database_during_ro_open_is_not_recreated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = self._mixed(root)
            original_connect = sqlite3.connect

            def disappear(*args, **kwargs):
                if kwargs.get("uri"):
                    (root / "whisparr3.db").unlink()
                return original_connect(*args, **kwargs)

            with patch(
                "utils.whisparr_profiles.sqlite3.connect", side_effect=disappear
            ):
                with self.assertRaisesRegex(WhisparrConfigurationError, "snapshot"):
                    prepare_whisparr_profile(planned)
            self.assertFalse((root / "whisparr3.db").exists())
            self.assertFalse(Path(planned["config_dir"]).exists())

    def test_unrelated_save_preserves_empty_selector_and_default_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._database(root, "whisparr3.db", "existing")
            previous = self._config(root, "")
            previous["whisparr_profiles"] = whisparr_profile_defaults()
            candidate = {**previous, "port": 7000}
            planned = plan_whisparr_profile(
                previous, candidate, instance_name="Default"
            )
            self.assertEqual(candidate, planned)
            self.assertEqual("", planned["whisparr_version"])

    def test_single_family_recovery_only_also_refuses_empty_start(self):
        for name in ("whisparr3.db-wal", "whisparr2.db-shm", "whisparr3.restore"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / name).write_bytes(b"recoverable")
                previous = self._config(root, "v2" if "whisparr2" in name else "v3")
                with self.assertRaisesRegex(
                    WhisparrConfigurationError, "recovery files"
                ):
                    plan_whisparr_profile(previous, previous)
                self.assertEqual(b"recoverable", (root / name).read_bytes())

    def test_bad_seed_destination_config_path_is_rejected_before_any_files_created(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = self._config(root, "v3")
            self._database(root, "whisparr2.db", "old")
            self._database(root, "whisparr3.db", "eros")
            candidate = {**previous, "config_file": str(root / "elsewhere/config.xml")}
            with self.assertRaisesRegex(WhisparrConfigurationError, "directly"):
                plan_whisparr_profile(previous, candidate, instance_name="Default")
            self.assertFalse((root / "whisparr-profiles").exists())

    def test_mixed_custom_native_source_is_not_silently_seeded_from_wrong_xml(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = self._config(root, "v3")
            previous["config_file"] = str(root / "custom.xml")
            self._database(root, "whisparr2.db", "old")
            self._database(root, "whisparr3.db", "eros")
            with self.assertRaisesRegex(WhisparrConfigurationError, "native"):
                plan_whisparr_profile(previous, previous, instance_name="Default")
            self.assertFalse((root / "whisparr-profiles").exists())

    def test_stale_dashboard_profile_map_cannot_reset_saved_paths_or_seed_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = self._mixed(root)
            prepared = prepare_whisparr_profile(planned)
            stale = deepcopy(prepared)
            stale["whisparr_profiles"] = deepcopy(planned["whisparr_profiles"])
            persisted = plan_whisparr_profile(prepared, stale, instance_name="Default")
            self.assertEqual(
                "", persisted["whisparr_profiles"]["v3"]["source_config_dir"]
            )
            stale["whisparr_profiles"] = whisparr_profile_defaults()
            persisted = plan_whisparr_profile(prepared, stale, instance_name="Default")
            self.assertEqual(
                prepared["whisparr_profiles"], persisted["whisparr_profiles"]
            )

    def test_incoming_profile_map_cannot_override_saved_inactive_family_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = self._config(root)
            previous["pinned_version"] = "2.2.0.231"
            planned = plan_whisparr_profile(
                previous,
                {**previous, "whisparr_version": "v3"},
                instance_name="Default",
            )
            candidate = {**planned, "whisparr_version": "v2"}
            candidate["whisparr_profiles"] = whisparr_profile_defaults()
            restored = plan_whisparr_profile(
                planned, candidate, instance_name="Default"
            )
            self.assertEqual(previous["config_dir"], restored["config_dir"])
            self.assertEqual("2.2.0.231", restored["pinned_version"])

    def test_incoming_initialized_profiles_cannot_forge_managed_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = self._config(root)
            candidate = deepcopy(previous)
            candidate["whisparr_profiles"] = whisparr_profile_defaults()
            candidate["whisparr_profiles"]["v3"].update(
                {
                    "initialized": True,
                    "config_dir": str(root / "unrelated"),
                    "source_config_dir": str(root),
                }
            )
            planned = plan_whisparr_profile(
                previous, candidate, instance_name="Default"
            )
            self.assertEqual(whisparr_profile_defaults(), planned["whisparr_profiles"])

    def test_mixed_legacy_profile_keeps_matching_original_runtime_for_adoption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._database(root, "whisparr.db", "legacy v2")
            self._database(root, "whisparr3.db", "eros")
            native_runtime = root / "original-runtime"
            native_runtime.mkdir()
            binary = native_runtime / "Whisparr"
            binary.write_bytes(b"not executed")
            (native_runtime / "Whisparr.Core.dll").write_bytes(
                b"Whisparr.Common, Version=2.0.0.2151"
            )
            previous = self._config(root, "v3")
            previous["command"] = [str(binary), "--nobrowser", f"--data={root}"]
            planned = plan_whisparr_profile(previous, previous, instance_name="Default")
            self.assertEqual(
                previous["command"], planned["whisparr_profiles"]["v2"]["command"]
            )
            self.assertEqual([], planned["command"])
            prepared = prepare_whisparr_profile(planned)
            returned = plan_whisparr_profile(
                prepared,
                {**prepared, "whisparr_version": "v2"},
                instance_name="Default",
            )
            self.assertEqual(str(binary), returned["command"][0])
            self.assertEqual(f"--data={returned['config_dir']}", returned["command"][2])
            self.assertEqual([("legacy v2",)], self._rows(root / "whisparr.db"))

    def test_fresh_detect_instance_can_select_v3_postgres_without_sqlite_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = self._config(root, "")
            previous["postgres_enabled"] = False
            previous["whisparr_profiles"] = whisparr_profile_defaults()
            candidate = {**previous, "whisparr_version": "v3", "postgres_enabled": True}
            planned = plan_whisparr_profile(
                previous, candidate, instance_name="Default"
            )
            self.assertEqual(candidate, planned)
            self.assertFalse(
                any(
                    profile["initialized"]
                    for profile in planned["whisparr_profiles"].values()
                )
            )
            self.assertFalse((root / "whisparr-profiles").exists())

    def test_new_v3_postgres_instance_from_empty_previous_has_no_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = {**self._config(root, "v3"), "postgres_enabled": True}
            planned = plan_whisparr_profile({}, candidate, instance_name="Default")
            self.assertEqual(candidate, planned)

    def test_pg_detected_existing_runtime_family_cannot_be_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "Whisparr"
            binary.write_bytes(b"not executed")
            (root / "Whisparr.Core.dll").write_bytes(
                b"Whisparr.Common, Version=2.2.0.231"
            )
            previous = self._config(root, "")
            previous["command"] = [str(binary)]
            previous["postgres_enabled"] = True
            with self.assertRaisesRegex(WhisparrConfigurationError, "PostgreSQL"):
                plan_whisparr_profile(previous, {**previous, "whisparr_version": "v3"})

    def test_saved_target_recovery_only_cannot_silently_start_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = self._config(root)
            profiles = whisparr_profile_defaults()
            destination = root / "saved-eros"
            destination.mkdir()
            (destination / "whisparr3.db-wal").write_bytes(b"recoverable")
            profiles["v3"].update(
                {
                    "initialized": True,
                    "config_dir": str(destination),
                    "config_file": str(destination / "config.xml"),
                }
            )
            previous["whisparr_profiles"] = profiles
            with self.assertRaisesRegex(WhisparrConfigurationError, "recovery files"):
                plan_whisparr_profile(previous, {**previous, "whisparr_version": "v3"})
            self.assertFalse((destination / "whisparr3.db").exists())
            self.assertEqual(
                b"recoverable", (destination / "whisparr3.db-wal").read_bytes()
            )

    def test_established_postgres_with_unknown_family_refuses_new_explicit_selection(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = self._config(root, "")
            previous["postgres_enabled"] = True
            for family in ("v2", "v3"):
                with self.subTest(family=family):
                    with self.assertRaisesRegex(
                        WhisparrConfigurationError, "cannot be verified"
                    ):
                        plan_whisparr_profile(
                            previous, {**previous, "whisparr_version": family}
                        )
            unrelated = {**previous, "port": 6970}
            self.assertEqual(unrelated, plan_whisparr_profile(previous, unrelated))

    def test_invalid_native_xml_is_rejected_before_any_seed_publication(self):
        for content in (b"<Config><ApiKey>private-test-value", b"<Other />"):
            with (
                self.subTest(content=content),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                planned = self._mixed(root)
                (root / "config.xml").write_bytes(content)
                with self.assertRaisesRegex(
                    WhisparrConfigurationError, "invalid or unsafe"
                ) as raised:
                    prepare_whisparr_profile(planned)
                self.assertNotIn("private-test-value", str(raised.exception))
                self.assertEqual(content, (root / "config.xml").read_bytes())
                self.assertFalse(Path(planned["config_dir"]).exists())
                self.assertFalse(
                    any(
                        Path(planned["config_dir"]).parent.glob(".dumb-whisparr-seed-*")
                    )
                )

    def test_native_xml_entities_are_rejected_without_disclosing_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = self._mixed(root)
            content = b'<!DOCTYPE Config [<!ENTITY example "private-entity-value">]><Config><ApiKey>&example;</ApiKey></Config>'
            (root / "config.xml").write_bytes(content)
            with self.assertRaisesRegex(
                WhisparrConfigurationError, "invalid or unsafe"
            ) as raised:
                prepare_whisparr_profile(planned)
            self.assertNotIn("private-entity-value", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)
            self.assertEqual(content, (root / "config.xml").read_bytes())
            self.assertFalse(Path(planned["config_dir"]).exists())

    def test_mixed_detect_root_retains_fixed_v2_pin_when_selecting_fresh_v3_profile(
        self,
    ):
        for release_enabled in (False, True):
            with (
                self.subTest(release_enabled=release_enabled),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                self._database(root, "whisparr2.db", "old")
                self._database(root, "whisparr3.db", "eros")
                previous = self._config(root, "")
                previous["release_version_enabled"] = release_enabled
                previous["release_version" if release_enabled else "pinned_version"] = (
                    "2.2.0.231"
                )
                planned = plan_whisparr_profile(
                    previous,
                    {**previous, "whisparr_version": "v3"},
                    instance_name="Default",
                )
                self.assertEqual("", planned["pinned_version"])
                self.assertFalse(planned["release_version_enabled"])
                self.assertEqual("latest", planned["release_version"])
                saved_v2 = planned["whisparr_profiles"]["v2"]
                self.assertEqual(
                    "2.2.0.231",
                    saved_v2[
                        "release_version" if release_enabled else "pinned_version"
                    ],
                )
                self.assertEqual(release_enabled, saved_v2["release_version_enabled"])
                self.assertFalse(Path(planned["config_dir"]).exists())

    def test_seed_ownership_failure_precedes_publication_and_preserves_originals(self):
        for failure_call in (1, 4):
            with (
                self.subTest(failure_call=failure_call),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                planned = self._mixed(root)
                originals = {
                    path.name: (
                        path.read_bytes(),
                        path.stat().st_uid,
                        path.stat().st_gid,
                    )
                    for path in root.iterdir()
                    if path.is_file()
                }
                calls = 0

                def refuse_owner(descriptor, user_id, group_id):
                    nonlocal calls
                    calls += 1
                    if calls == failure_call:
                        raise PermissionError("private ownership failure detail")

                with patch(
                    "utils.whisparr_profiles.os.fchown", side_effect=refuse_owner
                ):
                    with self.assertRaisesRegex(
                        WhisparrConfigurationError, "Cannot safely prepare"
                    ) as raised:
                        prepare_whisparr_profile(
                            planned,
                            owner_ids=(os.geteuid() + 1, os.getegid() + 1),
                        )
                self.assertEqual(failure_call, calls)
                self.assertNotIn("private ownership", str(raised.exception))
                self.assertFalse(Path(planned["config_dir"]).exists())
                self.assertFalse(
                    any(
                        Path(planned["config_dir"]).parent.glob(".dumb-whisparr-seed-*")
                    )
                )
                for name, before in originals.items():
                    path = root / name
                    self.assertEqual(
                        before,
                        (path.read_bytes(), path.stat().st_uid, path.stat().st_gid),
                    )

    def test_completed_seed_retry_repairs_only_its_published_entry_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = self._mixed(root)
            first = prepare_whisparr_profile(planned)
            destination = Path(first["config_dir"])
            before = {
                path.name: path.read_bytes()
                for path in destination.iterdir()
                if path.is_file()
            }
            changed = []

            def capture_owner(descriptor, user_id, group_id):
                changed.append(Path(os.readlink(f"/proc/self/fd/{descriptor}")))

            with patch("utils.whisparr_profiles.os.fchown", side_effect=capture_owner):
                retried = prepare_whisparr_profile(
                    planned,
                    owner_ids=(os.geteuid() + 1, os.getegid() + 1),
                )
            self.assertEqual(
                {
                    destination,
                    destination / "whisparr3.db",
                    destination / "config.xml",
                    destination / ".dumb-whisparr-profile.json",
                },
                set(changed),
            )
            self.assertEqual(
                "", retried["whisparr_profiles"]["v3"]["source_config_dir"]
            )
            self.assertEqual(
                before,
                {
                    path.name: path.read_bytes()
                    for path in destination.iterdir()
                    if path.is_file()
                },
            )

    def test_completed_seed_retry_ownership_failure_retains_library_and_pending_state(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = self._mixed(root)
            prepare_whisparr_profile(planned)
            destination = Path(planned["config_dir"])
            before = {
                path.name: path.read_bytes()
                for path in destination.iterdir()
                if path.is_file()
            }
            with patch(
                "utils.whisparr_profiles.os.fchown",
                side_effect=PermissionError("private failure"),
            ):
                with self.assertRaisesRegex(
                    WhisparrConfigurationError, "Cannot safely prepare"
                ):
                    prepare_whisparr_profile(
                        planned,
                        owner_ids=(os.geteuid() + 1, os.getegid() + 1),
                    )
            self.assertEqual(
                str(root), planned["whisparr_profiles"]["v3"]["source_config_dir"]
            )
            self.assertEqual(
                before,
                {
                    path.name: path.read_bytes()
                    for path in destination.iterdir()
                    if path.is_file()
                },
            )

    def test_completed_seed_cannot_follow_native_config_symlink_for_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = self._mixed(root)
            prepare_whisparr_profile(planned)
            destination = Path(planned["config_dir"])
            (destination / "config.xml").unlink()
            (destination / "config.xml").symlink_to(root / "config.xml")
            original = (root / "config.xml").stat()
            with patch("utils.whisparr_profiles.os.fchown") as change_owner:
                with self.assertRaisesRegex(
                    WhisparrConfigurationError, "Cannot safely prepare"
                ):
                    prepare_whisparr_profile(
                        planned,
                        owner_ids=(os.geteuid() + 1, os.getegid() + 1),
                    )
            change_owner.assert_not_called()
            after = (root / "config.xml").stat()
            self.assertEqual(
                (original.st_uid, original.st_gid, original.st_ino),
                (after.st_uid, after.st_gid, after.st_ino),
            )

    def test_new_profile_parent_remains_traversable_with_restrictive_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planned = self._mixed(root)
            original = root.stat()
            previous_umask = os.umask(0o077)
            try:
                prepare_whisparr_profile(
                    planned, owner_ids=(os.geteuid(), os.getegid())
                )
            finally:
                os.umask(previous_umask)
            destination = Path(planned["config_dir"])
            self.assertEqual(0o755, destination.parent.stat().st_mode & 0o777)
            self.assertEqual(0o700, destination.stat().st_mode & 0o777)
            after = root.stat()
            self.assertEqual(
                (original.st_uid, original.st_gid, original.st_mode),
                (after.st_uid, after.st_gid, after.st_mode),
            )


if __name__ == "__main__":
    unittest.main()
