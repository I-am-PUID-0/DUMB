import sys
import types
import unittest

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
    def test_combined_root_requires_decypharr_plus_companion_workflow(self):
        self.assertFalse(_uses_combined_root(["decypharr"]))
        self.assertFalse(_uses_combined_root(["nzbdav", "altmount"]))
        self.assertTrue(_uses_combined_root(["decypharr", "nzbdav"]))
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
