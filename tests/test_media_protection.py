import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils import media_protection


class FakeProcess:
    def poll(self):
        return None


class FakeConfigManager:
    def __init__(self):
        self.config = {
            "dumb": {
                "media_protection": {
                    "enabled": True,
                    "recovery_stabilization_seconds": 5,
                    "recovery_timeout_seconds": 30,
                    "monitor_interval_seconds": 2,
                    "services": [],
                }
            },
            "cli_debrid": {
                "enabled": True,
                "process_name": "CLI Debrid",
            },
            "plex": {
                "enabled": True,
                "process_name": "Plex Media Server",
            },
            "jellyfin": {"enabled": False, "process_name": "Jellyfin"},
            "emby": {"enabled": False, "process_name": "Emby Server"},
        }
        self.saved = 0

    def get(self, key, default=None):
        return self.config.get(key, default)

    def get_instance(self, instance_name=None, key=None):
        return self.config.get(key)

    def find_key_for_process(self, process_name):
        for key, config in self.config.items():
            if isinstance(config, dict) and config.get("process_name") == process_name:
                return key, None
        return None, None

    def save_config(self):
        self.saved += 1


class FakeProcessHandler:
    def __init__(self):
        self.startup_phase = "ready"
        self.processes = {
            10: {"name": "CLI Debrid", "process_obj": FakeProcess()},
            11: {"name": "Plex Media Server", "process_obj": FakeProcess()},
        }
        self.stopped = []
        self.started = []

    def stop_process(self, process_name):
        self.stopped.append(process_name)
        self.processes = {
            pid: info
            for pid, info in self.processes.items()
            if info["name"] != process_name
        }

    def start_process(self, process_name):
        self.started.append(process_name)
        self.processes[max(self.processes, default=20) + 1] = {
            "name": process_name,
            "process_obj": FakeProcess(),
        }
        return True, None

    def _find_process_entry(self, process_name):
        for pid, info in self.processes.items():
            if info["name"] == process_name:
                return pid, info
        return None, None

    def get_process_health(self, process_name, pid):
        return {"status": "healthy"}


class FakeAdapter:
    def __init__(self, state):
        self.state = state
        self.guarded = 0
        self.restored = 0

    def activity(self):
        return {
            "state": self.state,
            "active_sessions": 1 if self.state == "busy" else 0,
        }

    def enter_scan_guard(self):
        self.guarded += 1
        return {"changed": ["scan"]}

    def restore_scan_guard(self, snapshot):
        self.restored += 1
        return ["scan"]


class MediaProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = FakeConfigManager()
        self.handler = FakeProcessHandler()
        self.logger = Mock()
        self.adapter = FakeAdapter("idle")
        self.patches = [
            patch.object(media_protection, "CONFIG_MANAGER", self.config),
            patch.object(media_protection, "notify_event"),
            patch.object(
                media_protection,
                "build_adapter",
                side_effect=lambda *args, **kwargs: self.adapter,
            ),
        ]
        self.started_patches = [item.start() for item in self.patches]
        self.notify_event = self.started_patches[1]
        self.manager = media_protection.MediaProtectionManager(
            self.handler,
            self.logger,
            state_path=str(Path(self.temp_dir.name) / "state.json"),
        )

    def tearDown(self):
        self.manager.shutdown()
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    def test_protection_is_enabled_by_default(self):
        policy = media_protection.protection_policy("Plex Media Server")
        self.assertTrue(policy["enabled"])
        self.assertFalse(policy["api_key_configured"])

    def test_already_stopped_media_server_is_not_protected(self):
        self.handler.stop_process("Plex Media Server")

        preflight = self.manager.preflight("CLI Debrid", "restart")
        result = self.manager.begin_planned("CLI Debrid", "restart", "safe")

        self.assertFalse(preflight["protected"])
        self.assertEqual([], preflight["media_servers"])
        self.assertEqual("not_applicable", result["status"])
        self.assertIsNone(result["token"])
        self.assertIsNone(self.manager.begin_unplanned("CLI Debrid", "crashed"))
        self.assertEqual({}, self.manager.incidents)
        self.assertEqual(0, self.adapter.guarded)
        self.notify_event.assert_not_called()

    def test_safe_planned_action_defers_for_active_stream(self):
        self.adapter.state = "busy"
        result = self.manager.begin_planned("CLI Debrid", "restart", "safe")
        self.assertEqual(result["status"], "deferred")
        self.assertTrue(result["preflight"]["busy"])
        self.assertEqual(self.handler.stopped, [])
        self.assertEqual(self.adapter.guarded, 0)

    def test_keep_running_override_guards_scans_without_stopping_plex(self):
        self.adapter.state = "busy"
        result = self.manager.begin_planned("CLI Debrid", "restart", "keep_running")
        self.assertEqual(result["status"], "protected")
        self.assertEqual(self.adapter.guarded, 1)
        self.assertNotIn("Plex Media Server", self.handler.stopped)

    def test_planned_restart_waits_for_completion_and_stabilization(self):
        result = self.manager.begin_planned("CLI Debrid", "restart", "safe")
        self.assertIn("Plex Media Server", self.handler.stopped)
        incident = self.manager.incidents[result["token"]]
        self.assertTrue(incident["awaiting_operation_completion"])
        self.assertTrue(self.manager._dependency_ready(incident))
        self.assertFalse(self.manager._ready_for_recovery(incident))

        self.manager.complete_planned(result["token"], success=True)

        incident = self.manager.incidents[result["token"]]
        self.assertFalse(incident["awaiting_operation_completion"])
        self.assertEqual(incident["status"], "waiting_for_recovery")
        self.assertTrue(self.manager._ready_for_recovery(incident))
        self.assertNotIn("Plex Media Server", self.handler.started)
        self.assertEqual(self.adapter.restored, 0)

        self.manager._recover(result["token"])

        self.assertIn("Plex Media Server", self.handler.started)
        self.assertEqual(self.adapter.restored, 1)
        self.assertEqual(self.manager.incidents[result["token"]]["status"], "recovered")

    def test_planned_update_cannot_recover_while_target_is_still_running(self):
        result = self.manager.begin_planned("CLI Debrid", "update", "safe")
        incident = self.manager.incidents[result["token"]]

        self.assertTrue(self.manager._dependency_ready(incident))
        self.assertFalse(self.manager._ready_for_recovery(incident))
        self.assertNotIn("Plex Media Server", self.handler.started)

    def test_planned_stop_releases_operation_hold_only_after_stop_completes(self):
        result = self.manager.begin_planned("CLI Debrid", "stop", "safe")
        incident = self.manager.incidents[result["token"]]
        self.assertFalse(self.manager._ready_for_recovery(incident))

        self.handler.stop_process("CLI Debrid")
        self.manager.complete_planned(result["token"], success=True)

        incident = self.manager.incidents[result["token"]]
        self.assertFalse(incident["awaiting_operation_completion"])
        self.assertFalse(self.manager._ready_for_recovery(incident))
        self.assertNotIn("Plex Media Server", self.handler.started)

        self.handler.start_process("CLI Debrid")
        self.assertTrue(self.manager._ready_for_recovery(incident))

    def test_unexpected_outage_preserves_active_stream(self):
        self.adapter.state = "busy"
        token = self.manager.begin_unplanned("CLI Debrid", "crashed")
        self.assertIsNotNone(token)
        self.assertEqual(self.adapter.guarded, 1)
        self.assertNotIn("Plex Media Server", self.handler.stopped)
        self.assertEqual(self.manager.begin_unplanned("CLI Debrid", "again"), token)

    def test_recovery_failure_retries_without_repeating_critical_notification(self):
        result = self.manager.begin_planned("CLI Debrid", "restart", "safe")
        self.notify_event.reset_mock()
        self.adapter.restore_scan_guard = Mock(
            side_effect=RuntimeError("restore failed")
        )

        self.manager.complete_planned(result["token"], success=True)
        self.manager._recover(result["token"])
        incident = self.manager.incidents[result["token"]]
        self.assertEqual(incident["status"], "recovery_failed")
        self.assertTrue(incident["recovery_failure_notified"])
        self.assertGreater(
            incident["next_recovery_attempt_at"], incident["recovered_at"]
        )
        self.assertEqual(self.notify_event.call_count, 1)

        self.manager._recover(result["token"])
        self.assertEqual(self.notify_event.call_count, 1)

    def test_plex_library_paths_are_replaced_as_one_exact_set(self):
        section = Mock()
        section.key = "7"
        section.title = "Movies"
        section.locations = ["/mnt/debrid/nzbdav-symlinks/movies"]

        def edit(**kwargs):
            section.locations = list(kwargs["location"])

        section.edit.side_effect = edit
        plex = Mock()
        plex.library.sections.return_value = [section]
        adapter = media_protection.PlexAdapter(
            "plex", "Plex Media Server", {}, {}, self.logger
        )
        with patch.object(adapter, "_connect", return_value=plex):
            self.assertEqual(
                ["/mnt/debrid/nzbdav-symlinks/movies"],
                adapter.library_paths()[0]["paths"],
            )
            changed = adapter.replace_library_paths(
                [
                    {
                        "id": "7",
                        "name": "Movies",
                        "paths": ["/mnt/debrid/infinidysk-symlinks/movies"],
                    }
                ]
            )
        self.assertEqual(1, len(changed))
        section.edit.assert_called_once_with(
            location=["/mnt/debrid/infinidysk-symlinks/movies"]
        )

    def test_plex_library_operations_use_extended_timeout(self):
        adapter = media_protection.PlexAdapter(
            "plex", "Plex Media Server", {}, {}, self.logger
        )
        plex = Mock()
        plex.library.sections.return_value = []

        with patch.object(adapter, "_connect", return_value=plex) as connect:
            adapter.library_paths()

        connect.assert_called_once_with(
            timeout=media_protection.PLEX_LIBRARY_OPERATION_TIMEOUT_SECONDS
        )

    def test_plex_library_timeout_is_accepted_after_exact_verification(self):
        section = Mock()
        section.key = "7"
        section.title = "Movies"
        section.locations = ["/mnt/debrid/nzbdav-symlinks/movies"]
        section.edit.side_effect = media_protection.requests.exceptions.ReadTimeout(
            "slow Plex response"
        )
        plex = Mock()
        plex.library.sections.return_value = [section]
        adapter = media_protection.PlexAdapter(
            "plex", "Plex Media Server", {}, {}, self.logger
        )

        with (
            patch.object(adapter, "_connect_for_library_operation", return_value=plex),
            patch.object(
                adapter, "_wait_for_library_paths", return_value=True
            ) as verify,
        ):
            changed = adapter.replace_library_paths(
                [
                    {
                        "id": "7",
                        "name": "Movies",
                        "paths": ["/mnt/debrid/infinidysk-symlinks/movies"],
                    }
                ]
            )

        self.assertEqual(1, len(changed))
        verify.assert_called_once_with("7", ["/mnt/debrid/infinidysk-symlinks/movies"])

    def test_plex_library_timeout_fails_when_exact_paths_cannot_be_verified(self):
        section = Mock()
        section.key = "7"
        section.title = "Movies"
        section.locations = ["/mnt/debrid/nzbdav-symlinks/movies"]
        section.edit.side_effect = media_protection.requests.exceptions.ReadTimeout(
            "slow Plex response"
        )
        plex = Mock()
        plex.library.sections.return_value = [section]
        adapter = media_protection.PlexAdapter(
            "plex", "Plex Media Server", {}, {}, self.logger
        )

        with (
            patch.object(adapter, "_connect_for_library_operation", return_value=plex),
            patch.object(adapter, "_wait_for_library_paths", return_value=False),
            self.assertRaisesRegex(RuntimeError, "could not be verified"),
        ):
            adapter.replace_library_paths(
                [
                    {
                        "id": "7",
                        "name": "Movies",
                        "paths": ["/mnt/debrid/infinidysk-symlinks/movies"],
                    }
                ]
            )

    def test_jellyfin_library_path_adds_destination_before_removing_source(self):
        adapter = media_protection.MediaBrowserAdapter(
            "jellyfin",
            "Jellyfin Media Server",
            {"port": 8096},
            {"api_key": "secret"},
            self.logger,
        )
        current = ["/mnt/debrid/nzbdav-symlinks/shows"]
        calls = []

        def folders():
            return [{"ItemId": "9", "Name": "Shows", "Locations": list(current)}]

        def request(method, path, **kwargs):
            calls.append((method, path, kwargs.get("params")))
            target = kwargs["params"]["path"]
            if method == "POST":
                current.append(target)
            else:
                current.remove(target)

        with (
            patch.object(adapter, "_virtual_folders", side_effect=folders),
            patch.object(adapter, "_request", side_effect=request),
        ):
            adapter.replace_library_paths(
                [
                    {
                        "id": "9",
                        "name": "Shows",
                        "paths": ["/mnt/debrid/infinidysk-symlinks/shows"],
                    }
                ]
            )
        self.assertEqual(["/mnt/debrid/infinidysk-symlinks/shows"], current)
        self.assertEqual("POST", calls[0][0])
        self.assertEqual("DELETE", calls[1][0])


if __name__ == "__main__":
    unittest.main()
