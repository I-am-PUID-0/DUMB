"""Managed native updates preserve admission, release choices and runtime state."""

from contextlib import ExitStack, contextmanager
import copy
from functools import wraps
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


def isolated_update_test(method):
    """Keep legacy discovery's permanent dependency doubles out of integration."""

    @wraps(method)
    def isolated(self):
        test_id = f"tests.{Path(__file__).stem}.{type(self).__name__}.{method.__name__}"
        if os.environ.get("DUMB_NATIVE_UPDATE_TEST_ID") == test_id:
            return method(self)
        # Executable and test selector come only from this test module.
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-B", "-m", "unittest", test_id],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "DUMB_NATIVE_UPDATE_TEST_ID": test_id},
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    return isolated


class NativeUpdateIntegrationTests(unittest.TestCase):
    def fixture(self, *, running=False, selected="latest"):
        # Imports belong inside the isolated child, after test discovery.
        from utils.auto_update import Update
        from utils import infinidysk_v2_migration, infinidysk_v2_postgres

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.candidate = self.root / "candidate"
        self.candidate.mkdir()
        self.events = []
        self.config = {
            "enabled": True,
            "process_name": "InfiniDysk",
            "runtime_generation": 2,
            "repo_owner": "infinidysk",
            "repo_name": "infinidysk",
            "v2_runtime_dir": str(self.runtime),
            "v2_data_dir": str(self.root / "data"),
            "release_version": selected,
            "release_version_enabled": selected != "latest",
            "config_dir": str(self.root / "legacy"),
            "mount_backend": "rclone",
            "auto_update": True,
            "env": {},
        }
        self.original = copy.deepcopy(self.config)
        self.manager = Mock()
        self.manager.file_path = str(self.root / "dumb_config.json")
        self.manager.config = {
            "infinidysk": self.config,
            "data_root": str(self.root),
            "puid": os.geteuid(),
            "pgid": os.getegid(),
        }
        self.manager.find_key_for_process.return_value = ("infinidysk", None)
        self.manager.get_instance.return_value = self.config
        self.manager.get.side_effect = (
            lambda key, default=None: self.manager.config.get(key, default)
        )
        self.updater = object.__new__(Update)
        self.updater.logger = Mock()
        self.updater.downloader = Mock()
        self.updater.scheduler = Mock()
        self.updater.updating = threading.Lock()
        self.updater._rollback_snapshots = {}
        self.updater._active_install_operation = None
        self.updater._mark_update_downtime_started = Mock()
        self.updater._write_update_status = Mock()
        self.updater._safe_record_update_status = Mock()
        self.updater.process_handler = Mock()
        self.updater.process_handler.setup_tracker = {"InfiniDysk"}
        self.updater.process_handler.setup_tracker_lock = threading.Lock()
        self.updater.process_handler.processes = {}
        self.updater.process_handler.process_names = set()
        self.updater.process_handler.start_prepared_process.return_value = (
            SimpleNamespace(returncode=None),
            None,
        )

        def stop(name, **kwargs):
            self.events.append("stop")
            self.updater.process_handler.process_names.discard(name)
            self.updater.process_handler.processes = {}

        self.updater.process_handler.stop_process.side_effect = stop
        if running:
            process = SimpleNamespace(returncode=None, poll=lambda: None)
            self.updater.process_handler.processes[1] = {
                "name": "InfiniDysk",
                "process_obj": process,
            }
            self.updater.process_handler.process_names.add("InfiniDysk")
        self.updater.start_process = Mock(return_value=("started", None))
        self.updater._wait_for_update_health = Mock(return_value=(True, None))
        self.updater._native_provider_update_blocker = Mock(return_value=None)
        self.major = infinidysk_v2_migration.INFINIDYSK_V2_MIGRATION_MANAGER
        self.pg = infinidysk_v2_postgres.INFINIDYSK_V2_POSTGRES_MANAGER
        self.metadata = {
            "format": "official-v2-release-v1",
            "release": "v2.0.0-beta.10",
            "commit": "a" * 40,
            "architecture": "arm64",
            "archive_sha256": "b" * 64,
            "binary_sha256": "c" * 64,
        }
        self.plan = {
            "release": self.metadata["release"],
            "commit": self.metadata["commit"],
            "architecture": self.metadata["architecture"],
            "asset_name": "infinidysk-v2.0.0-beta.10-linux-arm64.tar.gz",
            "archive_url": "https://github.com/infinidysk/infinidysk/releases/download/v2.0.0-beta.10/infinidysk-v2.0.0-beta.10-linux-arm64.tar.gz",
            "archive_sha256": self.metadata["archive_sha256"],
            "archive_size": 1234,
        }
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

        def mocked(target, **kwargs):
            return self.stack.enter_context(patch(target, **kwargs))

        self.patches = {
            "config": mocked("utils.auto_update.CONFIG_MANAGER", new=self.manager),
            "installed": mocked(
                "utils.infinidysk_v2_runtime.installed_active_v2_version",
                return_value="v2.0.0-beta.6",
            ),
            "resolve": mocked(
                "utils.infinidysk_v2_runtime.resolve_v2_release",
                return_value=self.plan,
            ),
            "journal": mocked(
                "utils.infinidysk_v2_update.native_update_state", return_value=None
            ),
            "perform": mocked(
                "utils.infinidysk_v2_update.perform_native_update",
                side_effect=self.perform,
            ),
            "resume": mocked(
                "utils.infinidysk_v2_update.resume_native_update",
                side_effect=self.resume,
            ),
            "stage": mocked(
                "utils.infinidysk_v2_runtime.stage_v2_runtime_update",
                side_effect=self.stage,
            ),
            "configure": mocked(
                "utils.infinidysk_v2_runtime.configure_v2_runtime",
                return_value=(True, None),
            ),
        }
        for prefix, manager in (("major", self.major), ("pg", self.pg)):
            self.patches[prefix + "_blocker"] = self.stack.enter_context(
                patch.object(manager, "runtime_update_blocker", return_value=None)
            )
            self.patches[prefix + "_retire"] = self.stack.enter_context(
                patch.object(manager, "retire_runtime_rollback")
            )
        self.patches["unlock"] = self.stack.enter_context(
            patch.object(self.major, "unlock_runtime_updates")
        )
        return self.updater

    @contextmanager
    def stage(self, service, plan, *, uid, gid):
        self.events.append("stage")
        yield self.candidate, self.metadata
        self.events.append("stage_closed")

    def perform(self, service, directory, metadata, **callbacks):
        self.assertEqual(Path(directory), self.candidate)
        self.assertEqual(metadata, self.metadata)
        receipt = {
            "state": "completed",
            "release_metadata": self.metadata,
            "release": self.metadata["release"],
            "was_running": callbacks.get("was_running", False),
            "launched": True,
        }
        for name in ("verify_admission", "stop_current", "prepare_launch"):
            self.events.append(name)
            callbacks[name]()
        self.events.append("retire_rollback")
        callbacks["retire_rollback"](receipt)
        for name in ("start_candidate", "wait_ready"):
            self.events.append(name)
            callbacks[name]()
        self.events.append("save_selection")
        callbacks["save_selection"](metadata)
        return receipt

    def resume(self, service, **callbacks):
        for name in (
            "verify_admission",
            "prepare_launch",
            "start_candidate",
            "wait_ready",
        ):
            self.events.append("resume_" + name)
            callbacks[name]()
        callbacks["save_selection"](self.metadata)
        return {"state": "completed", "release_metadata": self.metadata}

    def update(self, **kwargs):
        return self.updater._update_native_runtime(
            "InfiniDysk", self.config, "infinidysk", None, **kwargs
        )

    @isolated_update_test
    def test_status_resolves_official_release_without_mutating_runtime_or_managers(
        self,
    ):
        updater = self.fixture(running=True)
        payload = updater._native_runtime_update_status(
            "InfiniDysk",
            self.config,
            checked_at=100,
            interval_hours=24,
            start_time="04:00",
            next_check_at=200,
        )
        self.assertEqual(payload["status"], "update_available")
        self.assertEqual(payload["current_version"], "v2.0.0-beta.6")
        self.assertEqual(payload["available_version"], "v2.0.0-beta.10")
        self.assertEqual(self.config, self.original)
        for name in (
            "stage",
            "perform",
            "resume",
            "unlock",
            "major_retire",
            "pg_retire",
        ):
            self.patches[name].assert_not_called()
        updater.process_handler.stop_process.assert_not_called()
        updater.start_process.assert_not_called()
        self.manager.save_config.assert_not_called()

    @isolated_update_test
    def test_disabled_release_control_ignores_dormant_v1_value(self):
        from utils.infinidysk_v2_releases import selected_release

        updater = self.fixture()
        self.config.update(release_version="rc", release_version_enabled=False)
        self.assertIsNone(updater._get_update_block_reason(self.config, "infinidysk"))
        self.assertEqual(selected_release(self.config), "latest")
        payload = updater._native_runtime_update_status("InfiniDysk", self.config)
        self.assertEqual(payload["status"], "update_available")
        self.assertTrue(payload["auto_update_enabled"])
        self.assertNotIn("configured_target_value", payload)
        self.patches["resolve"].assert_called_with(self.config)

    @isolated_update_test
    def test_invalid_release_flag_is_a_status_error_before_network_or_mutation(self):
        updater = self.fixture()
        self.config["release_version_enabled"] = "true"
        payload = updater._native_runtime_update_status("InfiniDysk", self.config)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["reason"], "unsupported_source")
        for name in ("resolve", "stage", "perform", "resume"):
            self.patches[name].assert_not_called()
        updater.process_handler.stop_process.assert_not_called()
        self.manager.save_config.assert_not_called()

    @isolated_update_test
    def test_configured_release_does_not_silently_follow_latest(self):
        self.fixture(selected="v2.0.0-beta.7")
        self.plan["release"] = "v2.0.0-beta.7"
        self.metadata["release"] = "v2.0.0-beta.7"
        payload = self.update(manual=True, target="configured")
        self.assertEqual(payload["status"], "updated")
        resolved = self.patches["resolve"].call_args.args[0]
        self.assertEqual(resolved["release_version"], "v2.0.0-beta.7")
        self.assertEqual(self.config["release_version"], "v2.0.0-beta.7")

    @isolated_update_test
    def test_explicit_latest_override_uses_latest_without_early_config_mutation(self):
        self.fixture(selected="v2.0.0-beta.7")

        def resolved(service):
            self.assertEqual(service["release_version"], "latest")
            self.assertFalse(service["release_version_enabled"])
            self.assertEqual(self.config["release_version"], "v2.0.0-beta.7")
            return self.plan

        self.patches["resolve"].side_effect = resolved
        payload = self.update(allow_override=True, manual=True)
        self.assertEqual(payload["status"], "updated")

    @isolated_update_test
    def test_equal_or_older_release_never_stages_or_stops(self):
        updater = self.fixture(running=True)
        for release in ("v2.0.0-beta.6", "v2.0.0-beta.5"):
            with self.subTest(release=release):
                self.plan["release"] = release
                payload = self.update(manual=True)
                self.assertEqual(
                    payload["status"],
                    "no_update" if release == "v2.0.0-beta.6" else "error",
                )
                if release == "v2.0.0-beta.5":
                    self.assertEqual(payload["reason"], "native_downgrade_unsupported")
                self.patches["stage"].assert_not_called()
                self.patches["perform"].assert_not_called()
                updater.process_handler.stop_process.assert_not_called()
                updater.start_process.assert_not_called()
                self.assertEqual(self.config, self.original)

    @isolated_update_test
    def test_major_and_postgres_policy_block_before_network_or_staging(self):
        updater = self.fixture(running=True)
        for blocker in ("major_blocker", "pg_blocker"):
            with self.subTest(blocker=blocker):
                self.patches[blocker].return_value = "fixture migration is still active"
                payload = self.update(manual=True)
                self.assertEqual(payload["status"], "blocked")
                self.patches["resolve"].assert_not_called()
                self.patches["stage"].assert_not_called()
                self.patches["perform"].assert_not_called()
                updater.process_handler.stop_process.assert_not_called()
                self.patches[blocker].return_value = None

    @isolated_update_test
    def test_unresumable_journal_is_blocked_without_new_download_or_old_restart(self):
        updater = self.fixture(running=True)
        self.patches["journal"].return_value = {
            "state": "recovery_required",
            "recovery_required": True,
            "launched": False,
            "stage": "candidate_published",
            "message": "fixture requires recovery",
        }
        payload = self.update(manual=True)
        self.assertIn(payload["status"], {"blocked", "error"})
        self.patches["stage"].assert_not_called()
        self.patches["perform"].assert_not_called()
        self.patches["resume"].assert_not_called()
        updater.start_process.assert_not_called()

    @isolated_update_test
    def test_success_keeps_intentionally_stopped_service_stopped(self):
        updater = self.fixture()
        payload = self.update(manual=True)
        self.assertEqual(payload["status"], "updated")
        updater.process_handler.start_prepared_process.assert_not_called()
        updater._wait_for_update_health.assert_not_called()
        updater.start_process.assert_not_called()
        self.assertEqual(self.config["release_version"], "latest")
        self.manager.save_config.assert_called()
        self.patches["major_retire"].assert_called_once()
        self.patches["pg_retire"].assert_called_once()

    @isolated_update_test
    def test_running_update_waits_for_health_before_saving_selection(self):
        updater = self.fixture(running=True, selected="v2.0.0-beta.7")

        def health(name):
            self.assertEqual(self.config["release_version"], "v2.0.0-beta.7")
            self.manager.save_config.assert_not_called()
            self.events.append("health")
            return True, None

        updater._wait_for_update_health.side_effect = health
        payload = self.update(allow_override=True, manual=True)
        self.assertEqual(payload["status"], "updated")
        updater.process_handler.stop_process.assert_called_once_with(
            "InfiniDysk", disable_restart=True
        )
        updater.process_handler.start_prepared_process.assert_called_once_with(
            "InfiniDysk"
        )
        self.assertLess(self.events.index("stage"), self.events.index("stop"))
        self.assertLess(
            self.events.index("retire_rollback"), self.events.index("health")
        )
        self.assertLess(
            self.events.index("health"), self.events.index("save_selection")
        )
        self.assertEqual(self.config["release_version"], "latest")

    @isolated_update_test
    def test_postlaunch_failure_retains_new_runtime_without_generic_rollback(self):
        from utils.infinidysk_v2_update import NativeUpdateRecoveryRequired

        updater = self.fixture(running=True)

        def failed_after_publication(*args, **kwargs):
            self.patches["installed"].return_value = self.metadata["release"]
            raise NativeUpdateRecoveryRequired(
                receipt={
                    "launched": True,
                    "was_running": True,
                    "release_metadata": self.metadata,
                },
                stage="wait_ready",
            )

        self.patches["perform"].side_effect = failed_after_publication
        updater._recover_pending_snapshot = Mock()
        payload = self.update(manual=True)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["reason"], "native_runtime_recovery")
        self.assertTrue(payload["can_retry_update"])
        updater.start_process.assert_not_called()
        updater._recover_pending_snapshot.assert_not_called()
        self.manager.save_config.assert_not_called()

    @isolated_update_test
    def test_manual_retry_uses_retained_replacement_without_staging_again(self):
        updater = self.fixture()
        self.patches["installed"].return_value = "v2.0.0-beta.10"
        self.patches["journal"].return_value = {
            "state": "recovery_required",
            "recovery_required": True,
            "launched": True,
            "was_running": True,
            "release": "v2.0.0-beta.10",
            "stage": "wait_ready",
            "target_selector": "latest",
        }
        payload = self.update(manual=True)
        self.assertEqual(payload["status"], "updated")
        self.patches["resume"].assert_called_once()
        self.patches["perform"].assert_not_called()
        self.patches["stage"].assert_not_called()
        self.patches["resolve"].assert_not_called()
        updater.process_handler.start_prepared_process.assert_called_once_with(
            "InfiniDysk"
        )
        updater.start_process.assert_not_called()

    @isolated_update_test
    def test_configuration_saved_during_download_is_not_overwritten_or_stopped(self):
        updater = self.fixture(running=True)

        @contextmanager
        def stage_after_user_edit(*args, **kwargs):
            self.config["release_version"] = "v2.0.0-beta.9"
            self.config["release_version_enabled"] = True
            yield self.candidate, self.metadata

        self.patches["stage"].side_effect = stage_after_user_edit
        payload = self.update(manual=True)
        self.assertIn(payload["status"], {"blocked", "error"})
        self.assertEqual(self.config["release_version"], "v2.0.0-beta.9")
        updater.process_handler.stop_process.assert_not_called()
        updater.process_handler.start_prepared_process.assert_not_called()
        self.manager.save_config.assert_not_called()

    @isolated_update_test
    def test_retry_after_latest_override_does_not_restore_obsolete_release_pin(self):
        self.fixture(selected="v2.0.0-beta.7")
        self.patches["installed"].return_value = "v2.0.0-beta.10"
        self.patches["journal"].return_value = {
            "state": "recovery_required",
            "recovery_required": True,
            "launched": True,
            "was_running": False,
            "release": "v2.0.0-beta.10",
            "stage": "save_selection",
            "target_selector": "latest",
        }
        payload = self.update(manual=True)
        self.assertEqual(payload["status"], "updated")
        self.assertEqual(self.config["release_version"], "latest")
        configured = self.patches["configure"].call_args.args[2]
        self.assertEqual(configured["release_version"], "latest")
        self.patches["resolve"].assert_not_called()
        self.patches["stage"].assert_not_called()

    @isolated_update_test
    def test_background_check_never_restarts_a_recovery_candidate(self):
        updater = self.fixture()
        self.patches["installed"].return_value = "v2.0.0-beta.10"
        self.patches["journal"].return_value = {
            "state": "recovery_required",
            "recovery_required": True,
            "launched": True,
            "was_running": True,
            "release": "v2.0.0-beta.10",
            "stage": "wait_ready",
            "target_selector": "latest",
        }
        payload = self.update(manual=False)
        self.assertEqual(payload["reason"], "native_runtime_recovery")
        self.assertTrue(payload["can_retry_update"])
        self.patches["resume"].assert_not_called()
        self.patches["perform"].assert_not_called()
        self.patches["stage"].assert_not_called()
        updater.process_handler.start_prepared_process.assert_not_called()

    @isolated_update_test
    def test_recovery_status_never_offers_retry_for_missing_or_wrong_replacement(self):
        updater = self.fixture()
        self.patches["journal"].return_value = {
            "state": "recovery_required",
            "recovery_required": True,
            "launched": True,
            "was_running": True,
            "release": "v2.0.0-beta.10",
            "target_selector": "latest",
        }
        for installed in (None, "v2.0.0-beta.6", "v2.0.0-beta.10"):
            with self.subTest(installed=installed):
                self.patches["installed"].return_value = installed
                payload = updater._native_runtime_update_status(
                    "InfiniDysk", self.config
                )
                self.assertEqual(
                    payload["can_retry_update"], installed == "v2.0.0-beta.10"
                )
                self.assertEqual(payload["reason"], "native_runtime_recovery")
                if installed != "v2.0.0-beta.10":
                    self.assertIn("recorded replacement", payload["message"])
                    self.assertIn("do not install an older binary", payload["message"])
        for name in ("resolve", "stage", "perform", "resume"):
            self.patches[name].assert_not_called()

    @isolated_update_test
    def test_release_lookup_failure_keeps_current_runtime_and_selection(self):
        updater = self.fixture(running=True)
        self.patches["resolve"].side_effect = OSError("fixture release lookup failed")
        payload = self.update(manual=True)
        self.assertEqual(payload["status"], "error")
        self.patches["stage"].assert_not_called()
        self.patches["perform"].assert_not_called()
        updater.process_handler.stop_process.assert_not_called()
        updater.process_handler.start_prepared_process.assert_not_called()
        self.assertEqual(self.config, self.original)
        self.manager.save_config.assert_not_called()

    @isolated_update_test
    def test_manual_install_preserves_native_recovery_details_and_source_choices(self):
        updater = self.fixture(selected="v2.0.0-beta.7")
        recovery = {
            "status": "error",
            "reason": "native_runtime_recovery",
            "can_retry_update": True,
            "message": "The retained replacement needs review.",
        }
        updater.supports_manual_update = Mock(return_value=True)
        updater._update_native_runtime = Mock(return_value=recovery)
        updater.update_check = Mock()
        updater._recover_pending_snapshot = Mock()
        payload = updater._manual_update_install_unprotected(
            "InfiniDysk", allow_override=True, target="latest"
        )
        self.assertEqual(payload, recovery)
        updater._update_native_runtime.assert_called_once_with(
            "InfiniDysk",
            self.config,
            "infinidysk",
            None,
            allow_override=True,
            target="latest",
            manual=True,
        )
        updater._safe_record_update_status.assert_called_once_with(
            "InfiniDysk", recovery
        )
        updater.update_check.assert_not_called()
        updater._recover_pending_snapshot.assert_not_called()
        self.assertEqual(self.config, self.original)

    @isolated_update_test
    def test_scheduled_install_preserves_native_recovery_without_snapshot_restore(self):
        updater = self.fixture(running=True)
        recovery = {
            "status": "error",
            "reason": "native_runtime_recovery",
            "can_retry_update": True,
            "message": "The retained replacement needs review.",
        }
        updater._update_native_runtime = Mock(return_value=recovery)
        updater._begin_update_timing = Mock(return_value=True)
        updater._finish_update_timing = Mock()
        updater._recover_pending_snapshot = Mock()
        updater.update_check = Mock()
        with patch("utils.auto_update.INSTALL_CACHE") as cache:
            cache.begin_operation.return_value = "fixture-operation"
            payload = updater._scheduled_update_check_unprotected(
                "InfiniDysk", self.config, "infinidysk", None
            )
        self.assertEqual(payload, recovery)
        updater._update_native_runtime.assert_called_once_with(
            "InfiniDysk", self.config, "infinidysk", None
        )
        updater._finish_update_timing.assert_called_once_with("InfiniDysk", recovery)
        updater._safe_record_update_status.assert_called_once_with(
            "InfiniDysk", recovery
        )
        self.assertIsNone(updater._active_install_operation)
        self.assertFalse(updater.updating.locked())
        updater.update_check.assert_not_called()
        updater._recover_pending_snapshot.assert_not_called()
        self.assertEqual(self.config, self.original)

    @isolated_update_test
    def test_scheduled_install_rechecks_live_policy_after_media_protection_wait(self):
        from contextlib import ExitStack

        updater = self.fixture(running=True)
        updater._manual_update_check_internal = Mock(
            return_value={"status": "update_available"}
        )
        updater._begin_update_timing = Mock(return_value=False)
        updater._infinidysk_migration_update_blocker = Mock(return_value=None)
        updater.media_protection_manager = Mock()
        for changed_policy, expected_status in (
            ({"auto_update_mode": "check_only"}, "no_update"),
            ({"auto_update": False}, "disabled"),
        ):
            with self.subTest(policy=changed_policy), ExitStack() as patches:
                self.config.clear()
                self.config.update(copy.deepcopy(self.original))
                before = []

                def change_while_waiting(*args):
                    self.config.update(changed_policy)
                    before.append(copy.deepcopy(self.config))
                    return {"status": "ready", "token": "fixture-protection"}

                updater.media_protection_manager.begin_planned.side_effect = (
                    change_while_waiting
                )
                patches.enter_context(patch("utils.auto_update.INSTALL_CACHE"))
                payload = updater._scheduled_update_check_admitted(
                    "InfiniDysk", self.config, "infinidysk", None
                )
                self.assertEqual(payload["status"], expected_status)
                self.assertEqual(self.config, before[0])
                self.manager.save_config.assert_not_called()
                updater.process_handler.stop_process.assert_not_called()
                updater.process_handler.start_prepared_process.assert_not_called()
                for name in (
                    "stage",
                    "perform",
                    "resume",
                    "unlock",
                    "major_retire",
                    "pg_retire",
                ):
                    self.patches[name].assert_not_called()

    @isolated_update_test
    def test_native_startup_reuses_setup_and_only_reschedules_future_updates(self):
        updater = self.fixture()
        updater.reschedule_auto_update = Mock()
        updater.update_check = Mock()
        updater.initial_update_check = Mock()
        updater.reschedule_symlink_backup = Mock()
        with (
            patch(
                "utils.auto_update.setup_project", return_value=(True, None)
            ) as setup,
            patch("utils.infinidysk_v2_runtime.install_v2_runtime") as strict_install,
        ):
            for enabled in (False, True):
                with self.subTest(enable_update=enabled):
                    updater.reschedule_auto_update.reset_mock()
                    process, error = updater._auto_update_admitted(
                        "InfiniDysk", enabled, force_update_check=True
                    )
                    self.assertEqual((process, error), ("started", None))
                    setup.assert_called_with(updater.process_handler, "InfiniDysk")
                    updater.start_process.assert_called_with(
                        "InfiniDysk", self.config, "infinidysk", None
                    )
                    if enabled:
                        updater.reschedule_auto_update.assert_called_once_with(
                            "InfiniDysk"
                        )
                    else:
                        updater.reschedule_auto_update.assert_not_called()
            strict_install.assert_not_called()
        updater.initial_update_check.assert_not_called()
        updater.update_check.assert_not_called()
        updater.reschedule_symlink_backup.assert_not_called()
        self.patches["resolve"].assert_not_called()
        self.patches["stage"].assert_not_called()
        self.patches["perform"].assert_not_called()

    @isolated_update_test
    def test_version_check_reads_verified_active_release_beyond_migration_pin(self):
        from utils.versions import Versions

        self.fixture()
        self.patches["installed"].return_value = "v2.0.0-beta.10"
        versions = object.__new__(Versions)
        versions.logger = Mock()
        with (
            patch("utils.versions.CONFIG_MANAGER", self.manager),
            patch("utils.infinidysk_v2_runtime.installed_v2_version") as strict_reader,
        ):
            version, error = versions.version_check("InfiniDysk", None, "infinidysk")
        self.assertEqual(version, "v2.0.0-beta.10")
        self.assertIsNone(error)
        strict_reader.assert_not_called()
        self.patches["installed"].assert_called_once_with(self.config)
        self.patches["resolve"].assert_not_called()

    @isolated_update_test
    def test_version_comparison_resolves_native_release_with_numeric_prerelease_order(
        self,
    ):
        from utils.versions import Versions

        self.fixture()
        versions = object.__new__(Versions)
        versions.logger = Mock()
        with (
            patch("utils.versions.CONFIG_MANAGER", self.manager),
            patch("utils.infinidysk_v2_runtime.installed_v2_version") as strict_reader,
        ):
            for current, needed in (
                ("v2.0.0-beta.6", True),
                ("v2.0.0-beta.10", False),
                ("v2.0.0-beta.11", False),
            ):
                with self.subTest(current=current):
                    self.patches["installed"].return_value = current
                    update, info = versions.compare_versions(
                        "InfiniDysk", "infinidysk", "infinidysk", None, "infinidysk"
                    )
                    self.assertEqual(update, needed)
                    self.assertEqual(info["current_version"], current)
                    self.assertEqual(info["latest_version"], "v2.0.0-beta.10")
                    if current == "v2.0.0-beta.11":
                        self.assertIn("downgrade", info["message"].lower())
            strict_reader.assert_not_called()
        self.patches["resolve"].assert_called_with(self.config)

    @isolated_update_test
    def test_controller_ready_unlock_preserves_disabled_and_check_only_preferences(
        self,
    ):
        updater = self.fixture(selected="v2.0.0-beta.6")
        updater.process_handler.startup_phase = "ready"
        updater.reschedule_auto_update = Mock()

        def unlock():
            self.config["release_version"] = "latest"
            self.config["release_version_enabled"] = False
            return True

        self.patches["unlock"].side_effect = unlock
        with patch("utils.infinidysk_v2_runtime.install_v2_runtime") as strict_install:
            for enabled, mode in ((False, "apply"), (True, "check_only")):
                with self.subTest(auto_update=enabled, mode=mode):
                    self.config.update(
                        release_version="v2.0.0-beta.6",
                        release_version_enabled=True,
                        auto_update=enabled,
                        auto_update_mode=mode,
                    )
                    before = copy.deepcopy(self.config)
                    self.patches["unlock"].reset_mock()
                    updater.reschedule_auto_update.reset_mock()
                    self.assertTrue(updater.release_completed_native_migration())
                    expected = {
                        **before,
                        "release_version": "latest",
                        "release_version_enabled": False,
                    }
                    self.assertEqual(self.config, expected)
                    self.patches["unlock"].assert_called_once_with()
                    updater.reschedule_auto_update.assert_called_once_with("InfiniDysk")
            strict_install.assert_not_called()
        for name in ("resolve", "stage", "perform", "resume"):
            self.patches[name].assert_not_called()
        updater.start_process.assert_not_called()
        updater.process_handler.start_prepared_process.assert_not_called()

    @isolated_update_test
    def test_controller_startup_or_degraded_state_never_unlocks_native_selector(self):
        updater = self.fixture(selected="v2.0.0-beta.6")
        updater.reschedule_auto_update = Mock()
        for phase in (None, "starting", "setup", "degraded"):
            with self.subTest(phase=phase):
                updater.process_handler.startup_phase = phase
                self.assertFalse(updater.release_completed_native_migration())
        self.patches["installed"].assert_not_called()
        for name in ("unlock", "major_blocker", "pg_blocker", "resolve", "stage"):
            self.patches[name].assert_not_called()
        updater.reschedule_auto_update.assert_not_called()
        self.assertEqual(self.config, self.original)

    @isolated_update_test
    def test_controller_ready_requires_verified_unblocked_native_runtime_and_change(
        self,
    ):
        updater = self.fixture(selected="v2.0.0-beta.6")
        updater.process_handler.startup_phase = "ready"
        updater.reschedule_auto_update = Mock()
        for condition in ("legacy", "missing", "postgres", "major", "unchanged"):
            with self.subTest(condition=condition):
                self.config["runtime_generation"] = 1 if condition == "legacy" else 2
                self.patches["installed"].return_value = (
                    None if condition == "missing" else "v2.0.0-beta.6"
                )
                self.patches["pg_blocker"].return_value = (
                    "fixture database operation" if condition == "postgres" else None
                )
                self.patches["major_blocker"].return_value = (
                    "fixture major migration" if condition == "major" else None
                )
                self.patches["unlock"].return_value = False
                self.patches["unlock"].reset_mock()
                before = copy.deepcopy(self.config)
                self.assertFalse(updater.release_completed_native_migration())
                if condition in {"major", "unchanged"}:
                    self.patches["unlock"].assert_called_once_with()
                else:
                    self.patches["unlock"].assert_not_called()
                self.assertEqual(self.config, before)
                updater.reschedule_auto_update.assert_not_called()
        for name in ("resolve", "stage", "perform", "resume"):
            self.patches[name].assert_not_called()
        self.manager.save_config.assert_not_called()

    @isolated_update_test
    def test_authorized_policy_unlock_finishes_its_own_pending_intent(self):
        updater = self.fixture(selected="v2.0.0-beta.6")
        self.patches["major_blocker"].side_effect = lambda: (
            "The native selector change needs to finish before updating."
            if self.config["release_version"] != "latest"
            else None
        )

        def unlock():
            self.config["release_version"] = "latest"
            self.config["release_version_enabled"] = False
            return True

        self.patches["unlock"].side_effect = unlock
        self.assertIsNone(updater._native_runtime_policy_blocker(allow_unlock=True))
        self.assertEqual(self.config["release_version"], "latest")
        self.patches["unlock"].assert_called_once_with()
        self.patches["resolve"].assert_not_called()
        self.patches["stage"].assert_not_called()
