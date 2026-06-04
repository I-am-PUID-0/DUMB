import io
import os
import sys
import tarfile
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _ConfigManager:
    def __init__(self):
        self.values = {"dumb": {}}

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeResponse:
    def __init__(self, status_code, headers=None, content=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content


def _install_runtime_stubs():
    global_logger = types.ModuleType("utils.global_logger")
    global_logger.logger = _Logger()
    sys.modules["utils.global_logger"] = global_logger

    config_loader = types.ModuleType("utils.config_loader")
    config_loader.CONFIG_MANAGER = _ConfigManager()
    sys.modules["utils.config_loader"] = config_loader

    requests_stub = types.ModuleType("requests")
    requests_stub.RequestException = Exception
    requests_stub.get = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_stub


_install_runtime_stubs()

sys.modules.pop("utils.download", None)
utils_pkg = sys.modules.get("utils")
if utils_pkg is not None and hasattr(utils_pkg, "download"):
    delattr(utils_pkg, "download")
from utils import download


class DownloaderHelperTests(unittest.TestCase):
    def setUp(self):
        download.CONFIG_MANAGER.values = {"dumb": {}}
        self.downloader = download.Downloader()

    def test_get_headers_uses_accept_header_without_token(self):
        self.assertEqual(
            self.downloader.get_headers(),
            {"Accept": "application/vnd.github.v3+json"},
        )

    def test_get_headers_uses_authorization_when_token_configured(self):
        download.CONFIG_MANAGER.values = {"dumb": {"github_token": "secret-token"}}

        self.assertEqual(
            self.downloader.get_headers(), {"Authorization": "token secret-token"}
        )

    def test_normalize_arch_maps_common_architectures(self):
        self.assertEqual(download.Downloader.normalize_arch("linux-x64"), "linux_x64")
        self.assertEqual(
            download.Downloader.normalize_arch("linux-arm64"), "linux_arm64"
        )
        self.assertEqual(download.Downloader.normalize_arch("linux-arm"), "linux_arm")
        self.assertEqual(download.Downloader.normalize_arch("amd64"), "amd64")

    def test_find_asset_download_url_prefers_matching_non_musl_asset(self):
        release_info = {
            "tag_name": "v1.0.0",
            "assets": [
                {
                    "id": 1,
                    "name": "app-linux-musl-x64.zip",
                    "browser_download_url": "musl",
                },
                {"id": 2, "name": "app-linux-x64.zip", "browser_download_url": "glibc"},
            ],
        }

        self.assertEqual(
            self.downloader.find_asset_download_url(release_info, "linux-x64"),
            ("glibc", 2),
        )

    def test_find_asset_download_url_prefers_musl_when_requested(self):
        release_info = {
            "tag_name": "v1.0.0",
            "assets": [
                {"id": 1, "name": "app-linux-x64.zip", "browser_download_url": "glibc"},
                {
                    "id": 2,
                    "name": "app-linux-musl-x64.zip",
                    "browser_download_url": "musl",
                },
            ],
        }

        self.assertEqual(
            self.downloader.find_asset_download_url(release_info, "linux-musl-x64"),
            ("musl", 2),
        )

    def test_find_asset_download_url_falls_back_to_zipball_without_assets(self):
        release_info = {
            "tag_name": "v1.0.0",
            "zipball_url": "zipball",
            "tarball_url": "tarball",
        }

        self.assertEqual(
            self.downloader.find_asset_download_url(release_info), ("zipball", None)
        )

    def test_handle_rate_limits_uses_retry_after_header(self):
        with patch.object(download.time, "sleep") as sleep:
            handled = self.downloader.handle_rate_limits(
                FakeResponse(429, {"Retry-After": "12"})
            )

        self.assertTrue(handled)
        sleep.assert_called_once_with(12)

    def test_download_and_extract_skips_zip_members_outside_target_dir(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("app/good.txt", "ok")
            archive.writestr("app/../../escape.txt", "bad")
        response = FakeResponse(
            200,
            {"Content-Disposition": "attachment; filename=app.zip"},
            zip_buffer.getvalue(),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target"
            escaped = Path(temp_dir) / "escape.txt"
            with patch.object(
                self.downloader, "fetch_with_retries", return_value=response
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.zip", str(target), zip_folder_name="app"
                )

            self.assertTrue(success, error)
            self.assertEqual((target / "good.txt").read_text(), "ok")
            self.assertFalse(escaped.exists())

    def test_download_and_extract_skips_tar_members_outside_target_dir(self):
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w") as archive:
            good_data = b"ok"
            good = tarfile.TarInfo("app/good.txt")
            good.size = len(good_data)
            archive.addfile(good, io.BytesIO(good_data))
            bad_data = b"bad"
            bad = tarfile.TarInfo("../escape.txt")
            bad.size = len(bad_data)
            archive.addfile(bad, io.BytesIO(bad_data))
        response = FakeResponse(
            200,
            {"Content-Disposition": "attachment; filename=app.tar"},
            tar_buffer.getvalue(),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target"
            escaped = Path(temp_dir) / "escape.txt"
            with patch.object(
                self.downloader, "fetch_with_retries", return_value=response
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.tar", str(target)
                )

            self.assertTrue(success, error)
            self.assertEqual((target / "app" / "good.txt").read_text(), "ok")
            self.assertFalse(escaped.exists())

    def test_handle_rate_limits_ignores_non_rate_limit_statuses(self):
        with patch.object(download.time, "sleep") as sleep:
            handled = self.downloader.handle_rate_limits(FakeResponse(500))

        self.assertFalse(handled)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
