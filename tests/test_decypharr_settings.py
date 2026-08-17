import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_STUBBED_MODULES = [
    "utils.global_logger",
    "utils.config_loader",
    "utils.core_services",
    "utils.url_security",
    "utils.versions",
    "fastapi",
]
_PREVIOUS_MODULES = {name: sys.modules.get(name) for name in _STUBBED_MODULES}


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def _install_runtime_stubs():
    import urllib.request

    global_logger = types.ModuleType("utils.global_logger")
    global_logger.logger = _Logger()
    sys.modules["utils.global_logger"] = global_logger

    config_loader = types.ModuleType("utils.config_loader")
    config_loader.CONFIG_MANAGER = types.SimpleNamespace(
        get=lambda *args, **kwargs: None
    )
    sys.modules["utils.config_loader"] = config_loader

    core_services = types.ModuleType("utils.core_services")
    core_services.get_core_services = lambda _config: []
    core_services.has_core_service = lambda _config, _service: False
    sys.modules["utils.core_services"] = core_services

    url_security = types.ModuleType("utils.url_security")
    url_security.safe_request = urllib.request.Request
    url_security.safe_urlopen = urllib.request.urlopen
    sys.modules["utils.url_security"] = url_security

    versions = types.ModuleType("utils.versions")
    versions.Versions = lambda: types.SimpleNamespace(
        is_latest_release_gt=lambda *args, **kwargs: (False, None, None)
    )
    sys.modules["utils.versions"] = versions


_install_runtime_stubs()

fastapi_stub = sys.modules.get("fastapi")
if fastapi_stub is not None and not hasattr(fastapi_stub, "WebSocket"):
    fastapi_stub.WebSocket = object
elif fastapi_stub is None:
    fastapi_stub = types.ModuleType("fastapi")
    fastapi_stub.WebSocket = object
    sys.modules["fastapi"] = fastapi_stub

from utils import decypharr_settings
from utils.decypharr_settings import _collect_arr_entries, _uses_combined_root

for module_name, previous_module in _PREVIOUS_MODULES.items():
    if previous_module is None:
        sys.modules.pop(module_name, None)
    else:
        sys.modules[module_name] = previous_module


class DecypharrSettingsTests(unittest.TestCase):
    def test_legacy_release_marker_keeps_legacy_config_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            (config_dir / "version.txt").write_text("v1.1.6\n", encoding="utf-8")

            current_schema, source = (
                decypharr_settings._uses_current_decypharr_config_schema(
                    {},
                    {
                        "config_dir": str(config_dir),
                        "release_version_enabled": False,
                        "branch_enabled": False,
                        "commit_sha": "",
                    },
                )
            )

            self.assertFalse(current_schema)
            self.assertEqual(source, "installed version v1.1.6")

    def test_fresh_current_release_configures_premiumize_before_first_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            config_path = config_dir / "config.json"
            (config_dir / "version.txt").write_text("v2.5\n", encoding="utf-8")
            decypharr_config = {
                "config_dir": str(config_dir),
                "config_file": str(config_path),
                "repo_owner": "sirrobot01",
                "repo_name": "decypharr",
                "release_version_enabled": False,
                "branch_enabled": False,
                "commit_sha": "",
                "mount_type": "dfs",
                "mount_path": "/mnt/debrid/decypharr",
                "api_keys": {"Premiumize": "test-premiumize-key"},
                "log_level": "INFO",
                "port": 8282,
            }

            class _ConfigManager:
                def get(self, key, default=None):
                    values = {
                        "decypharr": decypharr_config,
                        "rclone": {"instances": {}},
                        "puid": None,
                        "pgid": None,
                    }
                    return values.get(key, default if default is not None else {})

            original_config_manager = decypharr_settings.CONFIG_MANAGER
            original_shutdown_requested = decypharr_settings._shutdown_requested
            try:
                decypharr_settings.CONFIG_MANAGER = _ConfigManager()
                decypharr_settings._shutdown_requested = lambda: False
                with mock.patch.object(decypharr_settings.os, "makedirs"):
                    updated, error = decypharr_settings.patch_decypharr_config(
                        create_if_missing=True,
                        configure_integrations=False,
                    )
            finally:
                decypharr_settings.CONFIG_MANAGER = original_config_manager
                decypharr_settings._shutdown_requested = original_shutdown_requested

            self.assertTrue(updated)
            self.assertIsNone(error)
            rendered = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                rendered["download_folder"], "/mnt/debrid/decypharr_downloads"
            )
            self.assertEqual(rendered["mount"]["type"], "dfs")
            self.assertEqual(rendered["mount"]["mount_path"], "/mnt/debrid/decypharr")
            self.assertEqual(len(rendered["debrids"]), 1)
            self.assertEqual(rendered["debrids"][0]["provider"], "premiumize")
            self.assertEqual(rendered["debrids"][0]["api_key"], "test-premiumize-key")
            self.assertNotIn("qbittorrent", rendered)
            self.assertNotIn("sabnzbd", rendered)
            self.assertFalse(decypharr_config["branch_enabled"])
            self.assertFalse(decypharr_config["release_version_enabled"])
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)

    def test_combined_root_requires_decypharr_plus_companion_workflow(self):
        self.assertFalse(_uses_combined_root(["decypharr"]))
        self.assertFalse(_uses_combined_root(["infinidysk", "altmount"]))
        self.assertTrue(_uses_combined_root(["decypharr", "infinidysk"]))
        self.assertTrue(_uses_combined_root(["decypharr", "altmount"]))
        self.assertTrue(_uses_combined_root(["Decypharr", " AltMount "]))

    def test_collect_arr_entries_preserves_existing_download_uncached_without_override(
        self,
    ):
        sonarr_cfg = {
            "instances": {
                "sonarr-main": {
                    "enabled": True,
                    "instance_name": "Main",
                    "port": 8989,
                    "config_file": "/tmp/missing-config.xml",
                }
            }
        }

        original_config_manager = decypharr_settings.CONFIG_MANAGER
        original_has_core_service = decypharr_settings.has_core_service
        saved_parser = decypharr_settings._parse_arr_api_key
        try:
            decypharr_settings.CONFIG_MANAGER = types.SimpleNamespace(
                get=lambda key, default=None: sonarr_cfg if key == "sonarr" else {}
            )
            decypharr_settings.has_core_service = lambda _config, service: (
                service == "decypharr"
            )
            decypharr_settings._parse_arr_api_key = lambda _path: "sonarr-token"

            entries = _collect_arr_entries(
                {},
                [
                    {
                        "name": "sonarr:Main",
                        "host": "http://127.0.0.1:8989",
                        "token": "old-token",
                        "download_uncached": True,
                    }
                ],
            )
        finally:
            decypharr_settings.CONFIG_MANAGER = original_config_manager
            decypharr_settings.has_core_service = original_has_core_service
            decypharr_settings._parse_arr_api_key = saved_parser

        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["download_uncached"])
        self.assertEqual(entries[0]["token"], "sonarr-token")

    def test_collect_arr_entries_uses_explicit_download_uncached_override(self):
        sonarr_cfg = {
            "instances": {
                "sonarr-main": {
                    "enabled": True,
                    "instance_name": "Main",
                    "port": 8989,
                    "config_file": "/tmp/missing-config.xml",
                }
            }
        }

        original_config_manager = decypharr_settings.CONFIG_MANAGER
        original_has_core_service = decypharr_settings.has_core_service
        saved_parser = decypharr_settings._parse_arr_api_key
        try:
            decypharr_settings.CONFIG_MANAGER = types.SimpleNamespace(
                get=lambda key, default=None: sonarr_cfg if key == "sonarr" else {}
            )
            decypharr_settings.has_core_service = lambda _config, service: (
                service == "decypharr"
            )
            decypharr_settings._parse_arr_api_key = lambda _path: "sonarr-token"

            entries = _collect_arr_entries(
                {"arrs_download_uncached": False},
                [{"name": "sonarr:Main", "download_uncached": True}],
            )
        finally:
            decypharr_settings.CONFIG_MANAGER = original_config_manager
            decypharr_settings.has_core_service = original_has_core_service
            decypharr_settings._parse_arr_api_key = saved_parser

        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["download_uncached"])


if __name__ == "__main__":
    unittest.main()
