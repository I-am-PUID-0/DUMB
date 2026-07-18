import threading
import unittest
from unittest.mock import Mock, patch

from api.api_state import APIState
from utils.auto_update import Update


class UpdateNotificationTests(unittest.TestCase):
    def _updater(self):
        updater = object.__new__(Update)
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
