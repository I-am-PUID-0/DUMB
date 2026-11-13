"""Explicit installation persistence paths and bounded blocker diagnostics."""

from contextlib import contextmanager
import io
import os
from pathlib import Path
import tempfile
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from utils.backup_storage import BackupStorage
from utils.installation_backup_archive import (
    FORMAT,
    SCHEMA,
    InstallationArchiveError,
    source_rows,
    write_archive,
)
from utils.installation_backup_inventory import (
    _backup_profile_overlap_message,
    _effective_persistence,
    _plex_dbrepair_directories,
    _unclassified_message,
    collect_roots,
)


class InstallationPersistenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "managed-data"
        self.data.mkdir()
        self.config_root = self.root / "config"
        self.config_root.mkdir()
        self.config_path = self.config_root / "dumb.json"
        self.config_path.write_text("{}")
        self.plex = self.root / "plex"
        self.plex.mkdir()
        self.install = self.data / "dbrepair"
        self.work = self.root / "unowned-working-directory"
        self.databases = self.data / "repair-databases"
        for path in (self.install, self.work, self.databases):
            path.mkdir()
        self.config = {
            "data_root": str(self.data),
            "plex": {
                "enabled": False,
                "process_name": "Fixture Plex",
                "config_dir": str(self.plex),
                "dbrepair": {
                    "enabled": False,
                    "install_dir": str(self.install),
                    "work_dir": str(self.work),
                    "databases_path": str(self.databases),
                    "db_path": str(self.databases / "library.db"),
                },
            },
        }

    def collect(self, storage=None):
        with patch.dict(
            os.environ,
            {"DUMB_AUTH_CONFIG_PATH": str(self.config_root / "absent-auth.json")},
        ):
            return collect_roots(
                self.config,
                self.config_path,
                self.config_root / "installation-backups",
                storage,
            )

    def test_default_dbrepair_path_matches_literal_native_fallback(self):
        directories, _databases = _effective_persistence(
            "plex", {"config_dir": "/fixture/plex", "enabled": False}, "/custom-data"
        )
        self.assertIn("/data/dbrepair", directories)
        self.assertNotIn("/custom-data/dbrepair", directories)
        self.assertIn("/plex/Plex Media Server/Plug-in Support/Databases", directories)
        self.assertEqual(
            _plex_dbrepair_directories({"dbrepair": {"work_dir": ""}}), directories
        )

    def test_disabled_plex_and_dbrepair_include_custom_native_directories_and_contents(
        self,
    ):
        script = self.install / "DBRepair.sh"
        script.write_text("fixture managed repair tool\n")
        (self.work / "retained.state").write_text("fixture repair state\n")
        (self.databases / "library.db").write_text("fixture database bytes\n")
        roots, exclusions = self.collect()
        selected = {row["path"]: row for row in roots}
        for path in (self.install, self.databases):
            self.assertIn(str(path), selected)
            self.assertIn("plex:effective_appdata", selected[str(path)]["roles"])
        with self.rows(selected[str(self.install)], exclusions) as rows:
            self.assertTrue(any(path == script for _row, path, _stamp in rows))
        self.assertNotIn(str(self.work), selected)
        self.assertTrue(script.is_file())
        self.assertFalse((self.config_root / "installation-backups").exists())

    @contextmanager
    def rows(self, root, exclusions):
        iterator = source_rows(root, exclusions)
        try:
            yield iterator
        finally:
            iterator.close()

    def test_work_directory_does_not_claim_arbitrary_media_or_system_trees(self):
        expected = [str(self.install), str(self.databases)]
        for work in ("", None, "/", "/mnt/debrid", str(self.work)):
            with self.subTest(work=work):
                service = {
                    "dbrepair": {
                        "install_dir": str(self.install),
                        "work_dir": work,
                        "databases_path": str(self.databases),
                    }
                }
                self.assertEqual(_plex_dbrepair_directories(service), expected)

    def test_malformed_nested_settings_and_native_selectors_fail_clearly(self):
        self.assertEqual(
            _plex_dbrepair_directories({"dbrepair": None}),
            _plex_dbrepair_directories({}),
        )
        for settings in (False, 3, [], "custom"):
            with (
                self.subTest(settings=settings),
                self.assertRaisesRegex(
                    InstallationArchiveError, "DBRepair settings must be an object"
                ),
            ):
                _plex_dbrepair_directories({"dbrepair": settings})
        for field in ("install_dir", "databases_path"):
            for value in (
                None,
                False,
                3,
                [],
                {},
                "relative/path",
                "/direct/../escape",
                "/invalid\x00path",
                "",
            ):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaisesRegex(
                        InstallationArchiveError, "DBRepair " + field
                    ),
                ):
                    _plex_dbrepair_directories({"dbrepair": {field: value}})

    def test_protected_dbrepair_root_is_still_refused(self):
        self.config["plex"]["dbrepair"]["install_dir"] = "/"
        with self.assertRaisesRegex(
            InstallationArchiveError, "protected system or media root"
        ):
            self.collect()

    def test_unknown_empty_directory_is_named_and_remains_blocked(self):
        unknown = self.data / "unknown-empty"
        unknown.mkdir()
        with self.assertRaises(InstallationArchiveError) as raised:
            self.collect()
        message = str(raised.exception)
        self.assertIn(str(unknown), message)
        self.assertIn("directory", message)
        self.assertNotIn("assign", message)
        self.assertTrue(unknown.is_dir())

    def test_unknown_media_and_regular_files_are_not_silently_included(self):
        unknown = self.data / "unclassified-media.mkv"
        unknown.write_bytes(b"fixture payload")
        before = unknown.read_bytes()
        with self.assertRaises(InstallationArchiveError) as raised:
            self.collect()
        self.assertIn(str(unknown), str(raised.exception))
        self.assertIn("(file)", str(raised.exception))
        self.assertEqual(unknown.read_bytes(), before)

    def archive_members(self, roots, exclusions):
        scratch = self.root / "archive-scratch"
        scratch.mkdir()
        output = io.BytesIO()
        write_archive(
            output,
            {"format": FORMAT, "schema_version": SCHEMA, "roots": roots},
            roots,
            scratch,
            exclusions,
        )
        output.seek(0)
        with tarfile.open(fileobj=output, mode="r:") as archive:
            return archive.getmembers()

    def test_sibling_managed_alias_is_archived_literally_and_payload_only_once(self):
        payload = self.install / "DBRepair.sh"
        payload.write_bytes(b"reserved managed tool")
        media = self.work / "unowned-media.mkv"
        media.write_bytes(b"reserved media payload is not copied")
        (self.install / "external-media").symlink_to(
            self.work, target_is_directory=True
        )
        alias = self.data / "old-repair-location"
        alias.symlink_to(self.install, target_is_directory=True)

        roots, exclusions = self.collect()
        selected = {row["path"]: row for row in roots}
        self.assertTrue(selected[str(alias)]["link_only"])
        self.assertIn("managed_alias", selected[str(alias)]["roles"])
        with self.rows(selected[str(alias)], exclusions) as rows:
            literal = list(rows)
        self.assertEqual(len(literal), 1)
        self.assertEqual(literal[0][0]["link"], str(self.install))

        members = self.archive_members(roots, exclusions)
        alias_member = next(
            row for row in members if row.name == "roots/" + selected[str(alias)]["id"]
        )
        self.assertTrue(alias_member.issym())
        self.assertEqual(alias_member.linkname, str(self.install))
        self.assertEqual(sum(row.name.endswith("/DBRepair.sh") for row in members), 1)
        self.assertFalse(
            any(row.name.endswith("/unowned-media.mkv") for row in members)
        )
        self.assertTrue(
            any(row.name.endswith("/external-media") and row.issym() for row in members)
        )

    def test_sibling_alias_into_managed_descendant_is_literal_without_duplicate_payload(
        self,
    ):
        nested = self.install / "managed-child"
        nested.mkdir()
        (nested / "managed-state").write_bytes(b"reserved managed state")
        alias = self.data / "old-managed-child"
        alias.symlink_to(nested, target_is_directory=True)
        roots, exclusions = self.collect()
        selected = {row["path"]: row for row in roots}
        self.assertTrue(selected[str(alias)]["link_only"])
        members = self.archive_members(roots, exclusions)
        self.assertEqual(sum(row.name.endswith("/managed-state") for row in members), 1)
        self.assertTrue(
            any(row.issym() and row.linkname == str(nested) for row in members)
        )

    def test_alias_already_inside_included_directory_has_no_duplicate_root(self):
        alias = self.data / "old-repair-location"
        alias.symlink_to(self.install, target_is_directory=True)
        self.config["fixture"] = {
            "process_name": "Fixture",
            "config_dir": str(self.data),
        }
        roots, exclusions = self.collect()
        self.assertNotIn(str(alias), {row["path"] for row in roots})
        members = self.archive_members(roots, exclusions)
        self.assertEqual(
            sum(row.issym() and row.linkname == str(self.install) for row in members), 1
        )

    def test_new_literal_alias_respects_existing_root_bound(self):
        roots, _exclusions = self.collect()
        (self.data / "old-repair-location").symlink_to(
            self.install, target_is_directory=True
        )
        with patch(
            "utils.installation_backup_inventory.MAX_INSTALLATION_ROOTS", len(roots)
        ):
            with self.assertRaisesRegex(
                InstallationArchiveError, "root inventory exceeds"
            ):
                self.collect()

    def test_sibling_alias_to_unmanaged_media_remains_unclassified(self):
        media = self.work / "unowned-media.mkv"
        media.write_bytes(b"reserved retained media")
        alias = self.data / "unknown-media-alias"
        alias.symlink_to(self.work, target_is_directory=True)
        with self.assertRaisesRegex(InstallationArchiveError, "Unclassified state"):
            self.collect()
        self.assertEqual(media.read_bytes(), b"reserved retained media")

    def test_unused_or_disabled_profile_cannot_hide_managed_persistence(self):
        payload = self.install / "DBRepair.sh"
        payload.write_bytes(b"reserved managed tool")
        for destination in (self.install, self.data, self.config_root, self.root):
            for enabled in (False, True):
                with self.subTest(destination=destination, enabled=enabled):
                    self.config["backup_storage"] = {
                        "profiles": {
                            "unused": {"path": str(destination), "enabled": enabled}
                        }
                    }
                    with self.assertRaises(InstallationArchiveError) as caught:
                        self.collect()
                    self.assertIn(str(destination), str(caught.exception))
                    self.assertIn(
                        "would exclude managed persistence", str(caught.exception)
                    )
                    self.assertEqual(payload.read_bytes(), b"reserved managed tool")

    def test_registered_profile_cannot_hide_persistence_or_require_new_storage_probe(
        self,
    ):
        manager = SimpleNamespace(
            config=self.config, file_path=str(self.config_path), save_config=Mock()
        )
        storage = BackupStorage(manager, state_root=self.root / "storage-control")
        result = storage.save_profile(
            {"id": "fixture", "path": str(self.install), "enabled": True}
        )
        self.assertTrue(result["profile"]["registered"])
        with patch.object(
            storage, "_inspect", side_effect=AssertionError("No storage probe")
        ):
            with self.assertRaisesRegex(
                InstallationArchiveError, "would exclude managed persistence"
            ):
                self.collect(storage)

    def test_profile_cannot_hide_missing_declared_or_physical_alias_target(self):
        missing = self.root / "missing-application"
        self.config["fixture"] = {
            "enabled": False,
            "process_name": "Fixture",
            "config_dir": str(missing / "state"),
        }
        self.config["backup_storage"] = {"profiles": {"unused": {"path": str(missing)}}}
        with self.assertRaisesRegex(
            InstallationArchiveError, "would exclude managed persistence"
        ):
            self.collect()
        self.assertFalse(missing.exists())

        physical = self.root / "external-managed"
        physical.mkdir()
        alias = self.root / "published-managed"
        alias.symlink_to(physical, target_is_directory=True)
        self.config["fixture"]["config_dir"] = str(alias)
        self.config["backup_storage"]["profiles"]["unused"]["path"] = str(physical)
        with self.assertRaises(InstallationArchiveError) as caught:
            self.collect()
        self.assertIn(str(physical), str(caught.exception))

    def test_disjoint_and_dedicated_child_profile_preserve_state_and_exclude_archives(
        self,
    ):
        payload = self.install / "DBRepair.sh"
        payload.write_bytes(b"reserved managed tool")
        archived = self.install / "closed-archives"
        archived.mkdir()
        (archived / "old-backup.tar").write_bytes(b"reserved old archive")
        unavailable = self.root / "unavailable-mounted-destination"
        self.config["backup_storage"] = {
            "profiles": {
                "child": {"path": str(archived)},
                "disjoint": {"path": str(unavailable), "enabled": False},
            }
        }
        roots, exclusions = self.collect()
        self.assertIn(str(archived), exclusions)
        self.assertIn(str(unavailable), exclusions)
        self.assertIn(str(self.config_root / "installation-backups"), exclusions)
        self.assertFalse(unavailable.exists())
        members = self.archive_members(roots, exclusions)
        self.assertEqual(sum(row.name.endswith("/DBRepair.sh") for row in members), 1)
        self.assertFalse(any("closed-archives" in row.name for row in members))
        self.assertTrue((archived / "old-backup.tar").is_file())

    def test_profile_collision_diagnostic_is_bounded_without_config_values(self):
        message = _backup_profile_overlap_message(
            "/backup/" + "nested/" * 1000, '/data/managed\n"app"'
        )
        self.assertLess(len(message), 1300)
        self.assertIn("[path truncated]", message)
        self.assertIn("\\n", message)
        self.assertNotIn("\n", message)
        self.assertIn("dedicated archive directory", message)

    def test_path_diagnostics_escape_controls_and_fit_existing_ui_bound(self):
        message = _unclassified_message('/data/unknown\n"item"', "directory")
        self.assertIn("\\n", message)
        self.assertNotIn("\n", message)
        self.assertIn('\\"item\\"', message)
        message = _unclassified_message("/data/" + "nested/" * 1000, "directory")
        self.assertLess(len(message), 1600)
        self.assertIn("[path truncated]", message)
        self.assertIn("backup is blocked", message)


if __name__ == "__main__":
    unittest.main()
