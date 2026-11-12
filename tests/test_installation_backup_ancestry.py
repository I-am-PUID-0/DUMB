"""Real previews accept literal source roots without weakening store ancestry."""

from copy import deepcopy
import os
from pathlib import Path
from unittest.mock import patch
import unittest

from tests import test_installation_backup as fixtures
from utils.installation_backup import InstallationBackupManager


class InstallationBackupAncestryTests(unittest.TestCase):
    setUp = fixtures.ManagerTests.setUp
    join_worker = fixtures.ManagerTests.join_worker
    start_backup = fixtures.ManagerTests.start_backup

    def literal_roots(self):
        settings = self.root / "custom-settings.json"
        settings.write_text('{"value":"before"}')
        alias = self.root / "application-alias"
        alias.symlink_to(self.application, target_is_directory=True)
        library = self.root / "library-links"
        library.mkdir()
        (library / "episode.strm").write_text("https://example.invalid/stream")
        (library / "episode.srt").write_text("A retained subtitle")
        (library / "episode.mkv").symlink_to("/reserved-media/episode.mkv")
        self.config.config["fixture"].update(
            settings_file=str(settings),
            install_dir=str(alias),
            symlink_backup_roots=[str(library)],
        )
        self.config.save_config()
        return settings, alias, library

    def test_real_preview_covers_file_alias_and_library_namespace_without_quiescence(
        self,
    ):
        settings, alias, library = self.literal_roots()
        configuration = deepcopy(self.config.config)
        with patch(
            "utils.installation_backup_archive._content_hash",
            side_effect=AssertionError("Preview must not hash application payloads"),
        ):
            preview = self.manager.preview({"destination_id": "fixture"})
        self.assertTrue(preview["ready"], preview["blockers"])
        roots = {row["path"]: row for row in preview["inventory"]["roots"]}
        self.assertEqual(roots[str(settings)]["kind"], "file")
        self.assertEqual(roots[str(alias)]["kind"], "symlink")
        self.assertIn("link_state", roots[str(library)]["roles"])
        self.assertTrue(preview["capacity"]["complete"])
        self.assertTrue(preview["capacity"]["verification"]["ledger_verified"])
        self.assertEqual(self.handler.events, [])
        self.assertEqual(self.config.config, configuration)
        self.assertEqual(settings.read_text(), '{"value":"before"}')
        self.assertEqual(os.readlink(alias), str(self.application))
        self.assertEqual(
            list(self.manager.workspace_root.glob("preview/.installation-manifest-*")),
            [],
        )

    def test_default_workspace_uses_the_runtime_temp_parent(self):
        self.literal_roots()
        runtime = self.root / "runtime-temp"
        runtime.mkdir(mode=0o700)
        with patch(
            "utils.installation_backup.tempfile.gettempdir", return_value=str(runtime)
        ):
            self.manager = InstallationBackupManager(
                self.config, self.handler, storage=self.storage
            )
        with patch("utils.installation_backup._MANAGER", self.manager):
            preview = self.manager.preview({"destination_id": "fixture"})
        self.assertTrue(preview["ready"], preview["blockers"])
        self.assertEqual(
            self.manager.workspace_root, runtime / "dumb-installation-work"
        )
        self.assertEqual(self.handler.events, [])
        self.assertEqual(
            list(self.manager.workspace_root.glob("preview/.installation-manifest-*")),
            [],
        )

    def test_literal_roots_survive_backup_restore_and_exact_rollback(self):
        settings, alias, _library = self.literal_roots()
        backup = self.start_backup()
        self.assertEqual(backup["status"], "completed", backup)
        alternate = self.root / "alternate-application"
        alternate.mkdir()
        alias.unlink()
        alias.symlink_to(alternate, target_is_directory=True)
        settings.write_text('{"value":"after"}')
        before_preview = list(self.handler.events)
        preview = self.manager.restore_preview(
            {"reference": backup["result"]["archive"]["reference"]}
        )
        self.assertTrue(preview["ready"], preview["blockers"])
        self.assertTrue(preview["backup"]["verified"])
        self.assertEqual(self.handler.events, before_preview)
        accepted = self.manager.restore_start(
            {
                "preview_token": preview["preview_token"],
                "confirmation": preview["confirmation_text"],
                "acknowledgements": {
                    "downtime": True,
                    "replace_state": True,
                    "external_state": True,
                },
            }
        )
        self.join_worker()
        restored = self.manager.get_job(accepted["job"]["job_id"])["job"]
        self.assertEqual(restored["status"], "completed", restored)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(os.readlink(alias), str(self.application))
        self.assertEqual(settings.read_text(), '{"value":"before"}')
        self.manager.rollback(
            restored["job_id"], {"confirmation": restored["rollback_confirmation_text"]}
        )
        self.assertEqual(os.readlink(alias), str(alternate))
        self.assertEqual(settings.read_text(), '{"value":"after"}')

    def test_source_leaf_opt_in_does_not_allow_a_symlink_archive_store(self):
        self.literal_roots()
        unsafe = self.root / "linked-artifact-store"
        unsafe.symlink_to(self.root / "archives", target_is_directory=True)
        destination = self.storage.bound_identity("fixture")
        destination["artifact_root"] = str(unsafe)
        with patch.object(self.storage, "bound_identity", return_value=destination):
            preview = self.manager.preview({"destination_id": "fixture"})
        self.assertFalse(preview["ready"])
        self.assertTrue(preview["blockers"])
        self.assertEqual(self.handler.events, [])
        self.assertTrue(unsafe.is_symlink())


if __name__ == "__main__":
    unittest.main()
