import io
import hashlib
import os
import stat
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

    def find_key_for_process(self, _process_name):
        return None, None

    def get_instance(self, _instance_name, _key):
        return None


class FakeResponse:
    def __init__(self, status_code, headers=None, content=b"", json_data=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self.json_data = json_data or {}

    def json(self):
        return self.json_data


class _InstallCache:
    def lookup_download(self, _url):
        return None, {}

    def store_download(self, _url, _content, **_metadata):
        return {}

    def invalidate_download(self, _url, _reason):
        return None


def _install_runtime_stubs():
    global_logger = types.ModuleType("utils.global_logger")
    global_logger.logger = _Logger()
    sys.modules["utils.global_logger"] = global_logger

    config_loader = types.ModuleType("utils.config_loader")
    config_loader.CONFIG_MANAGER = _ConfigManager()
    sys.modules["utils.config_loader"] = config_loader

    requests_stub = types.ModuleType("requests")
    requests_stub.RequestException = Exception
    requests_stub.Session = object
    requests_stub.Response = object
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
        download.INSTALL_CACHE = _InstallCache()
        self.downloader = download.Downloader()

    def test_archive_root_filter_accepts_only_explicit_aliases(self):
        accepted = [
            "infinidysk-dev-linux-x64",
            "infinidysk-*-linux-x64",
            "nzbdav-*-linux-x64",
        ]

        self.assertEqual(
            "app/NzbWebDAV",
            self.downloader._strict_relative_path(
                "infinidysk-vdev-linux-x64/app/NzbWebDAV", accepted
            ),
        )
        self.assertIsNone(
            self.downloader._strict_relative_path(
                "infinidysk-other-linux-arm64/app/NzbWebDAV", accepted
            )
        )

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

    def test_prerelease_discovery_uses_github_prerelease_flag(self):
        response = FakeResponse(
            200,
            json_data=[
                {
                    "id": 30,
                    "tag_name": "v0.10.0-rc.3",
                    "draft": False,
                    "prerelease": True,
                    "published_at": "2026-08-05T13:22:05Z",
                },
                {
                    "id": 29,
                    "tag_name": "v0.9.5",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-08-05T13:23:05Z",
                },
            ],
        )

        with patch.object(self.downloader, "fetch_with_retries", return_value=response):
            version, error = self.downloader.get_latest_release(
                "infinidysk", "infinidysk", prerelease=True
            )

        self.assertIsNone(error)
        self.assertEqual("v0.10.0-rc.3", version)

    def test_prerelease_discovery_uses_publish_recency_not_tag_sorting(self):
        response = FakeResponse(
            200,
            json_data=[
                {
                    "id": 10,
                    "tag_name": "v1.0.0-rc.10",
                    "draft": False,
                    "prerelease": True,
                    "published_at": "2026-08-05T12:00:00Z",
                },
                {
                    "id": 9,
                    "tag_name": "v1.0.0-rc.9",
                    "draft": False,
                    "prerelease": True,
                    "published_at": "2026-08-04T12:00:00Z",
                },
            ],
        )

        with patch.object(self.downloader, "fetch_with_retries", return_value=response):
            version, error = self.downloader.get_latest_release(
                "owner", "repo", prerelease=True
            )

        self.assertIsNone(error)
        self.assertEqual("v1.0.0-rc.10", version)

    def test_count_releases_behind_uses_stable_release_history(self):
        response = FakeResponse(
            200,
            json_data=[
                {"tag_name": "v2.3.0", "draft": False, "prerelease": False},
                {"tag_name": "v2.3.0-rc.1", "draft": False, "prerelease": True},
                {"tag_name": "v2.2.0", "draft": False, "prerelease": False},
                {"tag_name": "v2.1.0", "draft": False, "prerelease": False},
                {"tag_name": "v2.0.0", "draft": False, "prerelease": False},
            ],
        )

        with patch.object(self.downloader, "fetch_with_retries", return_value=response):
            count, error = self.downloader.count_releases_behind(
                "owner", "repo", "2.0.0", "v2.3.0"
            )

        self.assertIsNone(error)
        self.assertEqual(3, count)

    def test_count_releases_behind_returns_unknown_for_unlisted_build(self):
        response = FakeResponse(
            200,
            json_data=[
                {"tag_name": "v2.3.0", "draft": False, "prerelease": False},
                {"tag_name": "v2.2.0", "draft": False, "prerelease": False},
            ],
        )

        with patch.object(self.downloader, "fetch_with_retries", return_value=response):
            count, error = self.downloader.count_releases_behind(
                "owner", "repo", "dev-abcdef12", "v2.3.0"
            )

        self.assertIsNone(count)
        self.assertIn("not found", error)

    def test_normalize_arch_maps_common_architectures(self):
        self.assertEqual(download.Downloader.normalize_arch("linux-x64"), "linux_x64")
        self.assertEqual(
            download.Downloader.normalize_arch("linux-arm64"), "linux_arm64"
        )
        self.assertEqual(download.Downloader.normalize_arch("linux-arm"), "linux_arm")
        self.assertEqual(download.Downloader.normalize_arch("amd64"), "amd64")

    def test_get_commit_uses_immutable_github_archive(self):
        commit_sha = "a" * 40
        response = FakeResponse(200)

        with patch.object(
            self.downloader, "fetch_with_retries", return_value=response
        ) as fetch:
            url, folder = self.downloader.get_commit("owner", "repo", commit_sha)

        self.assertEqual(url, f"https://github.com/owner/repo/archive/{commit_sha}.zip")
        self.assertEqual(folder, f"repo-{commit_sha}")
        fetch.assert_called_once_with(url, self.downloader.get_headers())

    def test_get_commit_rejects_short_or_non_hex_sha_without_network(self):
        with patch.object(self.downloader, "fetch_with_retries") as fetch:
            for value in ("abc1234", "g" * 40, ""):
                with self.subTest(value=value):
                    url, error = self.downloader.get_commit("owner", "repo", value)
                    self.assertIsNone(url)
                    self.assertIn("40-character hexadecimal", error)

        fetch.assert_not_called()

    def test_get_ref_commit_sha_resolves_encoded_tag_to_commit(self):
        commit_sha = "a" * 40
        response = FakeResponse(200, json_data={"sha": commit_sha.upper()})

        with patch.object(
            self.downloader, "fetch_with_retries", return_value=response
        ) as fetch:
            resolved, error = self.downloader.get_ref_commit_sha(
                "owner", "repo", "release/dev"
            )

        self.assertIsNone(error)
        self.assertEqual(commit_sha, resolved)
        fetch.assert_called_once_with(
            "https://api.github.com/repos/owner/repo/commits/release%2Fdev",
            self.downloader.get_headers(),
        )

    def test_get_ref_commit_sha_rejects_invalid_response_sha(self):
        response = FakeResponse(200, json_data={"sha": "short"})

        with patch.object(self.downloader, "fetch_with_retries", return_value=response):
            resolved, error = self.downloader.get_ref_commit_sha("owner", "repo", "dev")

        self.assertIsNone(resolved)
        self.assertIn("Unable to resolve GitHub ref commit SHA", error)

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

    def test_bazarr_release_extracts_flat_asset_without_wrapper_directory(self):
        release_info = {
            "tag_name": "v1.6.0",
            "assets": [
                {
                    "id": 42,
                    "name": "bazarr.zip",
                    "browser_download_url": "https://example.test/bazarr.zip",
                }
            ],
        }
        with (
            patch.object(
                self.downloader,
                "fetch_github_release_info",
                return_value=(release_info, None),
            ),
            patch.object(
                self.downloader,
                "download_and_extract",
                return_value=(True, None),
            ) as extract,
        ):
            success, error = self.downloader.download_release_version(
                process_name="Bazarr",
                key="bazarr",
                repo_owner="morpheus65535",
                repo_name="bazarr",
                release_version="v1.6.0",
                target_dir="/opt/bazarr",
            )

        self.assertTrue(success, error)
        self.assertIsNone(extract.call_args.args[2])

    def test_emby_release_selects_debian_asset_not_other_arm64_package(self):
        release_info = {
            "tag_name": "4.10.0.21",
            "assets": [
                {
                    "id": 1,
                    "name": "emby-server-asustor_4.10.0.21_arm64.apk",
                    "browser_download_url": "https://example.test/emby.apk",
                },
                {
                    "id": 2,
                    "name": "emby-server-deb_4.10.0.21_arm64.deb",
                    "browser_download_url": "https://example.test/emby.deb",
                },
            ],
        }
        with (
            patch.object(download.platform, "machine", return_value="aarch64"),
            patch.object(
                self.downloader,
                "fetch_github_release_info",
                return_value=(release_info, None),
            ),
            patch.object(
                self.downloader,
                "download_and_extract",
                return_value=(True, None),
            ) as extract,
        ):
            success, error = self.downloader.download_release_version(
                process_name="Emby Media Server",
                key="emby",
                repo_owner="MediaBrowser",
                repo_name="Emby.Releases",
                release_version="4.10.0.21",
                target_dir="/tmp/emby",
            )

        self.assertTrue(success, error)
        self.assertEqual(
            "https://api.github.com/repos/MediaBrowser/Emby.Releases/releases/assets/2",
            extract.call_args.args[0],
        )

    def test_nzbdav_tag_only_version_uses_source_zipball(self):
        with (
            patch.object(
                self.downloader,
                "fetch_github_release_info",
            ) as fetch_release,
            patch.object(
                self.downloader,
                "download_and_extract",
                return_value=(True, None),
            ) as extract,
        ):
            success, error = self.downloader.download_release_version(
                process_name="InfiniDysk",
                key="infinidysk",
                repo_owner="infinidysk",
                repo_name="infinidysk",
                release_version="dev",
                target_dir="/infinidysk",
            )

        self.assertTrue(success, error)
        fetch_release.assert_not_called()
        self.assertEqual(
            extract.call_args.args[:3],
            (
                "https://api.github.com/repos/infinidysk/infinidysk/zipball/dev",
                "/infinidysk",
                "infinidysk-infinidysk*",
            ),
        )

    def test_nzbdav_source_zipball_encodes_tag_ref(self):
        with patch.object(
            self.downloader,
            "download_and_extract",
            return_value=(True, None),
        ) as extract:
            success, error = self.downloader.download_release_version(
                process_name="InfiniDysk",
                key="infinidysk",
                repo_owner="infinidysk",
                repo_name="infinidysk",
                release_version="preview/test",
                target_dir="/infinidysk",
            )

        self.assertTrue(success, error)
        self.assertEqual(
            extract.call_args.args[0],
            "https://api.github.com/repos/infinidysk/infinidysk/zipball/preview%2Ftest",
        )

    def test_handle_rate_limits_uses_retry_after_header(self):
        with patch.object(download.time, "sleep") as sleep:
            handled = self.downloader.handle_rate_limits(
                FakeResponse(429, {"Retry-After": "12"})
            )

        self.assertTrue(handled)
        sleep.assert_called_once_with(12)

    def test_handle_rate_limits_refuses_wait_beyond_configured_maximum(self):
        download.CONFIG_MANAGER.values = {
            "dumb": {"github_rate_limit_max_wait_seconds": 10}
        }
        with patch.object(download.time, "sleep") as sleep:
            handled = self.downloader.handle_rate_limits(
                FakeResponse(403, {"Retry-After": "120"})
            )

        self.assertFalse(handled)
        sleep.assert_not_called()

    def test_fetch_stops_after_refused_rate_limit_wait(self):
        response = FakeResponse(403, {"Retry-After": "120"})
        with (
            patch.object(download.requests, "get", return_value=response) as get,
            patch.object(self.downloader, "handle_rate_limits", return_value=False),
        ):
            result = self.downloader.fetch_with_retries(
                "https://api.github.com/example", {}, max_retries=5
            )

        self.assertIsNone(result)
        get.assert_called_once()

    def test_download_and_extract_rejects_zip_members_outside_target_dir(self):
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

            self.assertFalse(success)
            self.assertIn("Unsafe archive member path", error)
            self.assertFalse((target / "good.txt").exists())
            self.assertFalse(escaped.exists())

    def test_download_and_extract_allows_internal_zip_file_symlink(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("app/init.freebsd", "#!/bin/sh\n")
            symlink = zipfile.ZipInfo("app/init.freenas")
            symlink.create_system = 3
            symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(symlink, "init.freebsd")
        response = FakeResponse(
            200,
            {"Content-Disposition": "attachment; filename=app.zip"},
            zip_buffer.getvalue(),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target"
            with patch.object(
                self.downloader, "fetch_with_retries", return_value=response
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.zip", str(target), zip_folder_name="app"
                )

            self.assertTrue(success, error)
            self.assertTrue((target / "init.freenas").is_symlink())
            self.assertEqual("init.freebsd", os.readlink(target / "init.freenas"))

    def test_download_and_extract_rejects_escaping_zip_symlink(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("app/good.txt", "ok")
            symlink = zipfile.ZipInfo("app/escape")
            symlink.create_system = 3
            symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(symlink, "../../outside")
        response = FakeResponse(
            200,
            {"Content-Disposition": "attachment; filename=app.zip"},
            zip_buffer.getvalue(),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target"
            with patch.object(
                self.downloader, "fetch_with_retries", return_value=response
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.zip", str(target), zip_folder_name="app"
                )

            self.assertFalse(success)
            self.assertIn("internal regular file", error)
            self.assertFalse(target.exists())

    def test_download_and_extract_rejects_tar_members_outside_target_dir(self):
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

            self.assertFalse(success)
            self.assertIn("Unsafe archive member path", error)
            self.assertFalse((target / "app" / "good.txt").exists())
            self.assertFalse(escaped.exists())

    def test_download_and_extract_allows_internal_tar_file_symlink(self):
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w") as archive:
            executable = tarfile.TarInfo("app/bin/tool.js")
            executable.mode = 0o755
            executable_data = b"#!/usr/bin/env node\n"
            executable.size = len(executable_data)
            archive.addfile(executable, io.BytesIO(executable_data))
            symlink = tarfile.TarInfo("app/node_modules/.bin/tool")
            symlink.type = tarfile.SYMTYPE
            symlink.linkname = "../../bin/tool.js"
            archive.addfile(symlink)
        response = FakeResponse(
            200,
            {"Content-Disposition": "attachment; filename=app.tar"},
            tar_buffer.getvalue(),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target"
            with patch.object(
                self.downloader, "fetch_with_retries", return_value=response
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.tar",
                    str(target),
                    zip_folder_name="app",
                )

            self.assertTrue(success, error)
            self.assertTrue((target / "node_modules/.bin/tool").is_symlink())
            self.assertEqual(
                "../../bin/tool.js", os.readlink(target / "node_modules/.bin/tool")
            )
            self.assertTrue((target / "bin/tool.js").stat().st_mode & stat.S_IXUSR)

    def test_download_and_extract_rejects_escaping_tar_symlink(self):
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w") as archive:
            executable = tarfile.TarInfo("app/bin/tool.js")
            executable_data = b"tool"
            executable.size = len(executable_data)
            archive.addfile(executable, io.BytesIO(executable_data))
            symlink = tarfile.TarInfo("app/escape")
            symlink.type = tarfile.SYMTYPE
            symlink.linkname = "../../outside"
            archive.addfile(symlink)
        response = FakeResponse(
            200,
            {"Content-Disposition": "attachment; filename=app.tar"},
            tar_buffer.getvalue(),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target"
            with patch.object(
                self.downloader, "fetch_with_retries", return_value=response
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.tar",
                    str(target),
                    zip_folder_name="app",
                )

            self.assertFalse(success)
            self.assertIn("internal regular file", error)
            self.assertFalse(target.exists())

    def test_download_and_extract_copies_internal_tar_hardlink(self):
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w") as archive:
            executable = tarfile.TarInfo("app/store/tool")
            executable.mode = 0o755
            executable_data = b"binary"
            executable.size = len(executable_data)
            archive.addfile(executable, io.BytesIO(executable_data))
            hardlink = tarfile.TarInfo("app/bin/tool")
            hardlink.type = tarfile.LNKTYPE
            hardlink.linkname = "app/store/tool"
            archive.addfile(hardlink)
        response = FakeResponse(
            200,
            {"Content-Disposition": "attachment; filename=app.tar"},
            tar_buffer.getvalue(),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target"
            with patch.object(
                self.downloader, "fetch_with_retries", return_value=response
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.tar",
                    str(target),
                    zip_folder_name="app",
                )

            self.assertTrue(success, error)
            self.assertEqual(b"binary", (target / "bin/tool").read_bytes())
            self.assertTrue((target / "bin/tool").stat().st_mode & stat.S_IXUSR)

    def test_download_and_extract_rejects_external_tar_hardlink(self):
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w") as archive:
            hardlink = tarfile.TarInfo("app/bin/tool")
            hardlink.type = tarfile.LNKTYPE
            hardlink.linkname = "../../outside"
            archive.addfile(hardlink)
        response = FakeResponse(
            200,
            {"Content-Disposition": "attachment; filename=app.tar"},
            tar_buffer.getvalue(),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target"
            with patch.object(
                self.downloader, "fetch_with_retries", return_value=response
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.tar",
                    str(target),
                    zip_folder_name="app",
                )

            self.assertFalse(success)
            self.assertIn("hard link", error)
            self.assertFalse(target.exists())

    def test_invalid_archive_does_not_replace_existing_runtime(self):
        response = FakeResponse(
            200,
            {"Content-Disposition": "attachment; filename=app.zip"},
            b"not-a-zip",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target"
            target.mkdir()
            existing = target / "runtime.txt"
            existing.write_text("working")
            with patch.object(
                self.downloader, "fetch_with_retries", return_value=response
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.zip", str(target)
                )

            self.assertFalse(success)
            self.assertIn("Invalid ZIP archive", error)
            self.assertEqual(existing.read_text(), "working")

    def test_download_and_extract_accepts_published_sha256(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("app/runtime.txt", "verified")
        content = zip_buffer.getvalue()
        response = FakeResponse(
            200,
            {"Content-Disposition": "attachment; filename=app.zip"},
            content,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target"
            with patch.object(
                self.downloader, "fetch_with_retries", return_value=response
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.zip",
                    str(target),
                    zip_folder_name="app",
                    expected_sha256=hashlib.sha256(content).hexdigest(),
                )

            self.assertTrue(success, error)
            self.assertEqual("verified", (target / "runtime.txt").read_text())

    def test_download_and_extract_rejects_published_sha256_mismatch(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("app/runtime.txt", "tampered")
        response = FakeResponse(
            200,
            {"Content-Disposition": "attachment; filename=app.zip"},
            zip_buffer.getvalue(),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target"
            with patch.object(
                self.downloader, "fetch_with_retries", return_value=response
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.zip",
                    str(target),
                    zip_folder_name="app",
                    expected_sha256="0" * 64,
                )

            self.assertFalse(success)
            self.assertIn("published SHA-256", error)
            self.assertFalse(target.exists())

    def test_github_zipball_wildcard_extracts_wrapped_source(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr(
                "Maintainerr-Maintainerr-abc123/package.json",
                '{"name":"maintainerr"}',
            )
            archive.writestr(
                "Maintainerr-Maintainerr-abc123/apps/api/main.ts",
                "export {};",
            )
        response = FakeResponse(
            200,
            {"Content-Disposition": "attachment; filename=maintainerr.zip"},
            zip_buffer.getvalue(),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "maintainerr"
            with patch.object(
                self.downloader, "fetch_with_retries", return_value=response
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/maintainerr.zip",
                    str(target),
                    zip_folder_name="Maintainerr-Maintainerr*",
                )

            self.assertTrue(success, error)
            self.assertTrue((target / "package.json").is_file())
            self.assertTrue((target / "apps" / "api" / "main.ts").is_file())

    def test_archive_with_no_matching_files_is_rejected_without_live_changes(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("unexpected-root/package.json", "{}")
            archive.writestr("unexpected-root/runtime.js", "export {};")
        response = FakeResponse(
            200,
            {"Content-Disposition": "attachment; filename=app.zip"},
            zip_buffer.getvalue(),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target"
            target.mkdir()
            existing = target / "runtime.txt"
            existing.write_text("working", encoding="utf-8")
            with patch.object(
                self.downloader, "fetch_with_retries", return_value=response
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.zip",
                    str(target),
                    zip_folder_name="expected-root*",
                )

            self.assertFalse(success)
            self.assertIn("no eligible files", error)
            self.assertEqual(existing.read_text(encoding="utf-8"), "working")

    def test_symlinked_install_stages_and_backs_up_on_resolved_filesystem(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("owner-repo-abc/runtime.txt", "updated")
            archive.writestr("owner-repo-abc/package.json", "{}")
        response = FakeResponse(
            200,
            {"Content-Disposition": "attachment; filename=app.zip"},
            zip_buffer.getvalue(),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_parent = root / "data"
            resolved_target = data_parent / "service"
            resolved_target.mkdir(parents=True)
            (resolved_target / "runtime.txt").write_text("working", encoding="utf-8")
            target_link = root / "service"
            target_link.symlink_to(resolved_target, target_is_directory=True)
            real_mkdtemp = tempfile.mkdtemp
            transaction_parents = []

            def tracked_mkdtemp(*args, **kwargs):
                transaction_parents.append(Path(kwargs["dir"]).resolve())
                return real_mkdtemp(*args, **kwargs)

            with (
                patch.object(
                    self.downloader, "fetch_with_retries", return_value=response
                ),
                patch.object(download.tempfile, "mkdtemp", side_effect=tracked_mkdtemp),
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.zip",
                    str(target_link),
                    zip_folder_name="owner-repo*",
                )

            self.assertTrue(success, error)
            self.assertTrue(target_link.is_symlink())
            self.assertEqual(
                (resolved_target / "runtime.txt").read_text(encoding="utf-8"),
                "updated",
            )
            self.assertGreaterEqual(len(transaction_parents), 2)
            self.assertEqual(
                set(transaction_parents),
                {data_parent.resolve()},
            )

    def test_download_uses_verified_cached_archive_when_revalidation_is_offline(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("app/runtime.txt", "cached")
        metadata = {
            "filename": "app.zip",
            "content_type": "application/zip",
            "etag": '"v1"',
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target"
            with (
                patch.object(
                    download.INSTALL_CACHE,
                    "lookup_download",
                    return_value=(zip_buffer.getvalue(), metadata),
                ),
                patch.object(self.downloader, "fetch_with_retries", return_value=None),
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.zip",
                    str(target),
                    zip_folder_name="app",
                )

            self.assertTrue(success, error)
            self.assertEqual((target / "runtime.txt").read_text(), "cached")

    def test_handle_rate_limits_ignores_non_rate_limit_statuses(self):
        with patch.object(download.time, "sleep") as sleep:
            handled = self.downloader.handle_rate_limits(FakeResponse(500))

        self.assertFalse(handled)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
