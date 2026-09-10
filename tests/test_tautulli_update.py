import io
import zipfile
from unittest.mock import Mock, patch

import tempfile
import threading
import unittest
from pathlib import Path

from utils.transactional_install import RuntimeRollbackSnapshot
from utils import setup
from utils.auto_update import Update
from utils.tautulli_update import tautulli_persistent_excludes


class TautulliUpdateTests(unittest.TestCase):
    def test_configured_release_reinstalls_even_with_auto_update_enabled(self):
        for install_error in (None, "release download failed"):
            with self.subTest(install_error=install_error):
                updater = Update.__new__(Update)
                updater.process_handler = Mock(
                    process_names=["Tautulli"],
                    setup_tracker=["Tautulli"],
                    setup_tracker_lock=threading.Lock(),
                )
                updater.stop_process = Mock()
                updater.start_process = Mock(return_value=(Mock(), None))
                config = {
                    "auto_update": True,
                    "release_version_enabled": True,
                    "release_version": "v2.18.0",
                }
                with (
                    patch(
                        "utils.auto_update.setup_release_version",
                        return_value=(install_error is None, install_error),
                    ) as install,
                    patch("utils.auto_update.setup_project") as default_setup,
                    patch(
                        "utils.auto_update.configure_project", return_value=(True, None)
                    ) as configure,
                ):
                    success, message = updater._install_configured_target(
                        "Tautulli", config, "tautulli", None, "release"
                    )
                install.assert_called_once_with(
                    updater.process_handler, config, "Tautulli", "tautulli"
                )
                default_setup.assert_not_called()
                updater.stop_process.assert_called_once_with("Tautulli")
                self.assertTrue(config["auto_update"])
                self.assertTrue(config["release_version_enabled"])
                if install_error:
                    self.assertFalse(success)
                    self.assertIn(install_error, message)
                    configure.assert_not_called()
                    updater.start_process.assert_not_called()
                else:
                    self.assertTrue(success, message)
                    configure.assert_called_once_with(
                        updater.process_handler, "Tautulli"
                    )
                    updater.start_process.assert_called_once()

    def test_install_and_rollback_allow_templates_but_preserve_all_runtime_entries(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            (data / "interfaces" / "default").mkdir(parents=True)
            (data / "interfaces" / "default" / "settings.html").write_text("old")
            for name in ("config.ini", "tautulli.db", "custom-state.json"):
                (data / name).write_text("persistent")
            config = {"repo_name": "Tautulli", "exclude_dirs": [str(data)]}
            for exclusions in (
                setup._update_persistent_excludes(
                    config, str(root), service_key="tautulli"
                ),
                Update._rollback_persistent_paths("tautulli", config, str(root)),
            ):
                self.assertNotIn(str(data), exclusions)
                self.assertNotIn(str(data / "interfaces"), exclusions)
                for name in ("config.ini", "tautulli.db", "custom-state.json"):
                    self.assertIn(str(data / name), exclusions)

    def test_explicit_template_exclusions_and_symlinked_data_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specific = root / "data" / "interfaces" / "custom"
            self.assertIn(str(specific), tautulli_persistent_excludes(root, [specific]))
            (root / "state").mkdir()
            (root / "data").symlink_to(root / "state", target_is_directory=True)
            self.assertEqual(
                tautulli_persistent_excludes(root, ["data"]), [str(root / "data")]
            )

    def test_archive_update_and_rollback_keep_templates_in_sync_without_losing_state(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tautulli"
            template = root / "data/interfaces/default/settings.html"
            template.parent.mkdir(parents=True)
            template.write_text("old-template")
            source = root / "Tautulli.py"
            source.write_text("old-source")
            state = root / "data/tautulli.db"
            state.write_bytes(b"persistent-state")
            config = {"repo_name": "Tautulli", "exclude_dirs": ["data"]}
            snapshot = RuntimeRollbackSnapshot(
                str(root),
                "Tautulli",
                Update._rollback_persistent_paths("tautulli", config, str(root)),
            )
            self.assertTrue(snapshot.capture())
            try:
                archive = io.BytesIO()
                with zipfile.ZipFile(archive, "w") as zipped:
                    zipped.writestr("app/Tautulli.py", "new-source")
                    zipped.writestr(
                        "app/data/interfaces/default/settings.html", "new-template"
                    )
                    zipped.writestr("app/data/tautulli.db", "must-not-overwrite")
                response = Mock(
                    status_code=200,
                    headers={"Content-Disposition": "attachment; filename=app.zip"},
                    content=archive.getvalue(),
                )
                exclusions = setup._update_persistent_excludes(
                    config, str(root), service_key="tautulli"
                )
                with patch.object(
                    setup.downloader, "fetch_with_retries", return_value=response
                ):
                    success, error = setup.downloader.download_and_extract(
                        "https://example.test/tautulli.zip",
                        str(root),
                        zip_folder_name="app",
                        exclude_dirs=exclusions,
                    )
                self.assertTrue(success, error)
                self.assertEqual(template.read_text(), "new-template")
                self.assertEqual(source.read_text(), "new-source")
                self.assertEqual(state.read_bytes(), b"persistent-state")
                self.assertTrue(snapshot.rollback())
                self.assertEqual(template.read_text(), "old-template")
                self.assertEqual(source.read_text(), "old-source")
                self.assertEqual(state.read_bytes(), b"persistent-state")
            finally:
                snapshot.commit()
