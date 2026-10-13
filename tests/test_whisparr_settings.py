import tempfile
import unittest
from pathlib import Path

from utils.whisparr_settings import (
    WhisparrConfigurationError,
    normalize_whisparr_release_version,
    read_whisparr_version,
    resolve_whisparr_version,
    validate_whisparr_runtime,
    whisparr_branch,
    whisparr_database_file,
    whisparr_release_selector,
    whisparr_repository,
)


class WhisparrSettingsTests(unittest.TestCase):
    def _runtime(self, root, version):
        binary = root / "Whisparr" / "Whisparr"
        binary.parent.mkdir(exist_ok=True)
        binary.write_bytes(b"must never execute")
        binary.with_name("Whisparr.Core.dll").write_bytes(
            b"assembly metadata\0Whisparr.Common, Version=" + version.encode() + b"\0"
        )
        return str(binary)

    def test_fresh_detection_defaults_to_v2_without_mutating_config(self):
        config = {"whisparr_version": ""}
        self.assertEqual("v2", resolve_whisparr_version(config))
        self.assertEqual({"whisparr_version": ""}, config)

    def test_database_identity_preserves_legacy_and_current_installs(self):
        for name, family in (
            ("whisparr.db", "v2"),
            ("whisparr2.db", "v2"),
            ("whisparr3.db", "v3"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                database = root / name
                database.write_bytes(b"existing library")
                config = {"config_dir": directory, "branch": "nightly"}
                self.assertEqual(family, resolve_whisparr_version(config))
                self.assertEqual(name, whisparr_database_file(config))
                self.assertEqual(b"existing library", database.read_bytes())

    def test_explicit_wrong_family_and_mixed_databases_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "whisparr3.db").write_bytes(b"eros library")
            config = {"config_dir": directory, "whisparr_version": "v2"}
            with self.assertRaisesRegex(WhisparrConfigurationError, "existing v3"):
                resolve_whisparr_version(config)
            (root / "whisparr.db").write_bytes(b"legacy library")
            config["whisparr_version"] = "v3"
            with self.assertRaisesRegex(WhisparrConfigurationError, "Both Whisparr"):
                resolve_whisparr_version(config)
            self.assertEqual(b"eros library", (root / "whisparr3.db").read_bytes())
            self.assertEqual(b"legacy library", (root / "whisparr.db").read_bytes())

    def test_orphaned_recovery_files_and_dangling_db_links_protect_family(self):
        for name in ("whisparr3.db-wal", "whisparr3.db-shm", "whisparr3.db-journal"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / name).write_bytes(b"recoverable")
                with self.assertRaisesRegex(WhisparrConfigurationError, "existing v3"):
                    resolve_whisparr_version(
                        {"config_dir": directory, "whisparr_version": "v2"}
                    )
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "whisparr3.db").symlink_to("missing-data")
            self.assertEqual("v3", resolve_whisparr_version({"config_dir": directory}))

    def test_old_eros_repository_branch_and_native_config_are_detected(self):
        for config in (
            {"repo_owner": "Whisparr", "repo_name": "Whisparr-eros"},
            {"branch": "eros", "branch_enabled": True},
            {"branch": "eros-develop"},
        ):
            with self.subTest(config=config):
                self.assertEqual("v3", resolve_whisparr_version(config))
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "config.xml").write_text(
                "<Config><Branch>eros</Branch></Config>"
            )
            self.assertEqual("v3", resolve_whisparr_version({"config_dir": directory}))

    def test_actual_runtime_metadata_preserves_existing_family(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = self._runtime(Path(directory), "3.6.4.1865")
            config = {
                "command": [binary],
                "repo_owner": "Whisparr",
                "repo_name": "Whisparr",
            }
            self.assertEqual("3.6.4.1865", read_whisparr_version(binary))
            self.assertEqual("v3", resolve_whisparr_version(config))
            self.assertEqual(("Whisparr", "Whisparr-Eros"), whisparr_repository(config))

    def test_explicit_version_selects_matching_repository_and_channel(self):
        self.assertEqual(
            ("Whisparr", "Whisparr-Eros"),
            whisparr_repository({"whisparr_version": "v3"}),
        )
        self.assertEqual(
            "eros", whisparr_branch({"whisparr_version": "v3", "branch": "nightly"})
        )
        self.assertEqual(
            ("Whisparr", "Whisparr"),
            whisparr_repository(
                {
                    "whisparr_version": "v2",
                    "repo_owner": "Whisparr",
                    "repo_name": "Whisparr-Eros",
                }
            ),
        )
        self.assertEqual(
            "v2", whisparr_branch({"whisparr_version": "v2", "branch": "eros"})
        )
        self.assertEqual(
            ("example", "custom-arr"),
            whisparr_repository(
                {
                    "whisparr_version": "v3",
                    "repo_owner": "example",
                    "repo_name": "custom-arr",
                }
            ),
        )

    def test_stable_development_and_fixed_release_selection(self):
        self.assertEqual(
            "latest", whisparr_release_selector({"whisparr_version": "v3"})
        )
        self.assertEqual(
            "prerelease", whisparr_release_selector({"branch": "eros-develop"})
        )
        self.assertEqual("prerelease", whisparr_release_selector({"branch": "nightly"}))
        self.assertEqual(
            "v3.6.4-release.1865",
            whisparr_release_selector(
                {
                    "whisparr_version": "v3",
                    "release_version_enabled": True,
                    "release_version": "v3.6.4-release.1865",
                }
            ),
        )
        self.assertEqual(
            "3.6.4.1865",
            whisparr_release_selector(
                {"whisparr_version": "v3", "pinned_version": "3.6.4.1865"}
            ),
        )

    def test_explicit_release_precedes_inactive_pin_and_legacy_nightly_maps_to_prerelease(
        self,
    ):
        config = {
            "whisparr_version": "v3",
            "pinned_version": "2.0.0.2151",
            "release_version_enabled": True,
            "release_version": "v3.6.4-release.1865",
        }
        self.assertEqual("v3.6.4-release.1865", whisparr_release_selector(config))
        config["release_version"] = "nightly"
        self.assertEqual("prerelease", whisparr_release_selector(config))

    def test_wrong_major_pins_branch_as_tag_and_commit_builds_are_refused(self):
        for config in (
            {"whisparr_version": "v3", "pinned_version": "2.0.0.2151"},
            {
                "whisparr_version": "v2",
                "release_version_enabled": True,
                "release_version": "v3.6.4-release.1865",
            },
            {
                "whisparr_version": "v3",
                "release_version_enabled": True,
                "release_version": "eros",
            },
            {"whisparr_version": "v3", "commit_sha": "a" * 40},
            {"whisparr_version": "v4"},
        ):
            with (
                self.subTest(config=config),
                self.assertRaises(WhisparrConfigurationError),
            ):
                resolve_whisparr_version(config)

    def test_postgres_ignores_inactive_sqlite_but_blocks_known_major_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "whisparr.db").write_bytes(b"inactive old SQLite")
            binary = self._runtime(root, "3.6.4.1865")
            config = {
                "config_dir": directory,
                "postgres_enabled": True,
                "command": [binary],
                "whisparr_version": "v3",
            }
            self.assertEqual("v3", resolve_whisparr_version(config))
            config["whisparr_version"] = "v2"
            with self.assertRaisesRegex(WhisparrConfigurationError, "PostgreSQL"):
                resolve_whisparr_version(config)

    def test_start_guard_requires_readable_matching_runtime_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = self._runtime(root, "2.2.0.231")
            with self.assertRaisesRegex(WhisparrConfigurationError, "does not match"):
                validate_whisparr_runtime({"whisparr_version": "v3"}, binary)
            validate_whisparr_runtime({"whisparr_version": "v2"}, binary)
            Path(binary).with_name("Whisparr.Core.dll").unlink()
            with self.assertRaisesRegex(WhisparrConfigurationError, "Cannot verify"):
                validate_whisparr_runtime({"whisparr_version": "v2"}, binary)

    def test_modern_v2_cannot_silently_initialize_a_legacy_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "whisparr.db"
            database.write_bytes(b"existing library")
            config = {"config_dir": directory, "whisparr_version": "v2"}
            binary = self._runtime(root, "2.0.0.2151")
            validate_whisparr_runtime(config, binary)
            self._runtime(root, "2.2.0.231")
            with self.assertRaisesRegex(
                WhisparrConfigurationError, "does not automatically"
            ):
                validate_whisparr_runtime(config, binary)
            self.assertEqual(b"existing library", database.read_bytes())
            self.assertFalse((root / "whisparr2.db").exists())

    def test_old_v2_and_ambiguous_v2_filenames_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "whisparr2.db").write_bytes(b"current library")
            config = {"config_dir": directory, "whisparr_version": "v2"}
            binary = self._runtime(root, "2.0.0.2151")
            with self.assertRaisesRegex(WhisparrConfigurationError, "older Whisparr"):
                validate_whisparr_runtime(config, binary)
            (root / "whisparr.db-wal").write_bytes(b"legacy recovery")
            with self.assertRaisesRegex(WhisparrConfigurationError, "Both legacy"):
                resolve_whisparr_version(config)

    def test_published_tags_match_numeric_assembly_versions(self):
        for tag, version in (
            ("v2.2.0-release.231", "2.2.0.231"),
            ("v3.6.4-release.1865", "3.6.4.1865"),
            ("v3.6.5-develop.1866", "3.6.5.1866"),
            ("3.6.2.1727", "3.6.2.1727"),
        ):
            with self.subTest(tag=tag):
                self.assertEqual(version, normalize_whisparr_release_version(tag))
        self.assertIsNone(normalize_whisparr_release_version("eros"))
