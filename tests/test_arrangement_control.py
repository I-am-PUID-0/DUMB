import copy
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from utils.arrangement_control import configure_altmount_webdav, configure_discovery


class ScopedRestartTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "config.json"
        self.path.write_text('{"unchanged": true}\n')
        self.original = self.path.read_bytes()
        self.config = {
            "cli_debrid": {
                "process_name": "CLI Debrid",
                "config_dir": temporary.name,
                "env": {"USER_CONFIG": temporary.name, "custom": "keep"},
            }
        }
        self.before = copy.deepcopy(self.config)
        self.action = {
            "service_key": "cli_debrid",
            "source": "cli_debrid",
            "action": "companion_connection",
        }
        self.payload = {
            "arrangement": {"values": {"cli_debrid::restart_after_config": True}}
        }
        self.process = Mock(pid=123456)
        self.process.poll.return_value = None
        self.handler = types.SimpleNamespace(
            process_names={"CLI Debrid": self.process},
            _process_group_alive=lambda _: False,
        )

        def stop(*args, **kwargs):
            self.process.poll.return_value = 0
            self.handler.process_names.clear()

        self.handler.stop_process = Mock(side_effect=stop)
        self.manager = types.SimpleNamespace(config=self.config, save_config=Mock())
        self.updater = types.SimpleNamespace(
            auto_update=Mock(return_value=(object(), None))
        )
        self.inspect = Mock(
            side_effect=[
                {"execution": "planned"},
                {"execution": "already_present", "verification": "passed"},
            ]
        )

    def execute(self, apply):
        with patch(
            "utils.arrangement_control.os.getpgid", return_value=os.getpgrp() + 100000
        ):
            return configure_discovery(
                self.action,
                self.config,
                self.payload,
                self.handler,
                self.updater,
                self.inspect,
                apply,
                manager=self.manager,
            )

    def test_explicit_stop_marker_and_manager_reach_only_selected_service(self):
        def apply(action, config, payload, *, manager):
            self.assertTrue(payload["_service_stopped"])
            self.assertIsNotNone(self.process.poll())
            self.assertIs(manager, self.manager)
            self.path.write_text('{"configured": true}\n')
            return {"execution": "applied"}

        self.assertEqual(self.execute(apply)["verification"], "passed")
        self.updater.auto_update.assert_called_once_with(
            "CLI Debrid", enable_update=False
        )
        self.assertNotIn("_service_stopped", self.payload)
        self.assertFalse(Path(str(self.path) + ".dumb-setup-recovery").exists())

    def test_altmount_restart_compensates_both_private_webdav_sidecars(self):
        cfg = {"process_name": "CLI Debrid", "config_file": str(self.path)}
        config = {"altmount": cfg}
        action = {"service_key": "altmount", "source": "altmount", "action": "webdav"}
        payload = {"arrangement": {"values": {"altmount::restart_after_config": True}}}
        ledger = self.path.parent / ".dumb-altmount-webdav-ownership.json"
        proposal = self.path.parent / ".dumb-altmount-webdav-preview.json"
        ledger.write_text('{"original": true}')

        def fail(*args):
            self.assertIsNotNone(self.process.poll())
            self.assertTrue(args[2]["_service_stopped"])
            ledger.write_text('{"changed": true}')
            proposal.write_text('{"new": true}')
            self.path.write_text("changed")
            raise RuntimeError("A private settings write failed")

        with patch(
            "utils.arrangement_control.os.getpgid", return_value=os.getpgrp() + 100000
        ):
            result = configure_altmount_webdav(
                action, config, payload, self.handler, self.updater, self.inspect, fail
            )
        self.assertEqual(result["execution"], "failed")
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(ledger.read_text(), '{"original": true}')
        self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
        self.assertFalse(proposal.exists())
        self.assertNotIn("_service_stopped", payload)

    def test_failed_configuration_restores_native_file_and_runtime_environment(self):
        def fail(action, config, payload, *, manager):
            config["cli_debrid"]["env"].update(CLI_DEBRID_PHALANX_HOST="127.0.0.1")
            manager.save_config()
            self.path.write_text('{"partial": true}\n')
            raise RuntimeError("Example save failure")

        result = self.execute(fail)
        self.assertEqual(result["execution"], "failed")
        self.assertEqual(self.config, self.before)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.save_config.call_count, 2)
        self.updater.auto_update.assert_called_once_with(
            "CLI Debrid", enable_update=False
        )

    def test_missing_restart_consent_preserves_running_application(self):
        self.payload["arrangement"]["values"].clear()
        apply = Mock()
        result = self.execute(apply)
        self.assertEqual(result["execution"], "waiting_input")
        self.assertIn("CLI Debrid", result["reason"])
        self.handler.stop_process.assert_not_called()
        apply.assert_not_called()
        self.assertEqual(self.config, self.before)

    def test_existing_recovery_evidence_is_preserved(self):
        recovery = Path(str(self.path) + ".dumb-setup-recovery")
        recovery.write_text("prior recovery evidence")
        apply = Mock()
        self.assertEqual(self.execute(apply)["execution"], "waiting_input")
        apply.assert_not_called()
        self.assertEqual(recovery.read_text(), "prior recovery evidence")
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_zurg_failed_apply_restores_hook_secrets_and_removes_new_ledger(self):
        self.action.update(
            service_key="zurg", source="zurg.Test", action="library_hook"
        )
        self.config = {
            "zurg": {
                "instances": {
                    "Test": {
                        "config_file": str(self.path),
                        "process_name": "CLI Debrid",
                    }
                }
            }
        }
        self.manager.config = self.config
        self.payload = {
            "arrangement": {"values": {"zurg.Test::restart_after_config": True}}
        }
        private = self.path.parent / ".dumb-zurg-library-hook.json"
        ledger = self.path.parent / ".dumb-zurg-hook-ownership.json"
        private.write_text('{"libraries": ["1"], "api_key": "example-original"}')
        original = private.read_bytes()

        def fail(action, config, payload):
            private.write_text('{"libraries": ["2"], "api_key": "example-new"}')
            ledger.write_text('{"partial": true}')
            raise RuntimeError("Example apply failure")

        result = self.execute(fail)
        self.assertEqual(result["execution"], "failed")
        self.assertEqual(private.read_bytes(), original)
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)
        self.assertFalse(ledger.exists())
        self.assertFalse(
            Path(str(self.path) + ".dumb-setup-recovery-state.json").exists()
        )
