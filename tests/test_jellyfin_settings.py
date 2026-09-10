import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import defusedxml.ElementTree as ET

from utils import jellyfin_settings


class JellyfinSettingsTests(unittest.TestCase):
    def test_setup_rebuilds_directories_without_discarding_ffmpeg(self):
        from utils import setup

        with tempfile.TemporaryDirectory() as directory:
            config = {
                "enabled": True,
                "config_dir": directory,
                "command": ["jellyfin", "--ffmpeg", "/custom/ffmpeg"],
                "env": {"LIBVA_DRIVER_NAME": "iHD"},
                "port": 8096,
            }
            with (
                patch.object(
                    setup.CONFIG_MANAGER,
                    "get",
                    side_effect=lambda key, default=None: (
                        config if key == "jellyfin" else 1000
                    ),
                ),
                patch.object(setup.os.path, "exists", return_value=True),
                patch.object(setup, "chown_recursive"),
                patch.object(jellyfin_settings, "patch_jellyfin_config"),
            ):
                success, error = setup.setup_jellyfin(configure_only=True)
        self.assertTrue(success, error)
        self.assertIn("--datadir", config["command"])
        index = config["command"].index("--ffmpeg")
        self.assertEqual("/custom/ffmpeg", config["command"][index + 1])

    def test_runtime_prefers_packaged_ffmpeg_and_keeps_selection_on_reconfigure(self):
        config = {"command": [], "env": {}}
        with (
            patch.object(jellyfin_settings.os.path, "isfile", return_value=True),
            patch.object(jellyfin_settings.Path, "glob", return_value=[]),
        ):
            for _ in range(2):
                jellyfin_settings.configure_jellyfin_runtime(config, ["jellyfin"])
        self.assertEqual(
            ["jellyfin", "--ffmpeg", "/usr/lib/jellyfin-ffmpeg/ffmpeg"],
            config["command"],
        )

    def test_runtime_preserves_explicit_ffmpeg_and_driver(self):
        for command in (
            ["jellyfin", "--ffmpeg", "/custom/ffmpeg"],
            "jellyfin --ffmpeg=/custom/ffmpeg",
        ):
            config = {"command": command, "env": {"LIBVA_DRIVER_NAME": "i965"}}
            jellyfin_settings.configure_jellyfin_runtime(config, ["jellyfin"])
            self.assertEqual(
                ["jellyfin", "--ffmpeg", "/custom/ffmpeg"], config["command"]
            )
            self.assertEqual("i965", config["env"]["LIBVA_DRIVER_NAME"])

    def test_driver_default_requires_only_intel_and_installed_ihd(self):
        for vendors, driver, expected in (
            (["0x8086"], True, "iHD"),
            (["0x1002"], True, None),
            (["0x8086", "0x1002"], True, None),
            ([], True, None),
            (["0x8086"], False, None),
        ):
            with self.subTest(vendors=vendors, driver=driver):
                config = {"env": {}}
                paths = [
                    Mock(read_text=Mock(return_value=vendor)) for vendor in vendors
                ]
                with (
                    patch.dict(jellyfin_settings.os.environ, {}, clear=True),
                    patch.object(
                        jellyfin_settings.os.path, "isfile", return_value=True
                    ),
                    patch.object(
                        jellyfin_settings.Path,
                        "glob",
                        side_effect=[paths, [Mock()] if driver else []],
                    ),
                ):
                    jellyfin_settings.configure_jellyfin_runtime(config, ["jellyfin"])
                self.assertEqual(expected, config["env"].get("LIBVA_DRIVER_NAME"))

    def test_missing_network_xml_is_created_with_requested_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                jellyfin_settings.CONFIG_MANAGER,
                "get",
                return_value={"config_dir": temp_dir, "port": 18096},
            ):
                updated, error = jellyfin_settings.patch_jellyfin_config()

            path = Path(temp_dir) / "config" / "network.xml"
            root = ET.parse(path).getroot()

        self.assertTrue(updated, error)
        self.assertEqual("18096", root.findtext("InternalHttpPort"))
        self.assertEqual("18096", root.findtext("PublicHttpPort"))


if __name__ == "__main__":
    unittest.main()
