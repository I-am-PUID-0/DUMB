import threading
import unittest
from unittest.mock import Mock, patch

from api.api_state import APIState
from utils.auto_update import Update


class UpdateNotificationTests(unittest.TestCase):
    def _updater(self):
        updater = object.__new__(Update)
        updater.logger = Mock()
        updater.scheduler = Mock()
        updater.updating = threading.Lock()
        updater._safe_record_update_status = Mock()
        updater.supports_manual_update = Mock(return_value=True)
        return updater

    def test_manual_update_check_records_missing_configuration(self):
        updater = self._updater()
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("sonarr", "Main")
        config_manager.get_instance.return_value = None

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            payload = updater.manual_update_check("Sonarr Main")

        self.assertEqual(payload["status"], "error")
        updater._safe_record_update_status.assert_called_once_with(
            "Sonarr Main", payload
        )

    def test_manual_update_install_records_unsupported_service(self):
        updater = self._updater()
        updater.supports_manual_update.return_value = False
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("example", None)
        config_manager.get_instance.return_value = {
            "process_name": "Example",
            "enabled": True,
        }

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            payload = updater.manual_update_install("Example")

        self.assertEqual(payload["status"], "unsupported")
        updater._safe_record_update_status.assert_called_once_with("Example", payload)

    def test_commit_sha_blocks_moving_update_target(self):
        updater = self._updater()

        self.assertEqual(
            "commit",
            updater._get_update_block_reason({"commit_sha": "a" * 40}),
        )

    def test_commit_status_reports_whether_configured_target_is_installed(self):
        updater = self._updater()
        updater.auto_update_interval = Mock(return_value=24)
        updater.auto_update_start_time = Mock(return_value="04:00")
        commit_sha = "a" * 40
        config = {
            "commit_sha": commit_sha,
            "auto_update": True,
            "repo_owner": "nzbdav",
            "repo_name": "nzbdav",
        }

        with patch("utils.auto_update.Versions") as versions:
            versions.return_value.version_check.return_value = ("v0.8.1", None)
            pending = updater._manual_check_generic_repo(
                "NzbDAV",
                config,
                "nzbdav",
                None,
                "commit",
                1,
                False,
                24,
                "04:00",
                None,
            )
            versions.return_value.version_check.return_value = (
                f"commit-{commit_sha[:12]}",
                None,
            )
            installed = updater._manual_check_generic_repo(
                "NzbDAV",
                config,
                "nzbdav",
                None,
                "commit",
                2,
                False,
                24,
                "04:00",
                None,
            )

        self.assertFalse(pending["configured_target_installed"])
        self.assertEqual("v0.8.1", pending["current_version"])
        self.assertTrue(installed["configured_target_installed"])

    def test_fixed_release_status_uses_the_configured_release_target(self):
        updater = self._updater()
        config = {
            "release_version_enabled": True,
            "release_version": "v0.7.9",
            "repo_owner": "nzbdav",
            "repo_name": "nzbdav",
        }

        with patch("utils.auto_update.Versions") as versions:
            versions.return_value.version_check.return_value = ("v0.8.1", None)
            pending = updater._manual_check_generic_repo(
                "NzbDAV",
                config,
                "nzbdav",
                None,
                "release",
                1,
                False,
                24,
                "04:00",
                None,
            )
            versions.return_value.version_check.return_value = ("0.7.9", None)
            installed = updater._manual_check_generic_repo(
                "NzbDAV",
                config,
                "nzbdav",
                None,
                "release",
                2,
                False,
                24,
                "04:00",
                None,
            )

        self.assertEqual("blocked", pending["status"])
        self.assertEqual("v0.7.9", pending["available_version"])
        self.assertEqual("release", pending["configured_target_kind"])
        self.assertFalse(pending["configured_target_installed"])
        self.assertEqual("no_update", installed["status"])
        self.assertTrue(installed["configured_target_installed"])
        versions.return_value.compare_versions.assert_not_called()

    def test_commit_pin_disables_initial_update_even_for_prerelease_selector(self):
        updater = self._updater()
        updater.process_handler = Mock(
            preinstall_complete=False,
            preinstalled_processes=set(),
        )
        updater.reschedule_symlink_backup = Mock()
        updater.initial_update_check = Mock()
        updater.start_process = Mock(return_value=("started", None))
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("nzbdav", None)
        config_manager.get_instance.return_value = {
            "process_name": "NzbDAV",
            "auto_update": True,
            "commit_sha": "a" * 40,
            "release_version_enabled": True,
            "release_version": "prerelease",
        }
        existing_job = object()
        Update._jobs = {"NzbDAV": existing_job}
        Update._next_check_at = {"NzbDAV": 123}

        with (
            patch("utils.auto_update.CONFIG_MANAGER", config_manager),
            patch("utils.auto_update.setup_project", return_value=(True, None)),
            patch("utils.auto_update.threading.Thread") as update_thread,
        ):
            process, error = updater.auto_update("NzbDAV", enable_update=True)

        self.assertEqual("started", process)
        self.assertIsNone(error)
        updater.initial_update_check.assert_not_called()
        update_thread.assert_not_called()
        updater.scheduler.cancel_job.assert_called_once_with(existing_job)
        self.assertNotIn("NzbDAV", Update._jobs)
        self.assertNotIn("NzbDAV", Update._next_check_at)

    def test_reschedule_cancels_existing_job_for_commit_pin(self):
        updater = self._updater()
        existing_job = object()
        Update._jobs = {"NzbDAV": existing_job}
        Update._next_check_at = {"NzbDAV": 123}
        updater.update_schedule = Mock()
        updater.auto_update_interval = Mock(return_value=24)
        updater.auto_update_start_time = Mock(return_value="04:00")
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("nzbdav", None)
        config_manager.get_instance.return_value = {
            "auto_update": True,
            "commit_sha": "a" * 40,
        }

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            success, message = updater.reschedule_auto_update("NzbDAV")

        self.assertTrue(success)
        self.assertEqual("Auto-update disabled by commit pin", message)
        updater.scheduler.cancel_job.assert_called_once_with(existing_job)
        updater.update_schedule.assert_not_called()
        self.assertNotIn("NzbDAV", Update._jobs)
        self.assertNotIn("NzbDAV", Update._next_check_at)
        status = updater._safe_record_update_status.call_args.args[1]
        self.assertEqual("blocked", status["status"])
        self.assertEqual("commit", status["reason"])
        self.assertFalse(status["auto_update_enabled"])

    def test_stale_scheduled_callback_stops_when_commit_pin_is_detected(self):
        updater = self._updater()
        existing_job = object()
        Update._jobs = {"NzbDAV": existing_job}
        Update._next_check_at = {"NzbDAV": 123}
        updater.scheduled_update_check = Mock()
        config_manager = Mock()
        config_manager.get_instance.return_value = {
            "auto_update": True,
            "commit_sha": "a" * 40,
        }

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            updater._run_scheduled_update_if_due(
                "NzbDAV",
                {"auto_update": True},
                "nzbdav",
                None,
            )

        updater.scheduler.cancel_job.assert_called_once_with(existing_job)
        updater.scheduled_update_check.assert_not_called()
        self.assertNotIn("NzbDAV", Update._jobs)
        self.assertNotIn("NzbDAV", Update._next_check_at)

    def test_direct_update_check_never_resolves_latest_for_commit_pin(self):
        updater = self._updater()
        updater.process_handler = Mock()

        with patch("utils.auto_update.Versions") as versions:
            success, message = updater.update_check(
                "NzbDAV",
                {
                    "commit_sha": "a" * 40,
                    "release_version_enabled": False,
                    "release_version": "latest",
                },
                "nzbdav",
                None,
            )

        self.assertFalse(success)
        self.assertIn("pinned to commit aaaaaaaaaaaa", message)
        versions.assert_not_called()

    def test_manual_override_preserves_commit_saved_while_install_is_running(self):
        updater = self._updater()
        commit_sha = "b" * 40
        config = {
            "repo_owner": "nzbdav",
            "repo_name": "nzbdav",
            "pinned_version": "",
            "commit_sha": "",
            "release_version_enabled": False,
            "release_version": "latest",
            "branch_enabled": True,
            "branch": "main",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("nzbdav", None)
        config_manager.get_instance.return_value = config

        def install_while_source_changes(*_args):
            config.update(
                {
                    "commit_sha": commit_sha,
                    "release_version_enabled": False,
                    "branch_enabled": False,
                }
            )
            return True, "Updated NzbDAV."

        updater.update_check = Mock(side_effect=install_while_source_changes)

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            payload = updater.manual_update_install("NzbDAV", allow_override=True)

        self.assertEqual("updated", payload["status"])
        self.assertEqual(commit_sha, config["commit_sha"])
        self.assertFalse(config["release_version_enabled"])
        self.assertFalse(config["branch_enabled"])
        updater.logger.info.assert_any_call(
            "Preserving newer source selection for %s saved during manual update.",
            "NzbDAV",
        )
        updater.process_handler = Mock()
        with patch("utils.auto_update.Versions") as versions:
            success, message = Update.update_check(
                updater,
                "NzbDAV",
                config,
                "nzbdav",
                None,
            )
        self.assertFalse(success)
        self.assertIn("pinned to commit bbbbbbbbbbbb", message)
        versions.assert_not_called()

    def test_manual_latest_install_preserves_commit_saved_while_running(self):
        updater = self._updater()
        commit_sha = "c" * 40
        config = {
            "repo_owner": "nzbdav",
            "repo_name": "nzbdav",
            "pinned_version": "",
            "commit_sha": "",
            "release_version_enabled": False,
            "release_version": "latest",
            "branch_enabled": False,
            "branch": "main",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("nzbdav", None)
        config_manager.get_instance.return_value = config

        def install_while_source_changes(*_args):
            config["commit_sha"] = commit_sha
            return True, "Updated NzbDAV."

        updater.update_check = Mock(side_effect=install_while_source_changes)

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            payload = updater.manual_update_install("NzbDAV")

        self.assertEqual("updated", payload["status"])
        self.assertEqual(commit_sha, config["commit_sha"])
        updater.logger.info.assert_any_call(
            "Preserving newer source selection for %s saved during manual update.",
            "NzbDAV",
        )

    def test_manual_latest_override_temporarily_ignores_branch_selection(self):
        updater = self._updater()
        config = {
            "repo_owner": "nzbdav",
            "repo_name": "nzbdav",
            "pinned_version": "",
            "commit_sha": "",
            "release_version_enabled": True,
            "release_version": "prerelease",
            "branch_enabled": True,
            "branch": "main",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("nzbdav", None)
        config_manager.get_instance.return_value = config

        def assert_latest_selection(*_args):
            self.assertFalse(config["branch_enabled"])
            self.assertFalse(config["release_version_enabled"])
            self.assertEqual("", config["commit_sha"])
            return True, "Updated NzbDAV to latest stable release."

        updater.update_check = Mock(side_effect=assert_latest_selection)

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            payload = updater.manual_update_install("NzbDAV", allow_override=True)

        self.assertEqual("updated", payload["status"])
        self.assertTrue(config["branch_enabled"])
        self.assertEqual("main", config["branch"])
        self.assertTrue(config["release_version_enabled"])
        self.assertEqual("prerelease", config["release_version"])

    def test_configured_commit_install_applies_pin_without_update_override(self):
        updater = self._updater()
        updater.process_handler = Mock(
            process_names=[],
            setup_tracker=set(),
            setup_tracker_lock=threading.Lock(),
        )
        updater.start_process = Mock(return_value=("started", None))
        updater.update_check = Mock()
        commit_sha = "d" * 40
        config = {
            "repo_owner": "nzbdav",
            "repo_name": "nzbdav",
            "commit_sha": commit_sha,
            "release_version_enabled": False,
            "release_version": "latest",
            "branch_enabled": False,
            "branch": "main",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("nzbdav", None)
        config_manager.get_instance.return_value = config

        with (
            patch("utils.auto_update.CONFIG_MANAGER", config_manager),
            patch(
                "utils.auto_update.setup_project", return_value=(True, None)
            ) as setup,
        ):
            payload = updater.manual_update_install(
                "NzbDAV",
                allow_override=False,
                target="configured",
            )

        self.assertEqual("updated", payload["status"])
        self.assertEqual(commit_sha, config["commit_sha"])
        setup.assert_called_once_with(updater.process_handler, "NzbDAV")
        updater.update_check.assert_not_called()
        updater.start_process.assert_called_once_with(
            "NzbDAV",
            config,
            "nzbdav",
            None,
        )

    def test_configured_release_install_preserves_the_saved_release(self):
        updater = self._updater()
        updater.process_handler = Mock(
            process_names=[],
            setup_tracker=set(),
            setup_tracker_lock=threading.Lock(),
        )
        updater.start_process = Mock(return_value=("started", None))
        updater.update_check = Mock()
        config = {
            "repo_owner": "nzbdav",
            "repo_name": "nzbdav",
            "commit_sha": "",
            "release_version_enabled": True,
            "release_version": "v0.7.9",
            "branch_enabled": False,
            "branch": "main",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("nzbdav", None)
        config_manager.get_instance.return_value = config

        with (
            patch("utils.auto_update.CONFIG_MANAGER", config_manager),
            patch(
                "utils.auto_update.setup_project", return_value=(True, None)
            ) as setup,
        ):
            payload = updater.manual_update_install(
                "NzbDAV",
                allow_override=False,
                target="configured",
            )

        self.assertEqual("updated", payload["status"])
        self.assertTrue(config["release_version_enabled"])
        self.assertEqual("v0.7.9", config["release_version"])
        setup.assert_called_once_with(updater.process_handler, "NzbDAV")
        updater.update_check.assert_not_called()

    @patch("utils.auto_update.Versions")
    def test_preinstalled_commit_runs_install_only_when_marker_differs(self, versions):
        updater = self._updater()
        commit_sha = "a" * 40
        config = {"commit_sha": commit_sha}
        versions.return_value.version_check.return_value = (
            f"commit-{commit_sha[:12]}",
            None,
        )

        self.assertFalse(
            updater._should_run_install_phase_for_preinstalled(
                "NzbDAV", "nzbdav", None, config
            )
        )

        versions.return_value.version_check.return_value = ("commit-bbbbbbbbbbbb", None)
        self.assertTrue(
            updater._should_run_install_phase_for_preinstalled(
                "NzbDAV", "nzbdav", None, config
            )
        )

    @patch("api.api_state.notify_event")
    def test_update_available_notifies_only_for_new_state_or_version(
        self, notify_event
    ):
        api_state = object.__new__(APIState)
        api_state._update_cache = {}
        api_state._update_cache_lock = threading.Lock()

        api_state.set_update_status(
            "Radarr",
            {"status": "update_available", "available_version": "1.1.0"},
        )
        api_state.set_update_status(
            "Radarr",
            {"status": "update_available", "available_version": "1.1.0"},
        )
        api_state.set_update_status(
            "Radarr",
            {"status": "update_available", "available_version": "1.2.0"},
        )

        self.assertEqual(notify_event.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in notify_event.call_args_list],
            ["update.available", "update.available"],
        )


if __name__ == "__main__":
    unittest.main()
