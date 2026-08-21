import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils import setup


class CliDebridSetupTests(unittest.TestCase):
    @staticmethod
    def _write_source(
        root: Path,
        *,
        imported_name: str = "get_media_items_presence_batch",
        exported_names: tuple[str, ...] = ("get_media_items_presence_batch",),
        marker: str = "current",
    ) -> None:
        (root / "database").mkdir(parents=True, exist_ok=True)
        (root / "routes").mkdir(parents=True, exist_ok=True)
        (root / "main.py").write_text("APP = 'cli-debrid'\n", encoding="utf-8")
        (root / "database" / "__init__.py").write_text("", encoding="utf-8")
        definitions = "\n".join(
            f"def {name}():\n    return {marker!r}\n" for name in exported_names
        )
        (root / "database" / "database_reading.py").write_text(
            definitions,
            encoding="utf-8",
        )
        (root / "routes" / "scraper_routes.py").write_text(
            f"from database.database_reading import {imported_name}\n",
            encoding="utf-8",
        )

    @staticmethod
    def _config(root: Path, **overrides) -> dict:
        config = {
            "enabled": True,
            "process_name": "CLI Debrid Custom",
            "repo_owner": "example",
            "repo_name": "cli-fork",
            "release_version_enabled": True,
            "release_version": "v0.7.48",
            "commit_sha": "",
            "branch_enabled": False,
            "branch": "main",
            "auto_update": False,
            "clear_on_update": True,
            "exclude_dirs": [str(root / "data")],
            "platforms": ["python"],
            "config_dir": str(root),
            "env": {},
        }
        config.update(overrides)
        return config

    @staticmethod
    def _process_handler() -> Mock:
        process_handler = Mock()
        process_handler.setup_tracker = set()
        process_handler.setup_tracker_lock = threading.Lock()
        process_handler.logger = Mock()
        return process_handler

    def test_source_validator_accepts_matching_imports_and_reexports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_source(root)
            (root / "database" / "core.py").write_text(
                "def get_db_connection():\n    return None\n", encoding="utf-8"
            )
            with (root / "database" / "database_reading.py").open(
                "a", encoding="utf-8"
            ) as database_reading:
                database_reading.write("from .core import get_db_connection\n")
            (root / "routes" / "database_routes.py").write_text(
                "from database.database_reading import get_db_connection\n",
                encoding="utf-8",
            )

            valid, error = setup._validate_cli_debrid_source(str(root))

        self.assertTrue(valid, error)

    def test_source_validator_rejects_exact_mixed_revision_import(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_source(
                root, exported_names=("get_media_item_presence_overall",)
            )

            valid, error = setup._validate_cli_debrid_source(str(root))

        self.assertFalse(valid)
        self.assertIn("routes/scraper_routes.py", error)
        self.assertIn("get_media_items_presence_batch", error)

    def test_source_validator_rejects_python_syntax_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_source(root)
            (root / "routes" / "broken.py").write_text(
                "def broken(:\n", encoding="utf-8"
            )

            valid, error = setup._validate_cli_debrid_source(str(root))

        self.assertFalse(valid)
        self.assertIn("invalid Python syntax", error)
        self.assertIn("routes/broken.py", error)

    def test_cli_inference_preserves_data_but_not_database_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_source(root)
            (root / "data").mkdir()
            (root / "data" / "state.json").write_text("state", encoding="utf-8")

            exclusions = setup._update_persistent_excludes(
                self._config(root),
                str(root),
                service_key="cli_debrid",
            )

        self.assertIn(str(root / "data"), exclusions)
        self.assertNotIn(str(root / "database"), exclusions)

    def test_explicit_cli_database_exclusion_remains_authoritative(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_source(root)
            config = self._config(root, exclude_dirs=["database"])

            exclusions = setup._update_persistent_excludes(
                config,
                str(root),
                service_key="cli_debrid",
            )

        self.assertIn(str(root / "database"), exclusions)

    def test_non_cli_database_directory_remains_conventionally_protected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "database").mkdir()

            exclusions = setup._update_persistent_excludes(
                {},
                str(root),
                service_key="another_service",
            )

        self.assertIn(str(root / "database"), exclusions)

    def test_release_install_replaces_database_source_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_source(
                root,
                imported_name="old_reader",
                exported_names=("old_reader",),
                marker="old",
            )
            (root / "data").mkdir()
            state = root / "data" / "state.json"
            state.write_text("persistent", encoding="utf-8")
            config = self._config(root)

            def download_release(**kwargs):
                self.assertIn(str(root / "data"), kwargs["exclude_dirs"])
                self.assertNotIn(str(root / "database"), kwargs["exclude_dirs"])
                self.assertIs(
                    kwargs["staging_validator"], setup._validate_cli_debrid_source
                )
                self._write_source(root, marker="new")
                return True, None

            with (
                patch.object(
                    setup.downloader,
                    "download_release_version",
                    side_effect=download_release,
                ),
                patch.object(setup, "additional_setup", return_value=(True, None)),
            ):
                success, error = setup.setup_release_version(
                    Mock(), config, "CLI Debrid Custom", "cli_debrid"
                )

            self.assertTrue(success, error)
            self.assertEqual("persistent", state.read_text(encoding="utf-8"))
            database_source = (root / "database" / "database_reading.py").read_text(
                encoding="utf-8"
            )
            self.assertIn("get_media_items_presence_batch", database_source)
            self.assertNotIn("old_reader", database_source)

    def test_branch_install_replaces_database_source_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_source(
                root,
                imported_name="old_reader",
                exported_names=("old_reader",),
                marker="old",
            )
            (root / "data").mkdir()
            state = root / "data" / "state.json"
            state.write_text("persistent", encoding="utf-8")
            config = self._config(
                root,
                release_version_enabled=False,
                branch_enabled=True,
            )

            def download_branch(_url, _target, **kwargs):
                self.assertIn(str(root / "data"), kwargs["exclude_dirs"])
                self.assertNotIn(str(root / "database"), kwargs["exclude_dirs"])
                self.assertIs(
                    kwargs["staging_validator"], setup._validate_cli_debrid_source
                )
                self._write_source(root, marker="new")
                return True, None

            with (
                patch.object(
                    setup.downloader,
                    "get_branch",
                    return_value=("https://example.test/source.zip", "example-cli*"),
                ),
                patch.object(
                    setup.downloader,
                    "get_ref_commit_sha",
                    return_value=(None, "offline"),
                ),
                patch.object(
                    setup.downloader,
                    "download_and_extract",
                    side_effect=download_branch,
                ),
                patch.object(setup, "additional_setup", return_value=(True, None)),
            ):
                success, error = setup.setup_branch_version(
                    Mock(), config, "CLI Debrid Custom", "cli_debrid"
                )

            self.assertTrue(success, error)
            self.assertEqual("persistent", state.read_text(encoding="utf-8"))
            database_source = (root / "database" / "database_reading.py").read_text(
                encoding="utf-8"
            )
            self.assertIn("get_media_items_presence_batch", database_source)
            self.assertNotIn("old_reader", database_source)

    def test_matching_release_is_reinstalled_when_source_is_inconsistent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_source(root, exported_names=("old_reader",))
            config = self._config(root, auto_update=True)
            process_handler = self._process_handler()

            with (
                patch.object(
                    setup.CONFIG_MANAGER,
                    "find_key_for_process",
                    return_value=("cli_debrid", None),
                ),
                patch.object(setup.CONFIG_MANAGER, "get_instance", return_value=config),
                patch.object(
                    setup.versions,
                    "version_check",
                    return_value=("v0.7.48", None),
                ),
                patch.object(
                    setup, "setup_release_version", return_value=(True, None)
                ) as install_release,
            ):
                success, error = setup.install_project(
                    process_handler, "CLI Debrid Custom"
                )

            self.assertTrue(success, error)
            install_release.assert_called_once()

    def test_configure_rejects_inconsistent_source_without_marking_setup_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_source(root, exported_names=("old_reader",))
            config = self._config(root)
            process_handler = self._process_handler()

            with (
                patch.object(
                    setup.CONFIG_MANAGER,
                    "find_key_for_process",
                    return_value=("cli_debrid", None),
                ),
                patch.object(setup.CONFIG_MANAGER, "get_instance", return_value=config),
            ):
                success, error = setup.configure_project(
                    process_handler, "CLI Debrid Custom"
                )

            self.assertFalse(success)
            self.assertIn("get_media_items_presence_batch", error)
            self.assertNotIn("CLI Debrid Custom", process_handler.setup_tracker)

    def test_unselected_mixed_runtime_repairs_installed_release(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_source(root, exported_names=("old_reader",))
            config = self._config(
                root,
                release_version_enabled=False,
                release_version="v0.6.07",
            )
            process_handler = self._process_handler()
            requested_releases = []

            def install_release(_handler, repair_config, _process_name, _key):
                requested_releases.append(repair_config["release_version"])
                return True, None

            with (
                patch.object(
                    setup.CONFIG_MANAGER,
                    "find_key_for_process",
                    return_value=("cli_debrid", None),
                ),
                patch.object(setup.CONFIG_MANAGER, "get_instance", return_value=config),
                patch.object(
                    setup.versions,
                    "version_check",
                    return_value=("0.7.48", None),
                ),
                patch.object(
                    setup, "setup_release_version", side_effect=install_release
                ),
            ):
                success, error = setup.install_project(
                    process_handler, "CLI Debrid Custom"
                )

            self.assertTrue(success, error)
            self.assertEqual(["v0.7.48"], requested_releases)
            self.assertEqual("v0.6.07", config["release_version"])


if __name__ == "__main__":
    unittest.main()
