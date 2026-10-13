import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils import arr
from utils.auto_update import Update
from tests.test_whisparr_setup import write_whisparr_runtime


class WhisparrUpdateTests(unittest.TestCase):
    def _updater(self):
        updater = object.__new__(Update)
        updater.logger = Mock()
        updater.process_handler = Mock()
        updater.process_handler.process_names = {"Whisparr Adult"}
        updater.process_handler.setup_tracker = {"Whisparr Adult"}
        updater.process_handler.setup_tracker_lock = threading.Lock()
        updater.stop_process = Mock()
        updater.start_process = Mock(return_value=(Mock(), None))
        return updater

    def test_whisparr_branch_selects_a_channel_without_source_pin(self):
        updater = self._updater()
        config = {"branch_enabled": True, "branch": "eros"}
        self.assertIsNone(updater._get_update_block_reason(config, "whisparr"))
        self.assertEqual(updater._get_update_block_reason(config, "radarr"), "branch")
        for field, value, reason in (
            ("pinned_version", "3.6.4.1865", "pinned_version"),
            ("commit_sha", "a" * 40, "commit"),
        ):
            with self.subTest(field=field):
                self.assertEqual(
                    updater._get_update_block_reason(
                        {**config, field: value}, "whisparr"
                    ),
                    reason,
                )

    def test_conflicting_database_blocks_every_direct_update_route(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp, "whisparr3.db")
            database.write_bytes(b"preserved v3 data")
            config = {"config_dir": temp, "whisparr_version": "v2"}
            for route in ("scheduled", "latest", "pinned", "configured"):
                with self.subTest(route=route):
                    updater = self._updater()
                    with (
                        patch.object(arr, "Downloader") as download,
                        patch("utils.auto_update.configure_project") as configure,
                    ):
                        if route == "scheduled":
                            success, error = updater.update_check(
                                "Whisparr Adult", config, "whisparr", "Adult"
                            )
                        elif route == "latest":
                            success, error = updater.update_check_arr_latest(
                                "Whisparr Adult", config, "whisparr", "Adult"
                            )
                        elif route == "pinned":
                            success, error = updater.update_check_pinned_version(
                                "Whisparr Adult",
                                config,
                                "whisparr",
                                "Adult",
                                "2.2.0.231",
                            )
                        else:
                            success, error = updater._install_configured_target(
                                "Whisparr Adult", config, "whisparr", "Adult", "release"
                            )
                    self.assertFalse(success)
                    self.assertIn("existing v3 database", error)
                    updater.stop_process.assert_not_called()
                    updater.start_process.assert_not_called()
                    download.assert_not_called()
                    configure.assert_not_called()
                    self.assertEqual(database.read_bytes(), b"preserved v3 data")

    def test_source_commit_is_rejected_before_stop(self):
        updater = self._updater()
        config = {"whisparr_version": "v3", "commit_sha": "a" * 40}
        with patch.object(arr, "Downloader") as download:
            success, error = updater.update_check(
                "Whisparr Adult", config, "whisparr", "Adult"
            )
        self.assertFalse(success)
        self.assertIn("source/commit builds are not supported", error)
        updater.stop_process.assert_not_called()
        download.assert_not_called()

    def test_wrong_family_published_release_fails_before_stop(self):
        updater = self._updater()
        config = {
            "whisparr_version": "v3",
            "release_version_enabled": True,
            "release_version": "latest",
        }
        downloader = Mock()
        downloader.get_latest_release.return_value = ("v2.2.0-release.231", None)
        with patch.object(arr, "Downloader", return_value=downloader):
            success, error = updater.update_check_arr_latest(
                "Whisparr Adult", config, "whisparr", "Adult"
            )
        self.assertFalse(success)
        self.assertIn("does not match selected v3", error)
        updater.stop_process.assert_not_called()
        downloader.download_release_version.assert_not_called()

    def test_wrong_major_download_preserves_running_process_and_old_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir = root / "install"
            binary = write_whisparr_runtime(
                install_dir / "Whisparr" / "Whisparr", "3.6.2.1727"
            )
            config = {
                "whisparr_version": "v3",
                "command": [str(binary)],
                "release_version_enabled": True,
                "release_version": "latest",
            }
            updater = self._updater()
            downloader = Mock()
            downloader.get_latest_release.return_value = ("v3.6.4-release.1865", None)

            def download(**kwargs):
                write_whisparr_runtime(
                    Path(kwargs["target_dir"]) / "Whisparr", "2.2.0.231"
                )
                return True, None

            downloader.download_release_version.side_effect = download
            with (
                patch.object(arr, "Downloader", return_value=downloader),
                patch(
                    "utils.auto_update._resolve_arr_install_dir",
                    return_value=(str(install_dir), True),
                ),
                patch(
                    "utils.transactional_install.install_cache_root",
                    return_value=root / "cache",
                ),
            ):
                success, error = updater.update_check(
                    "Whisparr Adult", config, "whisparr", "Adult"
                )
            self.assertFalse(success)
            self.assertIn("does not match selected v3", error)
            updater.stop_process.assert_not_called()
            updater.start_process.assert_not_called()
            self.assertEqual(updater.process_handler.setup_tracker, {"Whisparr Adult"})
            self.assertIn(
                b"Version=3.6.2.1727",
                binary.with_name("Whisparr.Core.dll").read_bytes(),
            )
            self.assertFalse(any("candidate" in child.name for child in root.iterdir()))

    def test_valid_candidate_is_checked_before_stop_and_live_config_is_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir = root / "install"
            binary = write_whisparr_runtime(
                install_dir / "Whisparr" / "Whisparr", "3.6.2.1727"
            )
            config = {"whisparr_version": "v3", "command": [str(binary)]}
            updater = self._updater()
            observations = []
            updater.stop_process.side_effect = lambda name: observations.append(
                "stopped"
            )
            downloader = Mock()
            downloader.get_latest_release.return_value = ("v3.6.4-release.1865", None)

            def download(**kwargs):
                observations.append("downloaded while running")
                self.assertFalse(updater.stop_process.called)
                write_whisparr_runtime(
                    Path(kwargs["target_dir"]) / "Whisparr", "3.6.4.1865"
                )
                return True, None

            downloader.download_release_version.side_effect = download
            with (
                patch.object(arr, "Downloader", return_value=downloader),
                patch(
                    "utils.auto_update._resolve_arr_install_dir",
                    return_value=(str(install_dir), True),
                ),
                patch(
                    "utils.auto_update.configure_project", return_value=(True, None)
                ) as configure,
                patch(
                    "utils.transactional_install.install_cache_root",
                    return_value=root / "cache",
                ),
            ):
                success, error = updater.update_check(
                    "Whisparr Adult", config, "whisparr", "Adult"
                )
            self.assertTrue(success, error)
            self.assertEqual(observations, ["downloaded while running", "stopped"])
            configure.assert_called_once_with(updater.process_handler, "Whisparr Adult")
            updater.start_process.assert_called_once_with(
                "Whisparr Adult", config, "whisparr", "Adult"
            )
            self.assertEqual(updater.process_handler.setup_tracker, set())
            self.assertIn(
                b"Version=3.6.4.1865",
                binary.with_name("Whisparr.Core.dll").read_bytes(),
            )

    def test_existing_v3_database_routes_legacy_nightly_to_eros_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "whisparr3.db").write_bytes(b"preserve")
            config = {
                "config_dir": temp,
                "branch": "nightly",
                "repo_owner": "Whisparr",
                "repo_name": "Whisparr",
            }
            updater = self._updater()
            downloader = Mock()
            downloader.get_latest_release.return_value = ("v3.6.4-release.1865", None)
            with patch.object(arr, "Downloader", return_value=downloader):
                payload = updater._manual_check_whisparr(
                    "Whisparr Adult",
                    config,
                    "whisparr",
                    "Adult",
                    None,
                    10,
                    False,
                    24,
                    "04:00",
                    None,
                )
            self.assertEqual(payload["status"], "update_available")
            self.assertEqual(payload["available_version"], "3.6.4.1865")
            downloader.get_latest_release.assert_called_once_with(
                "Whisparr", "Whisparr-Eros", prerelease=False
            )
            updater.stop_process.assert_not_called()

    def test_installed_numeric_version_matches_published_tag_without_perpetual_update(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp:
            install_dir = Path(temp, "install")
            binary = write_whisparr_runtime(
                install_dir / "Whisparr" / "Whisparr", "3.6.4.1865"
            )
            config = {
                "whisparr_version": "v3",
                "command": [str(binary)],
                "release_version_enabled": True,
                "release_version": "latest",
            }
            updater = self._updater()
            downloader = Mock()
            downloader.get_latest_release.return_value = ("v3.6.4-release.1865", None)
            with (
                patch.object(arr, "Downloader", return_value=downloader),
                patch(
                    "utils.auto_update._resolve_arr_install_dir",
                    return_value=(str(install_dir), True),
                ),
            ):
                payload = updater._manual_check_whisparr(
                    "Whisparr Adult",
                    config,
                    "whisparr",
                    "Adult",
                    None,
                    10,
                    False,
                    24,
                    "04:00",
                    None,
                )
                success, error = updater.update_check(
                    "Whisparr Adult", config, "whisparr", "Adult"
                )
            self.assertEqual(payload["status"], "no_update")
            self.assertFalse(success)
            self.assertIn("No updates available", error)
            downloader.download_release_version.assert_not_called()
            updater.stop_process.assert_not_called()

    def test_configured_target_uses_validated_whisparr_installer(self):
        updater = self._updater()
        config = {
            "whisparr_version": "v3",
            "release_version_enabled": True,
            "release_version": "v3.6.4-release.1865",
        }
        with (
            patch.object(
                updater,
                "_update_whisparr",
                return_value=(True, "installed selected release"),
            ) as install,
            patch("utils.auto_update.setup_project") as generic,
        ):
            result = updater._install_configured_target(
                "Whisparr Adult", config, "whisparr", "Adult", "release"
            )
        self.assertEqual(result, (True, "installed selected release"))
        install.assert_called_once_with(
            "Whisparr Adult", config, "whisparr", "Adult", force=True
        )
        generic.assert_not_called()

    def test_pinned_route_overrides_inactive_release_selector_without_copying_live_config(
        self,
    ):
        updater = self._updater()
        config = {
            "whisparr_version": "v3",
            "release_version_enabled": True,
            "release_version": "latest",
        }
        with patch.object(
            updater, "_update_whisparr", return_value=(True, "installed")
        ) as install:
            updater.update_check_pinned_version(
                "Whisparr Adult", config, "whisparr", "Adult", "3.6.4.1865"
            )
        args, kwargs = install.call_args
        self.assertIs(args[1], config)
        self.assertFalse(kwargs["target_config"]["release_version_enabled"])
        self.assertEqual(kwargs["target_config"]["pinned_version"], "3.6.4.1865")
        self.assertEqual(config["release_version"], "latest")

    def test_modern_v2_update_retains_legacy_library_and_running_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = write_whisparr_runtime(
                root / "old" / "Whisparr" / "Whisparr", "2.0.0.2151"
            )
            data = root / "data"
            data.mkdir()
            database = data / "whisparr.db"
            database.write_bytes(b"legacy series library")
            target = root / "managed"
            config = {
                "whisparr_version": "v2",
                "config_dir": str(data),
                "command": [str(source)],
                "release_version_enabled": True,
                "release_version": "latest",
            }
            updater = self._updater()
            downloader = Mock()
            downloader.get_latest_release.return_value = ("v2.2.0-release.231", None)

            def download(**kwargs):
                write_whisparr_runtime(
                    Path(kwargs["target_dir"]) / "Whisparr", "2.2.0.231"
                )
                return True, None

            downloader.download_release_version.side_effect = download
            with (
                patch.object(arr, "Downloader", return_value=downloader),
                patch(
                    "utils.auto_update._resolve_arr_install_dir",
                    return_value=(str(target), True),
                ),
                patch(
                    "utils.transactional_install.install_cache_root",
                    return_value=root / "cache",
                ),
            ):
                success, error = updater.update_check(
                    "Whisparr Adult", config, "whisparr", "Adult"
                )
            self.assertFalse(success)
            self.assertIn("does not automatically migrate", error)
            updater.stop_process.assert_not_called()
            updater.start_process.assert_not_called()
            self.assertEqual(updater.process_handler.setup_tracker, {"Whisparr Adult"})
            self.assertEqual(database.read_bytes(), b"legacy series library")
            self.assertFalse((data / "whisparr2.db").exists())
            self.assertFalse(target.exists())
            self.assertIn(
                b"Version=2.0.0.2151",
                source.with_name("Whisparr.Core.dll").read_bytes(),
            )
