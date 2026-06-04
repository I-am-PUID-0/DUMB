import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import altmount_settings
from utils.altmount_settings import (
    download_altmount_binary,
    sync_altmount_managed_config,
    write_altmount_default_config,
)


class AltMountSetupTests(unittest.TestCase):
    def test_write_default_config_creates_expected_paths_and_preserves_existing_file(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_file = root / "config.yaml"
            metadata_dir = root / "metadata"
            log_file = root / "logs" / "altmount.log"
            mount_path = root / "mount"
            config = {
                "config_dir": str(root),
                "config_file": str(config_file),
                "metadata_dir": str(metadata_dir),
                "mount_path": str(mount_path),
                "log_file": str(log_file),
                "port": 8088,
                "log_level": "info",
            }

            write_altmount_default_config(config)

            rendered = config_file.read_text()
            self.assertIn("webdav:", rendered)
            self.assertIn("  port: 8088", rendered)
            self.assertIn(f"  root_path: {metadata_dir}", rendered)
            self.assertIn(f"mount_path: {mount_path}", rendered)
            self.assertIn("mount_type: rclone", rendered)
            self.assertIn("  mount_enabled: true", rendered)
            self.assertIn("  rc_enabled: true", rendered)
            self.assertIn(f"  file: {log_file}", rendered)
            self.assertIn("providers: []", rendered)

            config_file.write_text("custom: true\n")
            write_altmount_default_config(config)
            self.assertEqual("custom: true\n", config_file.read_text())

    def test_sync_managed_config_updates_mount_type(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_file = root / "config.yaml"
            config_file.write_text(
                """
mount_path: /old
rclone:
  path: /old/rclone
  mount_enabled: true
  rc_enabled: false
api:
  prefix: /api
sabnzbd:
  enabled: true
  categories: []
arrs:
  enabled: true
""".lstrip(),
                encoding="utf-8",
            )
            config = {
                "config_dir": str(root),
                "config_file": str(config_file),
                "mount_path": str(root / "mount"),
                "mount_type": "dfs",
            }

            sync_altmount_managed_config(config)

            rendered = altmount_settings._load_yaml(str(config_file))
            self.assertEqual(str(root / "mount"), rendered["mount_path"])
            self.assertEqual("fuse", rendered["mount_type"])
            self.assertFalse(rendered["rclone"]["mount_enabled"])
            self.assertFalse(rendered["rclone"]["rc_enabled"])
            self.assertEqual(str(root / "rclone"), rendered["rclone"]["path"])

    def test_external_rclone_maps_to_altmount_external_rclone_mount_type(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_file = root / "config.yaml"
            config = {
                "config_dir": str(root),
                "config_file": str(config_file),
                "metadata_dir": str(root / "metadata"),
                "mount_path": str(root / "mount"),
                "log_file": str(root / "logs" / "altmount.log"),
                "mount_type": "external_rclone",
                "port": 8088,
                "log_level": "info",
            }

            write_altmount_default_config(config)

            rendered = altmount_settings._load_yaml(str(config_file))
            self.assertEqual("rclone_external", rendered["mount_type"])
            self.assertFalse(rendered["rclone"]["mount_enabled"])
            self.assertTrue(rendered["rclone"]["rc_enabled"])

    def test_download_binary_extracts_matching_linux_archive_asset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target_bin = root / "altmount"
            archive_bytes = io.BytesIO()
            with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
                payload = b"#!/bin/sh\necho altmount\n"
                info = tarfile.TarInfo("altmount-cli-linux-amd64")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            archive_payload = archive_bytes.getvalue()

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def raise_for_status(self):
                    return None

                def iter_content(self, chunk_size):
                    del chunk_size
                    yield archive_payload

            release_info = {
                "tag_name": "v0.2.0",
                "assets": [
                    {
                        "name": "altmount-cli_v0.2.0_linux_amd64.tar.gz",
                        "browser_download_url": "https://example.test/altmount.tar.gz",
                    }
                ],
            }
            config = {
                "repo_owner": "javi11",
                "repo_name": "altmount",
                "pinned_version": "latest",
            }

            with (
                patch.object(
                    altmount_settings.downloader,
                    "get_latest_release",
                    return_value=("v0.2.0", None),
                ),
                patch.object(
                    altmount_settings.downloader,
                    "fetch_github_release_info",
                    return_value=(release_info, None),
                ),
                patch.object(
                    altmount_settings.downloader,
                    "get_architecture",
                    return_value="linux-amd64",
                ),
                patch.object(
                    altmount_settings.requests, "get", return_value=FakeResponse()
                ),
                patch.object(altmount_settings.logger, "info") as log_info,
            ):
                success, error = download_altmount_binary(config, str(target_bin))

            self.assertTrue(success, error)
            self.assertTrue(target_bin.exists())
            self.assertTrue(target_bin.stat().st_mode & 0o111)
            self.assertEqual("v0.2.0", (root / "version.txt").read_text())
            log_info.assert_called_once_with(
                "Downloading AltMount %s from %s",
                "v0.2.0",
                "https://example.test/altmount.tar.gz",
            )


if __name__ == "__main__":
    unittest.main()
