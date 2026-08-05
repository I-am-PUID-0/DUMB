import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import setup


class SetupZurgTests(unittest.TestCase):
    def test_install_only_creates_target_for_dangling_data_symlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_target = root / "data" / "zurg_RD"
            config_link = root / "zurg" / "RD"
            config_link.parent.mkdir()
            config_link.symlink_to(data_target)
            instance = {
                "enabled": True,
                "process_name": "Zurg w/ RealDebrid",
                "repo_owner": "debridmediamanager",
                "repo_name": "zurg-testing",
                "release_version_enabled": True,
                "release_version": "v0.9.3-hotfix.11",
                "config_dir": str(config_link),
                "exclude_dirs": [],
                "api_key": "",
            }

            def config_get(key):
                if key == "zurg":
                    return {"instances": {"RealDebrid": instance}}
                return 1000

            def download(**kwargs):
                binary = Path(kwargs["target_dir"]) / "zurg"
                binary.write_bytes(b"binary")
                return True, None

            with (
                patch.object(setup.CONFIG_MANAGER, "get", side_effect=config_get),
                patch.object(setup, "chown_recursive"),
                patch.object(
                    setup.downloader,
                    "download_release_version",
                    side_effect=download,
                ),
                patch.object(setup.downloader, "set_permissions") as permissions,
            ):
                success, error = setup.zurg_setup(install_only=True)

            self.assertTrue(success, error)
            self.assertTrue(data_target.is_dir())
            permissions.assert_called_once_with(str(config_link / "zurg"), 0o755)


if __name__ == "__main__":
    unittest.main()
