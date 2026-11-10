import unittest
from unittest.mock import Mock, patch

from utils.arrangement_restart import reconcile_with_restart


class ControlledRestartTests(unittest.TestCase):
    def setUp(self):
        self.action = {"source": "external_arr.movies", "action": "provider.decypharr"}
        self.config = {"decypharr": {"process_name": "Decypharr"}}
        self.payload = {"arrangement": {"values": {}}}
        self.pending = {
            "execution": "waiting_input",
            "verification": "not_checked",
            "evidence": {
                "connections": [
                    {
                        "execution": "waiting_input",
                        "evidence": {"restart_required": "decypharr"},
                    }
                ]
            },
        }
        self.handler, self.updater = Mock(), Mock()
        self.callback = Mock(return_value=self.pending)

    def run_adapter(self, operation="apply", **kwargs):
        return reconcile_with_restart(
            self.action,
            self.config,
            self.payload,
            operation,
            self.callback,
            process_handler=self.handler,
            updater=self.updater,
            **kwargs,
        )

    def test_missing_exact_consent_preserves_saved_native_state_without_stopping(self):
        with patch("utils.arrangement_restart._stop_verified") as stop:
            result = self.run_adapter()
            self.assertEqual(result["execution"], "waiting_input")
            self.assertEqual(
                result["missing_inputs"],
                ["external_arr.movies::decypharr::restart_after_config"],
            )
            stop.assert_not_called()
        self.updater.auto_update.assert_not_called()

    def test_preview_and_recheck_never_restart_even_with_consent(self):
        self.payload["arrangement"]["values"][
            "external_arr.movies::decypharr::restart_after_config"
        ] = True
        for operation in ("preview", "recheck", "apply"):
            with patch("utils.arrangement_restart._stop_verified") as stop:
                self.run_adapter(operation, read_only=operation == "apply")
                stop.assert_not_called()

    def test_verified_dec_only_stop_then_no_update_restart_and_native_readback(self):
        self.payload["arrangement"]["values"][
            "external_arr.movies::decypharr::restart_after_config"
        ] = True
        self.updater.auto_update.return_value = (object(), None)
        self.callback.side_effect = [
            self.pending,
            {"execution": "applied", "verification": "passed", "evidence": {}},
        ]
        with (
            patch("utils.arrangement_restart._tracked", return_value=object()),
            patch(
                "utils.arrangement_restart._stop_verified", return_value=True
            ) as stop,
        ):
            result = self.run_adapter()
            stop.assert_called_once_with(self.handler, "Decypharr")
        self.updater.auto_update.assert_called_once_with(
            "Decypharr", enable_update=False
        )
        self.assertEqual(self.callback.call_count, 2)
        self.assertEqual(result["verification"], "passed")
        self.assertTrue(result["evidence"]["restart_attempted"])

    def test_failed_stop_never_launches_and_failed_launch_never_rolls_back_config(self):
        self.payload["arrangement"]["values"][
            "external_arr.movies::decypharr::restart_after_config"
        ] = True
        for stopped in (False, True):
            self.updater.reset_mock()
            self.updater.auto_update.return_value = (None, "fixture failure")
            with (
                patch("utils.arrangement_restart._tracked", return_value=object()),
                patch("utils.arrangement_restart._stop_verified", return_value=stopped),
            ):
                result = self.run_adapter()
            self.assertEqual(result["execution"], "failed")
            self.assertFalse(result["retryable"])
            self.assertIn("preserved", result["reason"])
            self.assertEqual(self.config, {"decypharr": {"process_name": "Decypharr"}})
            self.assertEqual(self.updater.auto_update.call_count, int(stopped))

    def test_untracked_service_and_other_provider_never_restart(self):
        self.payload["arrangement"]["values"][
            "external_arr.movies::decypharr::restart_after_config"
        ] = True
        with (
            patch("utils.arrangement_restart._tracked", return_value=None),
            patch("utils.arrangement_restart._stop_verified") as stop,
        ):
            self.assertEqual(self.run_adapter()["execution"], "waiting_input")
            self.action["action"] = "provider.altmount"
            self.run_adapter()
            stop.assert_not_called()
