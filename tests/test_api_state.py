import os
import sys
import threading
import types
import unittest
from unittest.mock import patch

psutil = types.ModuleType("psutil")
psutil.STATUS_ZOMBIE = "zombie"
psutil.AccessDenied = type("AccessDenied", (Exception,), {})
psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
psutil.pid_exists = lambda pid: False
psutil.Process = lambda pid: None
sys.modules.setdefault("psutil", psutil)

config_loader = types.ModuleType("utils.config_loader")
config_loader.CONFIG_MANAGER = types.SimpleNamespace(
    get=lambda *args, **kwargs: {},
    find_key_for_process=lambda process_name: (None, None),
    get_instance=lambda instance_name, key: None,
)
sys.modules["utils.config_loader"] = config_loader

from api.api_state import APIState


class APIStateHelperTests(unittest.TestCase):
    def _state(self):
        state = APIState.__new__(APIState)
        state._update_cache = {}
        state._update_cache_lock = threading.Lock()
        state._update_notices = {"applied": [], "info": []}
        state._update_notices_lock = threading.Lock()
        state._save_update_notices = lambda: None
        return state

    def test_current_dumb_version_prefers_environment(self):
        state = self._state()

        with patch.dict(os.environ, {"DUMB_VERSION": "dev-abc1234"}, clear=False):
            self.assertEqual(state._current_dumb_version(), "dev-abc1234")

    def test_current_dumb_version_reads_project_metadata(self):
        state = self._state()

        def fake_project_version(path, default="0.0.0"):
            if path == "/tmp/project/pyproject.toml":
                return "2.6.1"
            return default

        with patch.dict(os.environ, {"DUMB_VERSION": ""}, clear=False):
            with patch("os.getcwd", return_value="/tmp/project"):
                with patch("api.api_state.get_project_version", fake_project_version):
                    self.assertEqual(state._current_dumb_version(), "2.6.1")

    def test_version_classifiers(self):
        state = self._state()

        self.assertTrue(state._is_dev_version("2.5.0-dev.1"))
        self.assertTrue(state._is_dev_version("v2.5.0-dev.1"))
        self.assertFalse(state._is_dev_version("2.5.0"))
        self.assertTrue(state._is_release_version("2.5.0"))
        self.assertTrue(state._is_release_version("v2.5.0"))
        self.assertFalse(state._is_release_version("2.5.0-dev.1"))
        self.assertEqual(state._branch_commit_marker("dev-abcdef1"), "abcdef1")
        self.assertIsNone(state._branch_commit_marker("2.5.0"))

    def test_get_status_details_checks_dumb_api_current_process_health(self):
        state = self._state()
        state._refresh_status_cache = lambda: {}
        state.process_handler = types.SimpleNamespace(
            get_restart_stats=lambda process_name: {"process_name": process_name}
        )

        with patch("api.api_state.os.getpid", return_value=4321):
            with patch.object(
                state, "_check_health", return_value=(True, None)
            ) as check_health:
                details = state.get_status_details("DUMB API", include_health=True)

        check_health.assert_called_once_with("DUMB API", 4321, "running")
        self.assertEqual(details["status"], "running")
        self.assertTrue(details["healthy"])
        self.assertIsNone(details["health_reason"])
        self.assertEqual(details["restart"], {"process_name": "DUMB API"})

    def test_first_run_update_notice_uses_release_url_for_release_version(self):
        state = self._state()
        state._update_notices_file_existed = False
        state._current_dumb_version = lambda: "2.5.0"

        state._ensure_first_run_update_notice()

        notice = state.get_update_notices()["info"][0]
        self.assertEqual(notice["id"], "update-notices-intro:2.5.0")
        self.assertEqual(
            notice["release_url"],
            "https://github.com/I-am-PUID-0/DUMB/releases/tag/2.5.0",
        )
        self.assertEqual(notice["notes_label"], "Release notes")

    def test_first_run_update_notice_uses_commit_url_for_branch_marker(self):
        state = self._state()
        state._update_notices_file_existed = False
        state._current_dumb_version = lambda: "dev-abcdef1"

        state._ensure_first_run_update_notice()

        notice = state.get_update_notices()["info"][0]
        self.assertEqual(
            notice["release_url"], "https://github.com/I-am-PUID-0/DUMB/commit/abcdef1"
        )
        self.assertEqual(notice["notes_label"], "View commit")

    def test_updated_status_records_applied_notice_from_previous_available_version(
        self,
    ):
        state = self._state()
        state.set_update_status(
            "Radarr",
            {
                "status": "available",
                "current_version": "1.0.0",
                "available_version": "1.1.0",
                "checked_at": 100,
            },
        )

        state.set_update_status(
            "Radarr", {"status": "updated", "message": "Updated Radarr."}
        )

        status = state.get_update_status("radarr")
        self.assertEqual(status["previous_version"], "1.0.0")
        self.assertEqual(status["current_version"], "1.1.0")
        notice = state.get_update_notices()["applied"][0]
        self.assertEqual(notice["process_name"], "Radarr")
        self.assertEqual(notice["previous_version"], "1.0.0")
        self.assertEqual(notice["current_version"], "1.1.0")


if __name__ == "__main__":
    unittest.main()
