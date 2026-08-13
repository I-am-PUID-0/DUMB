import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from api.api_state import APIState
from utils.auto_update import Update
from utils.config_loader import CONFIG_MANAGER


class UpdateNotificationTests(unittest.TestCase):
    def _updater(self):
        updater = object.__new__(Update)
        updater.logger = Mock()
        updater.downloader = Mock()
        updater.scheduler = Mock()
        updater.updating = threading.Lock()
        updater._write_update_status = Mock()
        updater._safe_record_update_status = Mock()
        updater.supports_manual_update = Mock(return_value=True)
        updater._rollback_snapshots = {}
        updater._active_install_operation = None
        return updater

    def _frontend_transaction_updater(self, target):
        updater = self._updater()
        process_handler = Mock()
        process_handler.logger = Mock()
        process_handler.process_names = {"DUMB Frontend"}
        process_handler.setup_tracker = {"DUMB Frontend"}
        process_handler.setup_tracker_lock = threading.Lock()

        def stop_process(process_name):
            process_handler.process_names.discard(process_name)

        process_handler.stop_process.side_effect = stop_process
        updater.process_handler = process_handler
        updater._mark_update_downtime_started = Mock()
        updater.start_process = Mock(return_value=(Mock(), None))
        updater._wait_for_update_health = Mock(return_value=(True, None))
        config = {
            "config_dir": str(target),
            "exclude_dirs": [],
            "env": {},
        }
        return updater, process_handler, config

    def test_frontend_candidate_build_keeps_current_ui_running_until_activation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "frontend"
            target.mkdir()
            (target / "old-runtime").write_text("old", encoding="utf-8")
            updater, process_handler, config = self._frontend_transaction_updater(
                target
            )
            build_observations = []

            def installer():
                build_observations.append(
                    "DUMB Frontend" in process_handler.process_names
                )
                candidate = Path(config["config_dir"])
                (candidate / "package.json").write_text("{}", encoding="utf-8")
                entry = candidate / ".output" / "server" / "index.mjs"
                entry.parent.mkdir(parents=True)
                entry.write_text("export default {}", encoding="utf-8")
                config["env"] = {"RUNTIME_ROOT": str(candidate)}
                return True, None

            with (
                patch(
                    "utils.transactional_install.install_cache_root",
                    return_value=root / "cache",
                ),
                patch("utils.auto_update.configure_project", return_value=(True, None)),
                patch.object(CONFIG_MANAGER, "save_config", create=True),
            ):
                success, message = updater._transactional_frontend_update(
                    "DUMB Frontend",
                    config,
                    "dumb_frontend",
                    None,
                    "v1.80.0",
                    installer,
                )

            self.assertTrue(success)
            self.assertIn("transactionally", message)
            self.assertEqual(build_observations, [True])
            process_handler.stop_process.assert_called_once_with("DUMB Frontend")
            self.assertFalse((target / "old-runtime").exists())
            self.assertTrue((target / ".output" / "server" / "index.mjs").is_file())
            self.assertEqual(config["config_dir"], str(target))
            self.assertEqual(config["env"]["RUNTIME_ROOT"], str(target))

    def test_frontend_candidate_build_failure_leaves_current_ui_running(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "frontend"
            target.mkdir()
            (target / "old-runtime").write_text("old", encoding="utf-8")
            updater, process_handler, config = self._frontend_transaction_updater(
                target
            )

            with (
                patch(
                    "utils.transactional_install.install_cache_root",
                    return_value=root / "cache",
                ),
                patch.object(CONFIG_MANAGER, "save_config", create=True),
            ):
                success, message = updater._transactional_frontend_update(
                    "DUMB Frontend",
                    config,
                    "dumb_frontend",
                    None,
                    "v1.80.0",
                    lambda: (False, "pnpm build failed"),
                )

            self.assertFalse(success)
            self.assertIn("existing frontend retained", message)
            self.assertTrue((target / "old-runtime").is_file())
            self.assertIn("DUMB Frontend", process_handler.process_names)
            process_handler.stop_process.assert_not_called()

    def test_frontend_candidate_verification_reports_missing_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "frontend"
            target.mkdir()
            (target / "old-runtime").write_text("old", encoding="utf-8")
            updater, process_handler, config = self._frontend_transaction_updater(
                target
            )

            with (
                patch(
                    "utils.transactional_install.install_cache_root",
                    return_value=root / "cache",
                ),
                patch.object(CONFIG_MANAGER, "save_config", create=True),
            ):
                success, message = updater._transactional_frontend_update(
                    "DUMB Frontend",
                    config,
                    "dumb_frontend",
                    None,
                    "v1.80.0",
                    lambda: (True, None),
                )

            self.assertFalse(success)
            self.assertIn("package.json", message)
            self.assertIn(".output/server/index.mjs", message)
            self.assertTrue((target / "old-runtime").is_file())
            process_handler.stop_process.assert_not_called()

    def test_configured_frontend_release_bypasses_scheduled_update_guard(self):
        updater = self._updater()
        updater.process_handler = Mock()
        config = {
            "auto_update": True,
            "release_version_enabled": True,
            "release_version": "v1.81.0",
        }

        def run_candidate(
            process_name,
            candidate_config,
            key,
            instance_name,
            source_identity,
            installer,
        ):
            return installer()

        updater._transactional_frontend_update = Mock(side_effect=run_candidate)

        with (
            patch(
                "utils.auto_update.setup_release_version",
                return_value=(True, None),
            ) as setup_release,
            patch("utils.auto_update.setup_project") as setup_default,
        ):
            success, error = updater._install_configured_target(
                "DUMB Frontend",
                config,
                "dumb_frontend",
                None,
                "release",
            )

        self.assertTrue(success)
        self.assertIsNone(error)
        setup_release.assert_called_once_with(
            updater.process_handler,
            config,
            "DUMB Frontend",
            "dumb_frontend",
        )
        setup_default.assert_not_called()

    def test_frontend_failed_activation_restores_previous_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "frontend"
            target.mkdir()
            (target / "old-runtime").write_text("old", encoding="utf-8")
            updater, _, config = self._frontend_transaction_updater(target)
            updater._wait_for_update_health = Mock(
                side_effect=[(False, "probe failed"), (True, None)]
            )

            def installer():
                candidate = Path(config["config_dir"])
                (candidate / "package.json").write_text("{}", encoding="utf-8")
                entry = candidate / ".output" / "server" / "index.mjs"
                entry.parent.mkdir(parents=True)
                entry.write_text("export default {}", encoding="utf-8")
                return True, None

            with (
                patch(
                    "utils.transactional_install.install_cache_root",
                    return_value=root / "cache",
                ),
                patch("utils.auto_update.configure_project", return_value=(True, None)),
                patch.object(CONFIG_MANAGER, "save_config", create=True),
            ):
                success, message = updater._transactional_frontend_update(
                    "DUMB Frontend",
                    config,
                    "dumb_frontend",
                    None,
                    "v1.80.0",
                    installer,
                )

            self.assertFalse(success)
            self.assertIn("previous frontend restored", message)
            self.assertTrue((target / "old-runtime").is_file())
            self.assertFalse((target / ".output").exists())

    def test_unhealthy_replacement_triggers_runtime_recovery(self):
        updater = self._updater()
        updater._rollback_snapshots["Example"] = Mock()
        updater._wait_for_update_health = Mock(
            return_value=(False, "application probe failed")
        )
        updater._recover_pending_snapshot = Mock(return_value=True)

        process, error = updater._finalize_runtime_snapshot("Example", Mock())

        self.assertFalse(process)
        self.assertIn("previous runtime restored", error)
        updater._recover_pending_snapshot.assert_called_once_with("Example")

    def test_replacement_without_snapshot_still_requires_stable_health(self):
        updater = self._updater()
        updater._wait_for_update_health = Mock(return_value=(True, None))
        process = Mock()

        finalized, error = updater._finalize_runtime_snapshot("Example", process)

        self.assertIs(process, finalized)
        self.assertIsNone(error)
        updater._wait_for_update_health.assert_called_once_with("Example")

    def test_unhealthy_replacement_without_snapshot_reports_no_rollback(self):
        updater = self._updater()
        updater._wait_for_update_health = Mock(
            return_value=(False, "application probe failed")
        )

        finalized, error = updater._finalize_runtime_snapshot("Example", Mock())

        self.assertFalse(finalized)
        self.assertIn("application probe failed", error)
        self.assertIn("no automatic runtime rollback", error)

    def test_runtime_recovery_restores_configures_and_restarts_previous_version(self):
        updater = self._updater()
        snapshot = Mock()
        snapshot.rollback.return_value = True
        updater._rollback_snapshots["Example"] = snapshot
        updater.process_handler = Mock()
        updater.process_handler.transactional_update_snapshots = {"Example"}
        updater.process_handler.setup_tracker = {"Example"}
        updater.process_handler.setup_tracker_lock = threading.Lock()
        updater.start_process = Mock(return_value=(Mock(), None))
        updater._wait_for_update_health = Mock(return_value=(True, None))
        config = {"process_name": "Example"}
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("example", None)
        config_manager.get_instance.return_value = config

        with (
            patch("utils.auto_update.CONFIG_MANAGER", config_manager),
            patch("utils.auto_update.configure_project", return_value=(True, None)),
        ):
            restored = updater._recover_pending_snapshot("Example")

        self.assertTrue(restored)
        updater.process_handler.stop_process.assert_called_once_with("Example")
        snapshot.rollback.assert_called_once_with()
        snapshot.commit.assert_called_once_with()
        updater.start_process.assert_called_once_with(
            "Example", config, "example", None
        )
        updater._wait_for_update_health.assert_called_once_with("Example")

    def test_runtime_recovery_reports_failure_when_previous_version_will_not_start(
        self,
    ):
        updater = self._updater()
        snapshot = Mock()
        snapshot.rollback.return_value = True
        updater._rollback_snapshots["Example"] = snapshot
        updater.process_handler = Mock()
        updater.process_handler.transactional_update_snapshots = {"Example"}
        updater.process_handler.setup_tracker = {"Example"}
        updater.process_handler.setup_tracker_lock = threading.Lock()
        updater.start_process = Mock(return_value=(False, "immediate exit"))
        config = {"process_name": "Example"}
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("example", None)
        config_manager.get_instance.return_value = config

        with (
            patch("utils.auto_update.CONFIG_MANAGER", config_manager),
            patch("utils.auto_update.configure_project", return_value=(True, None)),
        ):
            restored = updater._recover_pending_snapshot("Example")

        self.assertFalse(restored)
        snapshot.rollback.assert_called_once_with()
        updater.start_process.assert_called_once_with(
            "Example", config, "example", None
        )

    def test_start_process_preserves_immediate_exit_error_without_snapshot(self):
        updater = self._updater()
        updater.process_handler = Mock()
        updater.process_handler.start_process.return_value = (
            False,
            "Example failed to stay running.",
        )
        config = {
            "command": ["/example/bin"],
            "config_dir": "/example",
            "env": {},
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("example", None)
        config_manager.get_instance.return_value = config

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            process, error = updater.start_process("Example", config, "example", None)

        self.assertFalse(process)
        self.assertEqual("Example failed to stay running.", error)

    def test_runtime_recovery_requires_resolvable_service_configuration(self):
        updater = self._updater()
        snapshot = Mock()
        snapshot.rollback.return_value = True
        updater._rollback_snapshots["Example"] = snapshot
        updater.process_handler = Mock()
        updater.process_handler.transactional_update_snapshots = {"Example"}
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = (None, None)

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            restored = updater._recover_pending_snapshot("Example")

        self.assertFalse(restored)
        snapshot.rollback.assert_called_once_with()
        snapshot.commit.assert_called_once_with()

    def test_failed_rollback_health_is_not_reported_as_restored(self):
        updater = self._updater()
        updater._rollback_snapshots["Example"] = Mock()
        updater._wait_for_update_health = Mock(
            return_value=(False, "replacement probe failed")
        )
        updater._recover_pending_snapshot = Mock(return_value=False)

        process, error = updater._finalize_runtime_snapshot("Example", Mock())

        self.assertFalse(process)
        self.assertIn("previous runtime did not return to stable health", error)
        updater._recover_pending_snapshot.assert_called_once_with("Example")

    def test_update_health_allows_starting_probe_to_become_ready(self):
        updater = self._updater()
        updater._mark_update_service_ready = Mock()
        updater._mark_update_downtime_started = Mock()
        updater.process_handler = Mock()
        updater.process_handler.get_service_readiness.side_effect = [
            {
                "state": "starting",
                "health_status": "unhealthy",
                "reason": "Port 127.0.0.1:1234 not responding",
            },
            {
                "state": "ready",
                "health_status": "healthy",
                "reason": None,
            },
        ]
        config_manager = Mock()
        config_manager.get.return_value = {
            "install_cache": {
                "activation_health_timeout_seconds": 5,
                "activation_stabilization_seconds": 0,
            }
        }

        with (
            patch("utils.auto_update.CONFIG_MANAGER", config_manager),
            patch("utils.auto_update.time.sleep"),
        ):
            healthy, error = updater._wait_for_update_health("Example")

        self.assertTrue(healthy)
        self.assertIsNone(error)
        self.assertEqual(
            updater.process_handler.get_service_readiness.call_count,
            2,
        )
        updater._mark_update_service_ready.assert_called_once_with("Example")

    def test_update_timing_separates_install_time_from_observed_downtime(self):
        updater = self._updater()
        updater._write_update_status = Mock()
        updater._safe_record_update_status = Update._safe_record_update_status.__get__(
            updater, Update
        )
        payload = {"status": "updated", "message": "Updated Example."}

        with patch(
            "utils.auto_update.time.monotonic",
            side_effect=[100.0, 110.0, 114.0, 120.0, 123.0, 130.0],
        ):
            self.assertTrue(updater._begin_update_timing("Example"))
            updater._mark_update_downtime_started("Example")
            updater._mark_update_service_ready("Example")
            updater._mark_update_downtime_started("Example")
            updater._mark_update_service_ready("Example")
            updater._safe_record_update_status("Example", payload)
            metrics = updater._finish_update_timing("Example", payload)

        self.assertEqual(metrics["install_duration_seconds"], 30.0)
        self.assertEqual(metrics["downtime_seconds"], 7.0)
        self.assertEqual(metrics["downtime_status"], "completed")
        updater._write_update_status.assert_called_once_with("Example", payload)

    def test_update_timing_reports_unrecovered_downtime_as_ongoing(self):
        updater = self._updater()
        updater._write_update_status = Mock()
        payload = {"status": "error", "message": "Update failed."}

        with patch(
            "utils.auto_update.time.monotonic",
            side_effect=[10.0, 12.0, 20.0],
        ):
            updater._begin_update_timing("Example")
            updater._mark_update_downtime_started("Example")
            metrics = updater._finish_update_timing("Example", payload)

        self.assertEqual(metrics["install_duration_seconds"], 10.0)
        self.assertEqual(metrics["downtime_seconds"], 8.0)
        self.assertEqual(metrics["downtime_status"], "ongoing")
        updater._write_update_status.assert_called_once_with("Example", payload)

    def test_update_health_still_fails_immediately_when_process_exits(self):
        updater = self._updater()
        updater.process_handler = Mock()
        updater.process_handler.get_service_readiness.return_value = {
            "state": "failed",
            "reason": "Process exited during startup.",
        }
        config_manager = Mock()
        config_manager.get.return_value = {
            "install_cache": {
                "activation_health_timeout_seconds": 5,
                "activation_stabilization_seconds": 0,
            }
        }

        with (
            patch("utils.auto_update.CONFIG_MANAGER", config_manager),
            patch("utils.auto_update.time.sleep"),
        ):
            healthy, error = updater._wait_for_update_health("Example")

        self.assertFalse(healthy)
        self.assertEqual(error, "Process exited during startup.")
        updater.process_handler.get_service_readiness.assert_called_once_with("Example")

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

    def test_manual_update_install_records_in_progress_before_running_update(self):
        updater = self._updater()
        config = {
            "pinned_version": "",
            "commit_sha": "",
            "release_version_enabled": False,
            "release_version": "latest",
            "branch_enabled": False,
            "branch": "main",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("example", None)
        config_manager.get_instance.return_value = config

        def update_check(*_args):
            self.assertEqual(
                updater._write_update_status.call_args_list[-1].args[1]["status"],
                "installing",
            )
            return True, "Updated Example."

        updater.update_check = Mock(side_effect=update_check)

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            payload = updater.manual_update_install("Example")

        self.assertEqual(payload["status"], "updated")
        self.assertEqual(
            [
                call.args[1]["status"]
                for call in updater._write_update_status.call_args_list
            ],
            ["installing", "updated"],
        )

    def test_duplicate_manual_update_requests_share_one_install_result(self):
        updater = self._updater()
        config = {
            "pinned_version": "",
            "commit_sha": "",
            "release_version_enabled": False,
            "release_version": "latest",
            "branch_enabled": False,
            "branch": "main",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("example", None)
        config_manager.get_instance.return_value = config
        install_started = threading.Event()
        release_install = threading.Event()
        duplicate_waiting = threading.Event()
        results = []

        def update_check(*_args):
            install_started.set()
            self.assertTrue(release_install.wait(2))
            return True, "Updated Example."

        def observe_coalescing(message, *_args):
            if str(message).startswith("Coalescing duplicate"):
                duplicate_waiting.set()

        def run_install():
            results.append(updater.manual_update_install("Example"))

        updater.update_check = Mock(side_effect=update_check)
        updater.logger.info.side_effect = observe_coalescing

        with (
            patch("utils.auto_update.CONFIG_MANAGER", config_manager),
            patch(
                "utils.auto_update.INSTALL_CACHE.begin_operation",
                return_value="operation-id",
            ),
            patch("utils.auto_update.INSTALL_CACHE.update_operation"),
        ):
            first = threading.Thread(target=run_install)
            second = threading.Thread(target=run_install)
            first.start()
            self.assertTrue(install_started.wait(2))
            second.start()
            self.assertTrue(duplicate_waiting.wait(2))
            release_install.set()
            first.join(2)
            second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual([result["status"] for result in results], ["updated"] * 2)
        updater.update_check.assert_called_once()

    def test_lowercase_candidate_failure_is_recorded_as_error(self):
        updater = self._updater()
        config = {
            "pinned_version": "",
            "commit_sha": "",
            "release_version_enabled": False,
            "release_version": "latest",
            "branch_enabled": False,
            "branch": "main",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("dumb_frontend", None)
        config_manager.get_instance.return_value = config
        updater.update_check = Mock(
            return_value=(False, "Candidate activation failed; frontend restored.")
        )

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            payload = updater._manual_update_install_unprotected("DUMB Frontend")

        self.assertEqual(payload["status"], "error")
        updater._safe_record_update_status.assert_called_once_with(
            "DUMB Frontend", payload
        )

    def test_commit_sha_blocks_moving_update_target(self):
        updater = self._updater()

        self.assertEqual(
            "commit",
            updater._get_update_block_reason({"commit_sha": "a" * 40}),
        )

    def test_latest_release_is_a_moving_update_target(self):
        updater = self._updater()

        self.assertIsNone(
            updater._get_update_block_reason(
                {
                    "release_version_enabled": True,
                    "release_version": "latest",
                },
                "profilarr",
            )
        )
        self.assertEqual(
            "release",
            updater._get_update_block_reason(
                {
                    "release_version_enabled": True,
                    "release_version": "v1.1.4",
                },
                "profilarr",
            ),
        )

    def test_only_digit_free_nzbdav_release_tags_are_moving_channels(self):
        for release in ("dev", "lts", "edge", "release-candidate"):
            with self.subTest(release=release):
                self.assertTrue(
                    Update._is_nzbdav_named_release_channel(
                        "infinidysk",
                        {
                            "release_version_enabled": True,
                            "release_version": release,
                        },
                    )
                )

        for release in ("v0.9.5", "2026.08.03", "dev2", "latest"):
            with self.subTest(release=release):
                self.assertFalse(
                    Update._is_nzbdav_named_release_channel(
                        "infinidysk",
                        {
                            "release_version_enabled": True,
                            "release_version": release,
                        },
                    )
                )

        self.assertFalse(
            Update._is_nzbdav_named_release_channel(
                "decypharr",
                {"release_version_enabled": True, "release_version": "dev"},
            )
        )

    def test_commit_status_reports_whether_configured_target_is_installed(self):
        updater = self._updater()
        updater.auto_update_interval = Mock(return_value=24)
        updater.auto_update_start_time = Mock(return_value="04:00")
        commit_sha = "a" * 40
        config = {
            "commit_sha": commit_sha,
            "auto_update": True,
            "repo_owner": "infinidysk",
            "repo_name": "infinidysk",
        }

        with patch("utils.auto_update.Versions") as versions:
            versions.return_value.version_check.return_value = ("v0.8.1", None)
            pending = updater._manual_check_generic_repo(
                "InfiniDysk",
                config,
                "infinidysk",
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
                "InfiniDysk",
                config,
                "infinidysk",
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
        release_sha = "a" * 40
        updater.downloader.get_ref_commit_sha.return_value = (release_sha, None)
        config = {
            "release_version_enabled": True,
            "release_version": "v0.7.9",
            "repo_owner": "infinidysk",
            "repo_name": "infinidysk",
        }

        with patch("utils.auto_update.Versions") as versions:
            versions.return_value.version_check.return_value = ("v0.8.1", None)
            pending = updater._manual_check_generic_repo(
                "InfiniDysk",
                config,
                "infinidysk",
                None,
                "release",
                1,
                False,
                24,
                "04:00",
                None,
            )
            versions.return_value.version_check.return_value = (
                f"v0.7.9-{release_sha[:8]}",
                None,
            )
            installed = updater._manual_check_generic_repo(
                "InfiniDysk",
                config,
                "infinidysk",
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
        self.assertEqual("v0.7.9", installed["current_version"])
        self.assertTrue(installed["configured_target_installed"])
        versions.return_value.compare_versions.assert_not_called()

    def test_frontend_release_downgrade_reports_configured_target(self):
        updater = self._updater()
        config = {
            "release_version_enabled": True,
            "release_version": "v1.81.0",
            "repo_owner": "nicocapalbo",
            "repo_name": "dmbdb",
        }

        with patch("utils.auto_update.Versions") as versions:
            versions.return_value.version_check.return_value = ("v1.82.0", None)
            payload = updater._manual_check_generic_repo(
                "DUMB Frontend",
                config,
                "dumb_frontend",
                None,
                "release",
                1,
                False,
                24,
                "04:00",
                None,
            )

        self.assertEqual("blocked", payload["status"])
        self.assertEqual("v1.81.0", payload["available_version"])
        self.assertEqual("release", payload["configured_target_kind"])
        self.assertFalse(payload["configured_target_installed"])
        versions.return_value.compare_versions.assert_not_called()

    def test_frontend_branch_reports_configured_target(self):
        updater = self._updater()
        updater.downloader.get_ref_commit_sha.return_value = ("a" * 40, None)
        config = {
            "branch_enabled": True,
            "branch": "dev",
            "repo_owner": "nicocapalbo",
            "repo_name": "dmbdb",
        }

        with patch("utils.auto_update.Versions") as versions:
            versions.return_value.version_check.return_value = ("v1.82.0", None)
            payload = updater._manual_check_generic_repo(
                "DUMB Frontend",
                config,
                "dumb_frontend",
                None,
                "branch",
                1,
                False,
                24,
                "04:00",
                None,
            )

        self.assertEqual("blocked", payload["status"])
        self.assertEqual("dev-aaaaaaaa", payload["available_version"])
        self.assertEqual("branch", payload["configured_target_kind"])
        self.assertFalse(payload["configured_target_installed"])
        versions.return_value.compare_versions.assert_not_called()

    def test_frontend_branch_marker_reports_configured_target_installed(self):
        updater = self._updater()
        updater.downloader.get_ref_commit_sha.return_value = ("a" * 40, None)
        config = {
            "branch_enabled": True,
            "branch": "dev",
            "repo_owner": "nicocapalbo",
            "repo_name": "dmbdb",
        }

        with patch("utils.auto_update.Versions") as versions:
            versions.return_value.version_check.return_value = (
                "dev-aaaaaaaa",
                None,
            )
            payload = updater._manual_check_generic_repo(
                "DUMB Frontend",
                config,
                "dumb_frontend",
                None,
                "branch",
                1,
                False,
                24,
                "04:00",
                None,
            )

        self.assertEqual("no_update", payload["status"])
        self.assertEqual("dev-aaaaaaaa", payload["current_version"])
        self.assertEqual("dev-aaaaaaaa", payload["available_version"])
        self.assertEqual("branch", payload["configured_target_kind"])
        self.assertTrue(payload["configured_target_installed"])
        versions.return_value.compare_versions.assert_not_called()

    def test_moving_nzbdav_release_tag_reports_changed_commit(self):
        updater = self._updater()
        new_sha = "b" * 40
        updater.downloader.get_ref_commit_sha.return_value = (new_sha, None)
        config = {
            "release_version_enabled": True,
            "release_version": "dev",
            "repo_owner": "infinidysk",
            "repo_name": "infinidysk",
        }

        with patch("utils.auto_update.Versions") as versions:
            versions.return_value.version_check.return_value = (
                "dev-aaaaaaaa",
                None,
            )
            payload = updater._manual_check_generic_repo(
                "InfiniDysk",
                config,
                "infinidysk",
                None,
                None,
                1,
                False,
                24,
                "04:00",
                None,
            )

        self.assertEqual("update_available", payload["status"])
        self.assertIsNone(payload["reason"])
        self.assertFalse(payload["configured_target_installed"])
        self.assertEqual("dev-bbbbbbbb", payload["available_version"])

    def test_mediastorm_status_uses_oci_digest_for_latest(self):
        updater = self._updater()
        config = {"release_version": "latest"}
        target = {
            "selector": "latest",
            "current_version": "v1.5.0-20260806",
            "current_digest": "sha256:" + "a" * 64,
            "available_digest": "sha256:" + "b" * 64,
            "installed": False,
        }

        with patch("utils.auto_update.mediastorm_target_status", return_value=target):
            payload = updater._manual_check_mediastorm(
                "mediastorm",
                config,
                None,
                1,
                True,
                24,
                "04:00",
                2,
            )

        self.assertEqual("update_available", payload["status"])
        self.assertEqual("v1.5.0-20260806", payload["current_version"])
        self.assertEqual("latest@bbbbbbbbbbbb", payload["available_version"])

    def test_mediastorm_pinned_oci_target_remains_blocked(self):
        updater = self._updater()
        target = {
            "selector": "1.5.0",
            "current_version": "v1.4.0-20260701",
            "current_digest": "sha256:" + "a" * 64,
            "available_digest": "sha256:" + "b" * 64,
            "installed": False,
        }

        with patch("utils.auto_update.mediastorm_target_status", return_value=target):
            payload = updater._manual_check_mediastorm(
                "mediastorm",
                {"release_version_enabled": True, "release_version": "1.5.0"},
                "release",
                1,
                False,
                24,
                "04:00",
                None,
            )

        self.assertEqual("blocked", payload["status"])
        self.assertEqual("1.5.0", payload["available_version"])
        self.assertFalse(payload["configured_target_installed"])

    def test_mediastorm_update_installs_changed_latest_oci_digest(self):
        updater = self._updater()
        updater.process_handler = Mock(
            process_names=[],
            setup_tracker={"mediastorm"},
            setup_tracker_lock=threading.Lock(),
        )
        updater.start_process = Mock(return_value=("started", None))
        target = {
            "selector": "latest",
            "current_version": "v1.5.0-20260806",
            "current_digest": "sha256:" + "a" * 64,
            "available_digest": "sha256:" + "b" * 64,
            "installed": False,
        }
        config = {"release_version": "latest"}

        with (
            patch("utils.auto_update.mediastorm_target_status", return_value=target),
            patch(
                "utils.auto_update.setup_release_version", return_value=(True, None)
            ) as install,
            patch("utils.auto_update.setup_project", return_value=(True, None)),
            patch("utils.auto_update.Versions") as versions,
        ):
            versions.return_value.version_check.return_value = (
                "v1.5.0-20260807",
                None,
            )
            success, message = updater.update_check_mediastorm(
                "mediastorm", config, "mediastorm", None
            )

        self.assertTrue(success)
        self.assertIn("v1.5.0-20260807", message)
        install.assert_called_once_with(
            updater.process_handler, config, "mediastorm", "mediastorm"
        )
        self.assertNotIn("mediastorm", updater.process_handler.setup_tracker)

    def test_direct_update_installs_changed_nzbdav_release_tag_commit(self):
        updater = self._updater()
        updater.process_handler = Mock(
            process_names=[],
            setup_tracker=set(),
            setup_tracker_lock=threading.Lock(),
        )
        updater.start_process = Mock(return_value=("started", None))
        release_sha = "c" * 40
        updater.downloader.get_ref_commit_sha.return_value = (release_sha, None)
        config = {
            "release_version_enabled": True,
            "release_version": "dev",
            "repo_owner": "infinidysk",
            "repo_name": "infinidysk",
        }

        with (
            patch("utils.auto_update.Versions") as versions,
            patch(
                "utils.auto_update.setup_release_version", return_value=(True, None)
            ) as install_release,
            patch(
                "utils.auto_update.setup_project", return_value=(True, None)
            ) as finish_setup,
        ):
            versions.return_value.version_check.return_value = (
                "dev-bbbbbbbb",
                None,
            )
            success, message = updater.update_check(
                "InfiniDysk", config, "infinidysk", None
            )

        self.assertTrue(success, message)
        self.assertIn("dev-cccccccc", message)
        self.assertEqual("dev", config["release_version"])
        install_release.assert_called_once_with(
            updater.process_handler, config, "InfiniDysk", "infinidysk"
        )
        finish_setup.assert_called_once_with(updater.process_handler, "InfiniDysk")
        updater.start_process.assert_called_once_with(
            "InfiniDysk", config, "infinidysk", None
        )

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
        config_manager.find_key_for_process.return_value = ("infinidysk", None)
        config_manager.get_instance.return_value = {
            "process_name": "InfiniDysk",
            "auto_update": True,
            "commit_sha": "a" * 40,
            "release_version_enabled": True,
            "release_version": "prerelease",
        }
        existing_job = object()
        Update._jobs = {"InfiniDysk": existing_job}
        Update._next_check_at = {"InfiniDysk": 123}

        with (
            patch("utils.auto_update.CONFIG_MANAGER", config_manager),
            patch("utils.auto_update.setup_project", return_value=(True, None)),
            patch("utils.auto_update.threading.Thread") as update_thread,
        ):
            process, error = updater.auto_update("InfiniDysk", enable_update=True)

        self.assertEqual("started", process)
        self.assertIsNone(error)
        updater.initial_update_check.assert_not_called()
        update_thread.assert_not_called()
        updater.scheduler.cancel_job.assert_called_once_with(existing_job)
        self.assertNotIn("InfiniDysk", Update._jobs)
        self.assertNotIn("InfiniDysk", Update._next_check_at)

    def test_digit_free_nzbdav_release_tag_allows_auto_update_schedule(self):
        updater = self._updater()
        updater.process_handler = Mock(
            preinstall_complete=False,
            preinstalled_processes=set(),
        )
        updater.reschedule_symlink_backup = Mock()
        updater.initial_update_check = Mock(return_value=(True, None))
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("infinidysk", None)
        config_manager.get_instance.return_value = {
            "process_name": "InfiniDysk",
            "auto_update": True,
            "release_version_enabled": True,
            "release_version": "dev",
            "commit_sha": "",
            "branch_enabled": False,
        }

        with (
            patch("utils.auto_update.CONFIG_MANAGER", config_manager),
            patch("utils.auto_update.threading.Thread") as update_thread,
        ):
            process, error = updater.auto_update("InfiniDysk", enable_update=True)

        self.assertTrue(process)
        self.assertIsNone(error)
        update_thread.assert_called_once_with(
            target=updater.update_schedule,
            args=(
                "InfiniDysk",
                config_manager.get_instance.return_value,
                "infinidysk",
                None,
            ),
        )
        update_thread.return_value.start.assert_called_once_with()
        updater.initial_update_check.assert_called_once()

    def test_numeric_nzbdav_release_tag_keeps_auto_updates_disabled(self):
        updater = self._updater()
        updater.process_handler = Mock(
            preinstall_complete=False,
            preinstalled_processes=set(),
        )
        updater.reschedule_symlink_backup = Mock()
        updater.start_process = Mock(return_value=("started", None))
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("infinidysk", None)
        config_manager.get_instance.return_value = {
            "process_name": "InfiniDysk",
            "auto_update": True,
            "release_version_enabled": True,
            "release_version": "v0.9.5",
            "commit_sha": "",
            "branch_enabled": False,
        }

        with (
            patch("utils.auto_update.CONFIG_MANAGER", config_manager),
            patch("utils.auto_update.setup_project", return_value=(True, None)),
            patch("utils.auto_update.threading.Thread") as update_thread,
        ):
            process, error = updater.auto_update("InfiniDysk", enable_update=True)

        self.assertEqual("started", process)
        self.assertIsNone(error)
        update_thread.assert_not_called()

    def test_reschedule_allows_digit_free_nzbdav_release_tag(self):
        updater = self._updater()
        updater.update_schedule = Mock()
        config = {
            "auto_update": True,
            "release_version_enabled": True,
            "release_version": "dev",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("infinidysk", None)
        config_manager.get_instance.return_value = config

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            success, message = updater.reschedule_auto_update("InfiniDysk")

        self.assertTrue(success)
        self.assertEqual("Auto-update rescheduled", message)
        updater.update_schedule.assert_called_once_with(
            "InfiniDysk", config, "infinidysk", None
        )

    def test_reschedule_blocks_numeric_nzbdav_release_tag(self):
        updater = self._updater()
        existing_job = object()
        Update._jobs = {"InfiniDysk": existing_job}
        Update._next_check_at = {"InfiniDysk": 123}
        updater.update_schedule = Mock()
        updater.auto_update_interval = Mock(return_value=24)
        updater.auto_update_start_time = Mock(return_value="04:00")
        config = {
            "auto_update": True,
            "release_version_enabled": True,
            "release_version": "v0.9.5",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("infinidysk", None)
        config_manager.get_instance.return_value = config

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            success, message = updater.reschedule_auto_update("InfiniDysk")

        self.assertTrue(success)
        self.assertEqual("Auto-update disabled by release v0.9.5", message)
        updater.scheduler.cancel_job.assert_called_once_with(existing_job)
        updater.update_schedule.assert_not_called()
        status = updater._safe_record_update_status.call_args.args[1]
        self.assertEqual("blocked", status["status"])
        self.assertEqual("release", status["reason"])

    def test_reschedule_cancels_existing_job_for_commit_pin(self):
        updater = self._updater()
        existing_job = object()
        Update._jobs = {"InfiniDysk": existing_job}
        Update._next_check_at = {"InfiniDysk": 123}
        updater.update_schedule = Mock()
        updater.auto_update_interval = Mock(return_value=24)
        updater.auto_update_start_time = Mock(return_value="04:00")
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("infinidysk", None)
        config_manager.get_instance.return_value = {
            "auto_update": True,
            "commit_sha": "a" * 40,
        }

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            success, message = updater.reschedule_auto_update("InfiniDysk")

        self.assertTrue(success)
        self.assertEqual("Auto-update disabled by commit pin", message)
        updater.scheduler.cancel_job.assert_called_once_with(existing_job)
        updater.update_schedule.assert_not_called()
        self.assertNotIn("InfiniDysk", Update._jobs)
        self.assertNotIn("InfiniDysk", Update._next_check_at)
        status = updater._safe_record_update_status.call_args.args[1]
        self.assertEqual("blocked", status["status"])
        self.assertEqual("commit", status["reason"])
        self.assertFalse(status["auto_update_enabled"])

    def test_stale_scheduled_callback_stops_when_commit_pin_is_detected(self):
        updater = self._updater()
        existing_job = object()
        Update._jobs = {"InfiniDysk": existing_job}
        Update._next_check_at = {"InfiniDysk": 123}
        updater.scheduled_update_check = Mock()
        config_manager = Mock()
        config_manager.get_instance.return_value = {
            "auto_update": True,
            "commit_sha": "a" * 40,
        }

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            updater._run_scheduled_update_if_due(
                "InfiniDysk",
                {"auto_update": True},
                "infinidysk",
                None,
            )

        updater.scheduler.cancel_job.assert_called_once_with(existing_job)
        updater.scheduled_update_check.assert_not_called()
        self.assertNotIn("InfiniDysk", Update._jobs)
        self.assertNotIn("InfiniDysk", Update._next_check_at)

    def test_scheduled_check_only_reports_update_without_installing(self):
        updater = self._updater()
        pending = {
            "status": "update_available",
            "current_version": "1.0.0",
            "available_version": "1.1.0",
        }
        updater._manual_update_check_internal = Mock(return_value=pending)
        updater._scheduled_update_check_unprotected = Mock()

        result = updater.scheduled_update_check(
            "Radarr",
            {"auto_update": True, "auto_update_mode": "check_only"},
            "radarr",
            None,
        )

        self.assertEqual(pending, result)
        updater._safe_record_update_status.assert_called_once_with("Radarr", pending)
        updater._scheduled_update_check_unprotected.assert_not_called()

    def test_due_check_only_schedule_preserves_pending_dashboard_status(self):
        updater = self._updater()
        updater.auto_update_interval = Mock(return_value=24)
        updater.auto_update_start_time = Mock(return_value="04:00")
        updater._calculate_next_check_at = Mock(return_value=200)
        updater.scheduled_update_check = Mock(
            return_value={
                "status": "update_available",
                "current_version": "1.0.0",
                "available_version": "1.1.0",
                "checked_at": 100,
            }
        )
        config = {
            "auto_update": True,
            "auto_update_mode": "check_only",
            "auto_update_interval": 24,
            "auto_update_start_time": "04:00",
        }
        config_manager = Mock()
        config_manager.get_instance.return_value = config
        Update._next_check_at = {"Radarr": 100}

        with (
            patch("utils.auto_update.CONFIG_MANAGER", config_manager),
            patch("utils.auto_update.time.time", return_value=100),
        ):
            updater._run_scheduled_update_if_due("Radarr", config, "radarr", None)

        status = updater._safe_record_update_status.call_args.args[1]
        self.assertEqual("update_available", status["status"])
        self.assertEqual("1.1.0", status["available_version"])
        self.assertEqual("check_only", status["auto_update_mode"])
        self.assertEqual(200, status["next_check_at"])

    def test_initial_check_only_does_not_call_installing_update_check(self):
        updater = self._updater()
        updater.process_handler = Mock()
        updater._manual_update_check_internal = Mock(
            return_value={"status": "update_available"}
        )
        updater.update_check = Mock()
        updater.start_process = Mock(return_value=("started", None))
        config = {"auto_update_mode": "check_only"}

        with patch("utils.auto_update.configure_project", return_value=(True, None)):
            process, error = updater.initial_update_check(
                "Radarr", config, "radarr", None
            )

        self.assertEqual("started", process)
        self.assertIsNone(error)
        updater.update_check.assert_not_called()
        updater.start_process.assert_called_once_with("Radarr", config, "radarr", None)

    def test_auto_update_mode_defaults_to_install_for_existing_configs(self):
        self.assertEqual("install", Update.auto_update_mode({}))
        self.assertEqual(
            "install", Update.auto_update_mode({"auto_update_mode": "bad"})
        )
        self.assertEqual(
            "check_only", Update.auto_update_mode({"auto_update_mode": "check_only"})
        )

    def test_api_update_check_is_always_check_only(self):
        updater = self._updater()
        updater._manual_update_check_internal = Mock(
            return_value={
                "status": "update_available",
                "current_version": "2.1.0",
                "available_version": "2.3.0",
                "releases_behind": 2,
            }
        )
        updater._calculate_next_run_at = Mock(return_value=500)
        config_manager = Mock()
        config_manager.get_instance.return_value = {
            "process_name": "DUMB API",
            "auto_update": True,
        }

        with (
            patch("utils.auto_update.CONFIG_MANAGER", config_manager),
            patch("utils.auto_update.time.time", return_value=100),
        ):
            payload = updater.check_api_update("DUMB API")

        checked_config = updater._manual_update_check_internal.call_args.args[1]
        self.assertFalse(checked_config["auto_update"])
        self.assertEqual("check_only", checked_config["auto_update_mode"])
        self.assertTrue(payload["update_check_required"])
        self.assertFalse(payload["auto_update_enabled"])
        self.assertEqual(2, payload["releases_behind"])
        self.assertEqual(500, payload["next_check_at"])

    def test_scheduled_api_check_failure_does_not_stop_scheduler_loop(self):
        updater = self._updater()
        updater.check_api_update = Mock(side_effect=RuntimeError("GitHub unavailable"))
        updater._calculate_next_run_at = Mock(return_value=500)
        Update._api_next_check_at = 100

        with patch("utils.auto_update.time.time", return_value=100):
            updater._run_api_update_check_if_due("DUMB API")

        self.assertEqual(500, Update._api_next_check_at)
        updater.logger.warning.assert_called_once()

    def test_direct_update_check_never_resolves_latest_for_commit_pin(self):
        updater = self._updater()
        updater.process_handler = Mock()

        with patch("utils.auto_update.Versions") as versions:
            success, message = updater.update_check(
                "InfiniDysk",
                {
                    "commit_sha": "a" * 40,
                    "release_version_enabled": False,
                    "release_version": "latest",
                },
                "infinidysk",
                None,
            )

        self.assertFalse(success)
        self.assertIn("pinned to commit aaaaaaaaaaaa", message)
        versions.assert_not_called()

    def test_manual_override_preserves_commit_saved_while_install_is_running(self):
        updater = self._updater()
        commit_sha = "b" * 40
        config = {
            "repo_owner": "infinidysk",
            "repo_name": "infinidysk",
            "pinned_version": "",
            "commit_sha": "",
            "release_version_enabled": False,
            "release_version": "latest",
            "branch_enabled": True,
            "branch": "main",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("infinidysk", None)
        config_manager.get_instance.return_value = config

        def install_while_source_changes(*_args):
            config.update(
                {
                    "commit_sha": commit_sha,
                    "release_version_enabled": False,
                    "branch_enabled": False,
                }
            )
            return True, "Updated InfiniDysk."

        updater.update_check = Mock(side_effect=install_while_source_changes)

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            payload = updater.manual_update_install("InfiniDysk", allow_override=True)

        self.assertEqual("updated", payload["status"])
        self.assertEqual(commit_sha, config["commit_sha"])
        self.assertFalse(config["release_version_enabled"])
        self.assertFalse(config["branch_enabled"])
        updater.logger.info.assert_any_call(
            "Preserving newer source selection for %s saved during manual update.",
            "InfiniDysk",
        )
        updater.process_handler = Mock()
        with patch("utils.auto_update.Versions") as versions:
            success, message = Update.update_check(
                updater,
                "InfiniDysk",
                config,
                "infinidysk",
                None,
            )
        self.assertFalse(success)
        self.assertIn("pinned to commit bbbbbbbbbbbb", message)
        versions.assert_not_called()

    def test_manual_latest_install_preserves_commit_saved_while_running(self):
        updater = self._updater()
        commit_sha = "c" * 40
        config = {
            "repo_owner": "infinidysk",
            "repo_name": "infinidysk",
            "pinned_version": "",
            "commit_sha": "",
            "release_version_enabled": False,
            "release_version": "latest",
            "branch_enabled": False,
            "branch": "main",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("infinidysk", None)
        config_manager.get_instance.return_value = config

        def install_while_source_changes(*_args):
            config["commit_sha"] = commit_sha
            return True, "Updated InfiniDysk."

        updater.update_check = Mock(side_effect=install_while_source_changes)

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            payload = updater.manual_update_install("InfiniDysk")

        self.assertEqual("updated", payload["status"])
        self.assertEqual(commit_sha, config["commit_sha"])
        updater.logger.info.assert_any_call(
            "Preserving newer source selection for %s saved during manual update.",
            "InfiniDysk",
        )

    def test_manual_latest_override_temporarily_ignores_branch_selection(self):
        updater = self._updater()
        config = {
            "repo_owner": "infinidysk",
            "repo_name": "infinidysk",
            "pinned_version": "",
            "commit_sha": "",
            "release_version_enabled": True,
            "release_version": "prerelease",
            "branch_enabled": True,
            "branch": "main",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("infinidysk", None)
        config_manager.get_instance.return_value = config

        def assert_latest_selection(*_args):
            self.assertFalse(config["branch_enabled"])
            self.assertFalse(config["release_version_enabled"])
            self.assertEqual("", config["commit_sha"])
            return True, "Updated InfiniDysk to latest stable release."

        updater.update_check = Mock(side_effect=assert_latest_selection)

        with patch("utils.auto_update.CONFIG_MANAGER", config_manager):
            payload = updater.manual_update_install("InfiniDysk", allow_override=True)

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
            "repo_owner": "infinidysk",
            "repo_name": "infinidysk",
            "commit_sha": commit_sha,
            "release_version_enabled": False,
            "release_version": "latest",
            "branch_enabled": False,
            "branch": "main",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("infinidysk", None)
        config_manager.get_instance.return_value = config

        with (
            patch("utils.auto_update.CONFIG_MANAGER", config_manager),
            patch(
                "utils.auto_update.setup_project", return_value=(True, None)
            ) as setup,
        ):
            payload = updater.manual_update_install(
                "InfiniDysk",
                allow_override=False,
                target="configured",
            )

        self.assertEqual("updated", payload["status"])
        self.assertEqual(commit_sha, config["commit_sha"])
        setup.assert_called_once_with(updater.process_handler, "InfiniDysk")
        updater.update_check.assert_not_called()
        updater.start_process.assert_called_once_with(
            "InfiniDysk",
            config,
            "infinidysk",
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
            "repo_owner": "infinidysk",
            "repo_name": "infinidysk",
            "commit_sha": "",
            "release_version_enabled": True,
            "release_version": "v0.7.9",
            "branch_enabled": False,
            "branch": "main",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("infinidysk", None)
        config_manager.get_instance.return_value = config

        with (
            patch("utils.auto_update.CONFIG_MANAGER", config_manager),
            patch(
                "utils.auto_update.setup_project", return_value=(True, None)
            ) as setup,
        ):
            payload = updater.manual_update_install(
                "InfiniDysk",
                allow_override=False,
                target="configured",
            )

        self.assertEqual("updated", payload["status"])
        self.assertTrue(config["release_version_enabled"])
        self.assertEqual("v0.7.9", config["release_version"])
        setup.assert_called_once_with(updater.process_handler, "InfiniDysk")
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
                "InfiniDysk", "infinidysk", None, config
            )
        )

        versions.return_value.version_check.return_value = ("commit-bbbbbbbbbbbb", None)
        self.assertTrue(
            updater._should_run_install_phase_for_preinstalled(
                "InfiniDysk", "infinidysk", None, config
            )
        )

    @patch("utils.auto_update.Versions")
    def test_preinstalled_nzbdav_release_tag_compares_commit_marker(self, versions):
        updater = self._updater()
        release_sha = "c" * 40
        updater.downloader.get_ref_commit_sha.return_value = (release_sha, None)
        config = {
            "release_version_enabled": True,
            "release_version": "dev",
            "repo_owner": "infinidysk",
            "repo_name": "infinidysk",
        }
        versions.return_value.version_check.return_value = (
            f"dev-{release_sha[:8]}",
            None,
        )

        self.assertFalse(
            updater._should_run_install_phase_for_preinstalled(
                "InfiniDysk", "infinidysk", None, config
            )
        )

        versions.return_value.version_check.return_value = ("dev-aaaaaaaa", None)
        self.assertTrue(
            updater._should_run_install_phase_for_preinstalled(
                "InfiniDysk", "infinidysk", None, config
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
