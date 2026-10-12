import json
from contextlib import contextmanager
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


@contextmanager
def _debrid_config_fixture(
    version, mount_type, api_keys, existing=None, *, rclone_instances=None
):
    """Exercise real atomic config writes without touching managed mounts."""
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        config_path = root / "config.json"
        (root / "version.txt").write_text(version + "\n", encoding="utf-8")
        if existing is not None:
            config_path.write_text(json.dumps(existing), encoding="utf-8")
        config = {
            "config_dir": str(root),
            "config_file": str(config_path),
            "mount_type": mount_type,
            "api_keys": api_keys,
            "port": 8282,
            "log_level": "INFO",
            "branch_enabled": False,
            "release_version_enabled": False,
        }
        values = {
            "decypharr": config,
            "rclone": {"instances": rclone_instances or {}},
            "puid": None,
            "pgid": None,
        }
        manager = types.SimpleNamespace(
            get=lambda key, default=None: values.get(
                key, default if default is not None else {}
            )
        )
        with (
            mock.patch.object(decypharr_settings, "CONFIG_MANAGER", manager),
            mock.patch.object(
                decypharr_settings, "_shutdown_requested", return_value=False
            ),
            mock.patch.object(decypharr_settings.os, "makedirs"),
        ):
            yield config_path, config


class DecypharrSettingsTests(unittest.TestCase):
    def _patch_debrid_fixture(self, config_path):
        changed, error = decypharr_settings.patch_decypharr_config(
            create_if_missing=True, configure_integrations=False
        )
        self.assertIsNone(error)
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
        return changed, json.loads(config_path.read_text(encoding="utf-8"))

    def test_provider_api_defaults_cover_all_bootstrap_and_append_modes(self):
        modes = [
            ("v2.5", "dfs"),
            ("v2.5", "rclone"),
            ("v2.5", "external_rclone"),
            ("v1.1.6", "rclone"),
            ("v1.1.6", "external_rclone"),
        ]
        for version, mount_type in modes:
            for existing in (None, {"debrids": []}):
                with self.subTest(
                    version=version, mount_type=mount_type, existing=existing
                ):
                    with _debrid_config_fixture(
                        version,
                        mount_type,
                        {
                            " ToRBox ": "fixture-torbox-key",
                            "RealDebrid": "fixture-rd-key",
                        },
                        existing,
                    ) as (config_path, _config):
                        changed, rendered = self._patch_debrid_fixture(config_path)
                self.assertTrue(changed)
                entries = {
                    decypharr_settings._debrid_key(entry): entry
                    for entry in rendered["debrids"]
                }
                self.assertEqual(set(entries), {"torbox", "realdebrid"})
                self.assertEqual(entries["torbox"]["rate_limit"], "2/second")
                self.assertEqual(entries["torbox"]["torrents_refresh_interval"], "30m")
                self.assertEqual(entries["realdebrid"]["rate_limit"], "250/minute")
                current = version.startswith("v2")
                self.assertEqual(
                    entries["realdebrid"]["torrents_refresh_interval"],
                    "10m" if current else "15s",
                )
                for entry in entries.values():
                    self.assertEqual(
                        entry["download_links_refresh_interval"],
                        "5m" if current else "40m",
                    )
                    self.assertEqual(entry["workers"], 4000 if current else 50)
                    self.assertEqual(entry["auto_expire_links_after"], "3d")

    def test_missing_torbox_controls_backfill_without_changing_other_tuning(self):
        modes = [
            ("v2.5", "dfs"),
            ("v2.5", "rclone"),
            ("v2.5", "external_rclone"),
            ("v1.1.6", "rclone"),
            ("v1.1.6", "external_rclone"),
        ]
        for version, mount_type in modes:
            for missing in (
                {},
                {"rate_limit": None},
                {"rate_limit": "", "torrents_refresh_interval": ""},
            ):
                entry = {
                    "provider": " ToRBox ",
                    "name": "custom-account",
                    "download_links_refresh_interval": "18m",
                    "workers": 7,
                    "auto_expire_links_after": "9d",
                    **missing,
                }
                with self.subTest(
                    version=version, mount_type=mount_type, missing=missing
                ):
                    with _debrid_config_fixture(
                        version,
                        mount_type,
                        {"TORBOX": "fixture-key"},
                        {"debrids": [entry]},
                    ) as (config_path, _config):
                        _changed, rendered = self._patch_debrid_fixture(config_path)
                self.assertEqual(len(rendered["debrids"]), 1)
                actual = rendered["debrids"][0]
                self.assertEqual(actual["rate_limit"], "2/second")
                self.assertEqual(actual["torrents_refresh_interval"], "30m")
                self.assertEqual(actual["download_links_refresh_interval"], "18m")
                self.assertEqual(actual["workers"], 7)
                self.assertEqual(actual["auto_expire_links_after"], "9d")

    def test_explicit_controls_survive_key_rotation_and_provider_selection(self):
        initial = {
            "debrids": [
                {
                    "name": "TORBOX",
                    "rate_limit": "250/minute",
                    "torrents_refresh_interval": "10m",
                },
                {
                    "provider": "realdebrid",
                    "name": "another-account",
                    "rate_limit": "99/minute",
                    "torrents_refresh_interval": "2h",
                },
            ]
        }
        with _debrid_config_fixture(
            "v2.5", "dfs", {"TorBox": "fixture-first-key"}, initial
        ) as (config_path, config):
            for keys in (
                {"TorBox": "fixture-first-key"},
                {"RealDebrid": "fixture-rd-key"},
                {" torbox ": "fixture-rotated-key", "RealDebrid": "fixture-rd-key"},
            ):
                config["api_keys"] = keys
                _changed, rendered = self._patch_debrid_fixture(config_path)
                entries = {
                    decypharr_settings._debrid_key(entry): entry
                    for entry in rendered["debrids"]
                }
                self.assertEqual(len(entries), 2)
                self.assertEqual(entries["torbox"]["rate_limit"], "250/minute")
                self.assertEqual(entries["torbox"]["torrents_refresh_interval"], "10m")
                self.assertEqual(entries["realdebrid"]["rate_limit"], "99/minute")
                self.assertEqual(
                    entries["realdebrid"]["torrents_refresh_interval"], "2h"
                )
            self.assertEqual(entries["torbox"]["api_key"], "fixture-rotated-key")
            previous = config_path.read_bytes()
            changed, _rendered = self._patch_debrid_fixture(config_path)
            self.assertFalse(changed)
            self.assertEqual(config_path.read_bytes(), previous)

    def test_explicit_zero_and_false_api_controls_are_preserved(self):
        for version, mount_type in (
            ("v2.5", "dfs"),
            ("v1.1.6", "rclone"),
            ("v1.1.6", "external_rclone"),
        ):
            with self.subTest(version=version, mount_type=mount_type):
                with _debrid_config_fixture(
                    version,
                    mount_type,
                    {"torbox": "fixture-key"},
                    {
                        "debrids": [
                            {
                                "name": "torbox",
                                "rate_limit": 0,
                                "torrents_refresh_interval": False,
                            }
                        ]
                    },
                ) as (config_path, _config):
                    _changed, rendered = self._patch_debrid_fixture(config_path)
            self.assertIs(type(rendered["debrids"][0]["rate_limit"]), int)
            self.assertEqual(rendered["debrids"][0]["rate_limit"], 0)
            self.assertIs(rendered["debrids"][0]["torrents_refresh_interval"], False)

    def test_legacy_embedded_other_provider_missing_controls_stay_untouched(self):
        with _debrid_config_fixture(
            "v1.1.6",
            "rclone",
            {"realdebrid": "fixture-key"},
            {"debrids": [{"name": "realdebrid"}]},
        ) as (config_path, _config):
            _changed, rendered = self._patch_debrid_fixture(config_path)
        self.assertNotIn("rate_limit", rendered["debrids"][0])
        self.assertNotIn("torrents_refresh_interval", rendered["debrids"][0])

    def test_other_provider_api_backfill_keeps_existing_mode_behavior(self):
        for version, mount_type in (
            ("v2.5", "dfs"),
            ("v1.1.6", "external_rclone"),
            ("v1.1.6", "rclone"),
        ):
            with self.subTest(version=version, mount_type=mount_type):
                with _debrid_config_fixture(
                    version,
                    mount_type,
                    {"realdebrid": "fixture-key"},
                    {
                        "debrids": [
                            {
                                "name": "realdebrid",
                                "rate_limit": 0,
                                "torrents_refresh_interval": False,
                            }
                        ]
                    },
                ) as (config_path, _config):
                    _changed, rendered = self._patch_debrid_fixture(config_path)
                entry = rendered["debrids"][0]
                if version == "v1.1.6" and mount_type == "rclone":
                    self.assertEqual(entry["rate_limit"], 0)
                    self.assertIs(entry["torrents_refresh_interval"], False)
                else:
                    self.assertEqual(entry["rate_limit"], "250/minute")
                    self.assertEqual(
                        entry["torrents_refresh_interval"],
                        "10m" if version == "v2.5" else "15s",
                    )

    def test_torbox_default_application_is_idempotent_on_repatch(self):
        for version, mount_type in (
            ("v2.5", "dfs"),
            ("v2.5", "rclone"),
            ("v2.5", "external_rclone"),
            ("v1.1.6", "rclone"),
        ):
            with self.subTest(version=version, mount_type=mount_type):
                with _debrid_config_fixture(
                    version, mount_type, {"torbox": "fixture-key"}
                ) as (config_path, _config):
                    first, _rendered = self._patch_debrid_fixture(config_path)
                    previous = config_path.read_bytes()
                    second, _rendered = self._patch_debrid_fixture(config_path)
                    self.assertTrue(first)
                    self.assertFalse(second)
                    self.assertEqual(config_path.read_bytes(), previous)

    def test_fresh_external_mount_has_complete_rc_settings_on_first_write(self):
        rclone_instance = {
            "enabled": True,
            "command": ["rclone", "mount", "fixture:", "--rc-addr", ":5588"],
        }
        with _debrid_config_fixture(
            "v2.5",
            "external_rclone",
            {"torbox": "fixture-key"},
            rclone_instances={"Decypharr": rclone_instance},
        ) as (config_path, _config):
            with mock.patch.object(
                decypharr_settings, "has_core_service", return_value=True
            ):
                _changed, rendered = self._patch_debrid_fixture(config_path)
        self.assertEqual(rendered["mount"]["type"], "external_rclone")
        self.assertEqual(
            rendered["mount"]["external_rclone"],
            {
                "rc_url": "http://127.0.0.1:5588",
                "rc_username": "",
                "rc_password": "",
            },
        )

    def test_minimal_bootstrap_preserves_prepared_custom_mount_settings(self):
        mount_settings = {
            "external_rclone": {
                "rc_username": "example-user",
                "rc_password": "example-password",
            },
            "rclone": {"vfs_cache_mode": "full", "dir_cache_time": "45m"},
            "dfs": {"cache_dir": "/cache/example", "allow_other": False},
        }
        for mount_type, settings in mount_settings.items():
            initial = {
                "mount": {
                    "type": mount_type,
                    "mount_path": "/mnt/debrid/decypharr",
                    "read_only": True,
                    mount_type: settings,
                }
            }
            with self.subTest(mount_type=mount_type):
                with _debrid_config_fixture(
                    "v2.5", mount_type, {"torbox": "fixture-key"}, initial
                ) as (config_path, _config):
                    _changed, rendered = self._patch_debrid_fixture(config_path)
                self.assertIs(rendered["mount"]["read_only"], True)
                for key, expected in settings.items():
                    self.assertEqual(rendered["mount"][mount_type][key], expected)
                if mount_type == "external_rclone":
                    self.assertEqual(
                        rendered["mount"][mount_type]["rc_url"], "http://127.0.0.1:5572"
                    )

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
