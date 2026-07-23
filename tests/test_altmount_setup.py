import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

from utils import altmount_settings
from utils import setup as setup_module
from utils.altmount_settings import (
    download_altmount_binary,
    prepare_altmount_mount_path,
    sync_altmount_managed_config,
    write_altmount_default_config,
)


class AltMountSetupTests(unittest.TestCase):
    def test_setup_normalizes_nested_rclone_ownership(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            binary = root / "altmount"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            rclone_dir = root / "rclone"
            rclone_dir.mkdir()
            config = {
                "enabled": True,
                "process_name": "AltMount",
                "config_dir": str(root),
                "config_file": str(root / "config.yaml"),
                "metadata_dir": str(root / "metadata"),
                "log_file": str(root / "logs" / "altmount.log"),
                "mount_path": str(root / "mount"),
                "mount_type": "none",
                "port": 8088,
                "pinned_version": "latest",
                "env": {
                    "JWT_SECRET": "jwt-secret",
                    "ALTMOUNT_API_KEY": "api-key",
                    "PUID": "1000",
                    "PGID": "1000",
                    "PORT": "8088",
                    "COOKIE_DOMAIN": "localhost",
                },
                "command": [
                    str(binary),
                    "serve",
                    "--config",
                    str(root / "config.yaml"),
                ],
            }
            manager = MagicMock()
            manager.get.side_effect = lambda key, default=None: {
                "altmount": config,
                "postgres": {},
                "puid": 1000,
                "pgid": 1000,
            }.get(key, default)

            with (
                patch.object(setup_module, "CONFIG_MANAGER", manager),
                patch.object(
                    altmount_settings,
                    "prepare_altmount_mount_path",
                    return_value=(True, None),
                ),
                patch.object(altmount_settings, "write_altmount_default_config"),
                patch.object(altmount_settings, "sync_altmount_managed_config"),
                patch.object(setup_module, "service_postgres_database_name"),
                patch.object(setup_module, "apply_service_postgres_config"),
                patch.object(setup_module, "_chown_recursive_if_needed"),
                patch.object(
                    setup_module, "chown_recursive", return_value=(True, None)
                ) as recursive_chown,
            ):
                success, error = setup_module.setup_altmount(object())

            self.assertTrue(success, error)
            recursive_chown.assert_called_once_with(str(rclone_dir), 1000, 1000)

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

    def test_release_update_uses_altmount_binary_asset_installer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target_bin = Path(tmpdir) / "altmount"
            target_bin.write_text("old binary", encoding="utf-8")
            config = {
                "config_dir": tmpdir,
                "repo_owner": "javi11",
                "repo_name": "altmount",
                "pinned_version": "latest",
                "release_version": "v0.3.2",
            }

            with (
                patch.object(
                    altmount_settings,
                    "download_altmount_binary",
                    return_value=(True, None),
                ) as binary_download,
                patch.object(
                    setup_module.downloader, "download_release_version"
                ) as generic_download,
            ):
                success, error = setup_module.setup_release_version(
                    object(), config, "AltMount", "altmount"
                )

            self.assertTrue(success, error)
            generic_download.assert_not_called()
            binary_download.assert_called_once()
            release_config, downloaded_path = binary_download.call_args.args
            self.assertEqual("v0.3.2", release_config["pinned_version"])
            self.assertEqual(str(target_bin), downloaded_path)
            self.assertEqual("latest", config["pinned_version"])

    def test_prepare_mount_path_unmounts_existing_internal_mount(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mount_path = Path(tmpdir) / "mount"
            mount_path.mkdir()
            completed = type(
                "CompletedProcess",
                (),
                {"returncode": 0, "stdout": "", "stderr": ""},
            )()

            with (
                patch.object(
                    altmount_settings,
                    "_altmount_mount_filesystem",
                    return_value="fuse.rclone",
                ),
                patch.object(
                    altmount_settings.subprocess,
                    "run",
                    return_value=completed,
                ) as unmount,
            ):
                success, error = prepare_altmount_mount_path(
                    str(mount_path), "rclone", cleanup_internal_mount=True
                )

            self.assertTrue(success, error)
            unmount.assert_called_once_with(
                ["umount", str(mount_path)],
                capture_output=True,
                text=True,
                timeout=15,
            )

    def test_mount_filesystem_reads_exact_mountinfo_entry(self):
        mountinfo = (
            "42 31 0:123 / /mnt/debrid/altmount rw,nosuid shared:7 - "
            "fuse.rclone rclone rw\n"
        )

        with patch("builtins.open", mock_open(read_data=mountinfo)):
            filesystem = altmount_settings._altmount_mount_filesystem(
                "/mnt/debrid/altmount"
            )

        self.assertEqual("fuse.rclone", filesystem)

    def test_prepare_mount_path_preserves_external_rclone_mount(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mount_path = Path(tmpdir) / "mount"
            mount_path.mkdir()

            with (
                patch.object(
                    altmount_settings,
                    "_altmount_mount_filesystem",
                    return_value="fuse.rclone",
                ),
                patch.object(altmount_settings.subprocess, "run") as unmount,
            ):
                success, error = prepare_altmount_mount_path(
                    str(mount_path),
                    "external_rclone",
                    cleanup_internal_mount=True,
                )

            self.assertTrue(success, error)
            unmount.assert_not_called()

    def test_prepare_mount_path_preserves_non_fuse_bind_mount(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mount_path = Path(tmpdir) / "mount"
            mount_path.mkdir()

            with (
                patch.object(
                    altmount_settings,
                    "_altmount_mount_filesystem",
                    return_value="ext4",
                ),
                patch.object(altmount_settings.subprocess, "run") as unmount,
            ):
                success, error = prepare_altmount_mount_path(
                    str(mount_path), "rclone", cleanup_internal_mount=True
                )

            self.assertTrue(success, error)
            unmount.assert_not_called()

    def test_prepare_mount_path_reports_dangling_symlink_without_removing_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mount_path = Path(tmpdir) / "mount"
            missing_target = Path(tmpdir) / "missing"
            mount_path.symlink_to(missing_target, target_is_directory=True)

            success, error = prepare_altmount_mount_path(
                str(mount_path), "rclone", cleanup_internal_mount=True
            )

            self.assertFalse(success)
            self.assertIn("dangling symbolic link", error)
            self.assertTrue(mount_path.is_symlink())


if __name__ == "__main__":
    unittest.main()
