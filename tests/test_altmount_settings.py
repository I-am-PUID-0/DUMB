import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from utils import altmount_settings


class FakeConfigManager:
    def __init__(self, data):
        self.data = data

    def get(self, key, default=None):
        return self.data.get(key, default)


class AltMountSettingsTests(unittest.TestCase):
    def test_collect_arr_entries_filters_enabled_altmount_instances(self):
        fake_config = FakeConfigManager(
            {
                "sonarr": {
                    "instances": {
                        "Usenet": {
                            "enabled": True,
                            "core_service": "altmount",
                            "port": 8990,
                            "config_file": "/sonarr/config.xml",
                            "instance_name": "Usenet",
                        },
                        "NzbDAV": {
                            "enabled": True,
                            "core_service": "nzbdav",
                            "port": 8991,
                        },
                    }
                },
                "radarr": {"instances": {}},
                "lidarr": {"instances": {}},
                "whisparr": {"instances": {}},
            }
        )

        with (
            patch.object(altmount_settings, "CONFIG_MANAGER", fake_config),
            patch.object(
                altmount_settings, "_parse_arr_api_key", return_value="arr-token"
            ),
        ):
            entries = altmount_settings._collect_arr_entries()

        self.assertEqual(
            entries,
            [
                {
                    "service": "sonarr",
                    "name": "sonarr:Usenet",
                    "host": "http://127.0.0.1:8990",
                    "token": "arr-token",
                }
            ],
        )

    def test_sync_altmount_config_enables_sabnzbd_and_arr_instances(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            config_file.write_text(
                yaml.safe_dump(
                    {
                        "api": {"prefix": "/api", "key_override": "old"},
                        "sabnzbd": {"enabled": False, "categories": []},
                        "arrs": {"enabled": False},
                    }
                ),
                encoding="utf-8",
            )
            config = {
                "config_file": str(config_file),
                "env": {"ALTMOUNT_API_KEY": "A" * 33},
            }
            entries = [
                {
                    "service": "radarr",
                    "name": "radarr:Usenet",
                    "host": "http://127.0.0.1:7879",
                    "token": "radarr-token",
                }
            ]

            with patch.object(altmount_settings.logger, "info") as log_info:
                altmount_settings._sync_altmount_config(config, entries)

            rendered = yaml.safe_load(config_file.read_text(encoding="utf-8"))
            self.assertEqual(rendered["api"]["key_override"], "A" * 33)
            self.assertTrue(rendered["sabnzbd"]["enabled"])
            self.assertIn(
                "movies",
                [category["name"] for category in rendered["sabnzbd"]["categories"]],
            )
            self.assertTrue(rendered["arrs"]["enabled"])
            self.assertEqual(
                rendered["arrs"]["radarr_instances"],
                [
                    {
                        "name": "radarr:Usenet",
                        "url": "http://127.0.0.1:7879",
                        "api_key": "radarr-token",
                        "enabled": True,
                    }
                ],
            )
            log_info.assert_called_once_with(
                "Updated AltMount Arr integration config at %s",
                str(config_file),
            )

    def test_sync_altmount_config_prunes_only_stale_dumb_managed_arr_instances(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.yaml"
            config_file.write_text(
                yaml.safe_dump(
                    {
                        "arrs": {
                            "enabled": True,
                            "radarr_instances": [
                                {
                                    "name": "radarr:Old",
                                    "url": "http://127.0.0.1:7878",
                                    "api_key": "old",
                                    "enabled": True,
                                },
                                {
                                    "name": "Remote Radarr",
                                    "url": "https://radarr.example.invalid",
                                    "api_key": "custom",
                                    "enabled": True,
                                },
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = {
                "config_file": str(config_file),
                "env": {"ALTMOUNT_API_KEY": "A" * 33},
            }
            entries = [
                {
                    "service": "radarr",
                    "name": "radarr:Current",
                    "host": "http://127.0.0.1:7879",
                    "token": "current",
                }
            ]

            altmount_settings._sync_altmount_config(config, entries)

            rendered = yaml.safe_load(config_file.read_text(encoding="utf-8"))
            instances = rendered["arrs"]["radarr_instances"]
            self.assertEqual(
                [item["name"] for item in instances],
                ["Remote Radarr", "radarr:Current"],
            )

    def test_patch_altmount_arr_integration_prunes_stale_entries_when_none_linked(
        self,
    ):
        config = {"enabled": True}
        with (
            patch.object(
                altmount_settings.CONFIG_MANAGER,
                "get",
                return_value=config,
            ),
            patch.object(altmount_settings, "_collect_arr_entries", return_value=[]),
            patch.object(altmount_settings, "_sync_altmount_config") as sync_config,
        ):
            success, error = altmount_settings.patch_altmount_arr_integration()

        self.assertTrue(success, error)
        sync_config.assert_called_once_with(config, [])


if __name__ == "__main__":
    unittest.main()
