"""Explicit installation persistence paths and bounded blocker diagnostics."""

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from utils.installation_backup_archive import InstallationArchiveError, source_rows
from utils.installation_backup_inventory import (
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

    def collect(self):
        with patch.dict(
            os.environ,
            {"DUMB_AUTH_CONFIG_PATH": str(self.config_root / "absent-auth.json")},
        ):
            return collect_roots(
                self.config,
                self.config_path,
                self.config_root / "installation-backups",
                None,
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
