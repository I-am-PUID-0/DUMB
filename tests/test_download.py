import io
import hashlib
import os
import errno
import shutil
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
from utils import install_cache as install_cache_module
from utils.install_cache import InstallCache


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
                    "digest": "sha256:" + "b" * 64,
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
        self.assertEqual(
            "sha256:" + "b" * 64, extract.call_args.kwargs["expected_sha256"]
        )

    @staticmethod
    def _bazarr_release_info(digest):
        return {
            "tag_name": "v1.0.0",
            "assets": [
                {
                    "id": 42,
                    "name": "bazarr.zip",
                    "browser_download_url": "https://example.test/bazarr.zip",
                    "digest": digest,
                }
            ],
        }

    def _download_bazarr_release(self, target, **kwargs):
        return self.downloader.download_release_version(
            process_name="Bazarr",
            key="bazarr",
            repo_owner="owner",
            repo_name="repo",
            release_version="v1.0.0",
            target_dir=str(target),
            **kwargs,
        )

    @staticmethod
    def _flat_zip_content(runtime="verified"):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("runtime.txt", runtime)
            archive.writestr("config.json", "candidate config")
        return buffer.getvalue()

    def test_release_asset_digest_is_propagated_for_bazarr(self):
        digest = "sha256:" + "A" * 64
        with (
            patch.object(
                self.downloader,
                "fetch_github_release_info",
                return_value=(self._bazarr_release_info(digest), None),
            ),
            patch.object(
                self.downloader, "download_and_extract", return_value=(True, None)
            ) as extract,
        ):
            success, error = self._download_bazarr_release("/unused/bazarr")

        self.assertTrue(success, error)
        self.assertEqual(digest.lower(), extract.call_args.kwargs["expected_sha256"])

    def test_release_uses_digest_from_selected_architecture_asset(self):
        digest = "sha256:" + "a" * 64
        release_info = {
            "assets": [
                {
                    "id": 1,
                    "name": "decypharr-linux-arm64",
                    "browser_download_url": "https://example.test/arm64",
                    "digest": "unsupported-unselected-metadata",
                },
                {
                    "id": 2,
                    "name": "decypharr-linux-amd64",
                    "browser_download_url": "https://example.test/amd64",
                    "digest": digest,
                },
            ],
        }
        with (
            patch.object(
                self.downloader, "get_architecture", return_value="linux-amd64"
            ),
            patch.object(
                self.downloader,
                "fetch_github_release_info",
                return_value=(release_info, None),
            ),
            patch.object(
                self.downloader, "download_and_extract", return_value=(True, None)
            ) as extract,
        ):
            success, error = self.downloader.download_release_version(
                "Decypharr", "decypharr", "owner", "repo", "v1.0.0", "/unused"
            )

        self.assertTrue(success, error)
        self.assertTrue(extract.call_args.args[0].endswith("/assets/2"))
        self.assertEqual(digest, extract.call_args.kwargs["expected_sha256"])

    def test_release_rejects_invalid_or_unsupported_asset_digest_before_download(self):
        for digest in (
            "",
            " ",
            "a" * 64,
            "sha256:short",
            "sha256:" + "g" * 64,
            "sha512:" + "a" * 128,
            123,
            False,
            [],
            {},
        ):
            with self.subTest(digest=digest), tempfile.TemporaryDirectory() as temp_dir:
                target = Path(temp_dir, "service")
                target.mkdir()
                runtime = target / "runtime.txt"
                runtime.write_text("working", encoding="utf-8")
                with (
                    patch.object(
                        self.downloader,
                        "fetch_github_release_info",
                        return_value=(self._bazarr_release_info(digest), None),
                    ),
                    patch.object(self.downloader, "download_and_extract") as extract,
                ):
                    success, error = self._download_bazarr_release(target)

                self.assertFalse(success)
                self.assertIn("invalid or unsupported", error)
                self.assertIn("upstream release metadata", error)
                extract.assert_not_called()
                self.assertEqual("working", runtime.read_text(encoding="utf-8"))

    def test_release_missing_asset_digest_logs_unsigned_fallback(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                release_info = self._bazarr_release_info(None)
                if missing:
                    release_info["assets"][0].pop("digest")
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
                    patch.object(self.downloader.logger, "warning") as warning,
                ):
                    success, error = self._download_bazarr_release("/unused")

                self.assertTrue(success, error)
                self.assertIsNone(extract.call_args.kwargs["expected_sha256"])
                self.assertIn(
                    "No published GitHub asset checksum", warning.call_args.args[0]
                )
                self.assertIn(
                    "without publisher-signature verification",
                    warning.call_args.args[0],
                )

    def test_release_digest_verifies_fresh_and_cached_downloads(self):
        content = self._flat_zip_content()
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        url = "https://api.github.com/repos/owner/repo/releases/assets/42"
        response = FakeResponse(
            200,
            {"Content-Disposition": "attachment; filename=bazarr.zip", "ETag": '"v1"'},
            content,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = InstallCache(Path(temp_dir, "cache"))
            with (
                patch.object(download, "INSTALL_CACHE", cache),
                patch.object(
                    install_cache_module, "install_cache_enabled", return_value=True
                ),
                patch.object(
                    self.downloader,
                    "fetch_github_release_info",
                    return_value=(self._bazarr_release_info(digest), None),
                ),
                patch.object(
                    self.downloader,
                    "fetch_with_retries",
                    side_effect=[response, FakeResponse(304), None],
                ) as fetch,
                patch.object(
                    cache, "store_download", wraps=cache.store_download
                ) as store,
            ):
                for attempt in range(3):
                    target = Path(temp_dir, f"service-{attempt}")
                    success, error = self._download_bazarr_release(target)
                    self.assertTrue(success, error)
                    self.assertEqual("verified", (target / "runtime.txt").read_text())
                cached, metadata = cache.lookup_download(url)

            self.assertEqual(content, cached)
            self.assertEqual(digest.removeprefix("sha256:"), metadata["sha256"])
            self.assertEqual(3, fetch.call_count)
            store.assert_called_once()
            self.assertEqual('"v1"', fetch.call_args_list[1].args[1]["If-None-Match"])

    def test_release_digest_mismatch_never_retries_without_verification(self):
        content = self._flat_zip_content("unverified")
        for status in (200, 304, None):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp_dir:
                cache = InstallCache(Path(temp_dir, "cache"))
                target = Path(temp_dir, "service")
                target.mkdir()
                runtime = target / "runtime.txt"
                runtime.write_text("working", encoding="utf-8")
                config = target / "config.json"
                config.write_text("persistent config", encoding="utf-8")
                response = (
                    None
                    if status is None
                    else FakeResponse(
                        status,
                        {"Content-Disposition": "attachment; filename=bazarr.zip"},
                        content,
                    )
                )
                url = "https://api.github.com/repos/owner/repo/releases/assets/42"
                with (
                    patch.object(download, "INSTALL_CACHE", cache),
                    patch.object(
                        install_cache_module, "install_cache_enabled", return_value=True
                    ),
                    patch.object(
                        self.downloader,
                        "fetch_github_release_info",
                        return_value=(
                            self._bazarr_release_info("sha256:" + "0" * 64),
                            None,
                        ),
                    ),
                    patch.object(
                        self.downloader, "fetch_with_retries", return_value=response
                    ) as fetch,
                ):
                    if status != 200:
                        # The cache hashes are self-consistent but disagree with
                        # the independent published digest.
                        cache.store_download(url, content, filename="bazarr.zip")
                    with patch.object(
                        cache, "store_download", wraps=cache.store_download
                    ) as store:
                        success, error = self._download_bazarr_release(target)
                    cached, _ = cache.lookup_download(url)

                self.assertFalse(success)
                self.assertIn("published SHA-256", error)
                self.assertEqual("working", runtime.read_text())
                self.assertEqual("persistent config", config.read_text())
                self.assertEqual(
                    ["config.json", "runtime.txt"],
                    sorted(p.name for p in target.iterdir()),
                )
                self.assertIsNone(cached)
                fetch.assert_called_once()
                store.assert_not_called()
                self.assertFalse(any(Path(temp_dir).glob(".service.dumb-*")))

    def test_corrupt_release_cache_redownload_still_requires_published_digest(self):
        content = self._flat_zip_content()
        expected_digest = hashlib.sha256(content).hexdigest()
        for valid_replacement in (True, False):
            with (
                self.subTest(valid_replacement=valid_replacement),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                cache = InstallCache(Path(temp_dir, "cache"))
                url = "https://api.github.com/repos/owner/repo/releases/assets/42"
                target = Path(temp_dir, "service")
                target.mkdir()
                (target / "runtime.txt").write_text("working")
                replacement = (
                    content if valid_replacement else self._flat_zip_content("tampered")
                )
                response = FakeResponse(
                    200,
                    {"Content-Disposition": "attachment; filename=bazarr.zip"},
                    replacement,
                )
                with (
                    patch.object(download, "INSTALL_CACHE", cache),
                    patch.object(
                        install_cache_module, "install_cache_enabled", return_value=True
                    ),
                    patch.object(
                        self.downloader,
                        "fetch_github_release_info",
                        return_value=(
                            self._bazarr_release_info("sha256:" + expected_digest),
                            None,
                        ),
                    ),
                    patch.object(
                        self.downloader, "fetch_with_retries", return_value=response
                    ) as fetch,
                ):
                    metadata = cache.store_download(url, content, filename="bazarr.zip")
                    cache._object_path(metadata["sha256"]).write_bytes(b"corrupt")
                    success, error = self._download_bazarr_release(target)

                self.assertEqual(valid_replacement, success, error)
                self.assertEqual(
                    "verified" if valid_replacement else "working",
                    (target / "runtime.txt").read_text(),
                )
                fetch.assert_called_once()
                self.assertTrue(list(cache.quarantine.iterdir()))

    def test_verified_release_candidate_rejection_and_retry_preserve_persistent_data(
        self,
    ):
        content = self._flat_zip_content("candidate")
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        response = FakeResponse(
            200,
            {"Content-Disposition": "attachment; filename=bazarr.zip"},
            content,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = InstallCache(Path(temp_dir, "cache"))
            target = Path(temp_dir, "service")
            target.mkdir()
            (target / "runtime.txt").write_text("working")
            (target / "config.json").write_text("persistent config")
            staged_paths = []

            def reject_candidate(staging_dir):
                staged_paths.append(Path(staging_dir))
                self.assertEqual(
                    "candidate", (Path(staging_dir) / "runtime.txt").read_text()
                )
                return False, "candidate is incompatible"

            with (
                patch.object(download, "INSTALL_CACHE", cache),
                patch.object(
                    install_cache_module, "install_cache_enabled", return_value=True
                ),
                patch.object(
                    self.downloader,
                    "fetch_github_release_info",
                    return_value=(self._bazarr_release_info(digest), None),
                ),
                patch.object(
                    self.downloader,
                    "fetch_with_retries",
                    side_effect=[response, FakeResponse(304)],
                ) as fetch,
            ):
                success, error = self._download_bazarr_release(
                    target,
                    exclude_dirs=["config.json"],
                    staging_validator=reject_candidate,
                )
                self.assertFalse(success)
                self.assertIn("candidate is incompatible", error)
                self.assertEqual("working", (target / "runtime.txt").read_text())
                self.assertEqual(
                    "persistent config", (target / "config.json").read_text()
                )
                self.assertFalse(staged_paths[0].exists())
                success, error = self._download_bazarr_release(
                    target, exclude_dirs=["config.json"]
                )

            self.assertTrue(success, error)
            self.assertEqual("candidate", (target / "runtime.txt").read_text())
            self.assertEqual("persistent config", (target / "config.json").read_text())
            self.assertEqual(2, fetch.call_count)
            self.assertFalse(any(Path(temp_dir).glob(".service.dumb-*")))

    def test_invalid_expected_digest_is_rejected_before_cache_or_network(self):
        for digest in ("", "sha512:" + "a" * 128, "sha256:invalid", 123, False):
            with (
                self.subTest(digest=digest),
                patch.object(download.INSTALL_CACHE, "lookup_download") as cache_lookup,
                patch.object(self.downloader, "fetch_with_retries") as fetch,
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.zip", "/unused", expected_sha256=digest
                )
                self.assertFalse(success)
                self.assertIn("SHA-256 digest is invalid", error)
                cache_lookup.assert_not_called()
                fetch.assert_not_called()

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

    def test_staging_validator_rejection_leaves_live_target_unchanged(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("owner-repo-abc/runtime.txt", "candidate")
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
            validated_paths = []

            def reject_candidate(staging_dir):
                staging = Path(staging_dir)
                validated_paths.append(staging)
                self.assertEqual(
                    "candidate",
                    (staging / "runtime.txt").read_text(encoding="utf-8"),
                )
                return False, "source import mismatch"

            with patch.object(
                self.downloader, "fetch_with_retries", return_value=response
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.zip",
                    str(target),
                    zip_folder_name="owner-repo*",
                    staging_validator=reject_candidate,
                )

            self.assertFalse(success)
            self.assertIn("source import mismatch", error)
            self.assertEqual("working", existing.read_text(encoding="utf-8"))
            self.assertEqual(1, len(validated_paths))
            self.assertFalse(validated_paths[0].exists())

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
            transaction_parents = []
            original_workspace = download._ArchiveWorkspace

            def tracked_workspace(target_dir, logger):
                workspace = original_workspace(target_dir, logger)
                transaction_parents.append(
                    Path(workspace.stage).resolve().parent.parent
                )
                transaction_parents.append(
                    Path(workspace.backup).resolve().parent.parent
                )
                return workspace

            with (
                patch.object(
                    self.downloader, "fetch_with_retries", return_value=response
                ),
                patch.object(
                    download, "_ArchiveWorkspace", side_effect=tracked_workspace
                ),
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.zip",
                    str(target_link),
                    zip_folder_name="owner-repo*",
                )

            self.assertTrue(success, error)
            self.assertTrue(target_link.is_symlink())
            self.assertEqual("updated", (resolved_target / "runtime.txt").read_text())
            self.assertEqual([resolved_target.resolve()] * 2, transaction_parents)
            self.assertFalse(list(resolved_target.glob(".dumb-overlay-*")))

    @staticmethod
    def _overlay_response(entries):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
        return FakeResponse(
            200,
            {"Content-Disposition": "attachment; filename=app.zip"},
            buffer.getvalue(),
        )

    def _apply_overlay(self, target, entries, **kwargs):
        with patch.object(
            self.downloader,
            "fetch_with_retries",
            return_value=self._overlay_response(entries),
        ):
            return self.downloader.download_and_extract(
                "https://example.test/app.zip", str(target), **kwargs
            )

    def test_install_failure_after_backup_restores_the_same_entry_and_prior_changes(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "service")
            target.mkdir()
            for name in ("a.txt", "b.txt"):
                (target / name).write_text(f"old {name}")
            original_replace = os.replace

            def fail_second_install(source, destination, **kwargs):
                if "/stage/" in str(source) and destination == "b.txt":
                    raise OSError(errno.EIO, "injected install failure")
                return original_replace(source, destination, **kwargs)

            with patch.object(download.os, "replace", side_effect=fail_second_install):
                success, error = self._apply_overlay(
                    target, {"a.txt": "new a", "b.txt": "new b", "new.txt": "new"}
                )
            self.assertFalse(success)
            self.assertIn("injected install failure", error)
            self.assertEqual(
                {"a.txt": "old a.txt", "b.txt": "old b.txt"},
                {p.name: p.read_text() for p in target.iterdir()},
            )

    def test_late_exdev_failure_rolls_back_without_copy_unlink_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "service")
            target.mkdir()
            (target / "runtime.txt").write_text("original")
            original_replace = os.replace

            def fail_new_move(source, destination, **kwargs):
                if "/stage/" in str(source):
                    raise OSError(errno.EXDEV, "injected late cross-device failure")
                return original_replace(source, destination, **kwargs)

            with (
                patch.object(download.os, "replace", side_effect=fail_new_move),
                patch.object(download.shutil, "copytree") as copy_tree,
                patch.object(download.shutil, "copy2") as copy_file,
            ):
                success, error = self._apply_overlay(
                    target, {"runtime.txt": "candidate"}
                )
            self.assertFalse(success)
            self.assertIn("cross-device", error)
            self.assertEqual("original", (target / "runtime.txt").read_text())
            self.assertFalse(list(target.glob(".dumb-overlay-*")))
            copy_tree.assert_not_called()
            copy_file.assert_not_called()

    def test_external_stage_on_different_mount_is_rejected_before_backups(self):
        if not os.path.isdir("/dev/shm") or not os.access("/dev/shm", os.W_OK):
            self.skipTest("No writable separate temporary filesystem.")
        with (
            tempfile.TemporaryDirectory() as source_root,
            tempfile.TemporaryDirectory(
                prefix="dumb-overlay-test-", dir="/dev/shm"
            ) as target_root,
        ):
            if os.stat(source_root).st_dev == os.stat(target_root).st_dev:
                self.skipTest("Temporary fixtures are on the same filesystem.")
            source = Path(source_root)
            target = Path(target_root)
            (source / "runtime.txt").write_text("candidate")
            (target / "runtime.txt").write_text("original")
            with patch.object(download.os, "replace") as replace:
                success, error = self.downloader._merge_staging_transactionally(
                    str(source), str(target)
                )
            self.assertFalse(success)
            self.assertIn("staging is outside", error)
            replace.assert_not_called()
            self.assertEqual("candidate", (source / "runtime.txt").read_text())
            self.assertEqual("original", (target / "runtime.txt").read_text())
            self.assertEqual(["runtime.txt"], [p.name for p in target.iterdir()])

    def test_failed_restore_retains_original_backup_and_reports_recovery_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "service")
            target.mkdir()
            (target / "runtime.txt").write_text("old runtime")
            original_replace = os.replace

            def fail_install_and_restore(source, destination, **kwargs):
                if "/stage/" in str(source):
                    raise OSError(errno.EIO, "injected install failure")
                if "/backup/" in str(source):
                    raise OSError(errno.EACCES, "injected restore failure")
                return original_replace(source, destination, **kwargs)

            with patch.object(
                download.os, "replace", side_effect=fail_install_and_restore
            ):
                success, error = self._apply_overlay(
                    target, {"runtime.txt": "new runtime"}
                )
            self.assertFalse(success)
            self.assertIn("Rollback incomplete", error)
            workspaces = list(target.glob(".dumb-overlay-*"))
            self.assertEqual(1, len(workspaces))
            backup = workspaces[0] / "backup"
            self.assertIn(str(backup), error)
            self.assertEqual("old runtime", (backup / "runtime.txt").read_text())
            self.assertFalse((workspaces[0] / "stage").exists())

    def test_commit_cleanup_failure_does_not_roll_back_installed_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "service")
            target.mkdir()
            for name in ("a.txt", "b.txt"):
                (target / name).write_text(f"old {name}")
            original_rmtree = shutil.rmtree
            cleanup_calls = []

            def fail_partial_cleanup(path, *args, **kwargs):
                if path == "backup":
                    cleanup_calls.append(path)
                    os.unlink("backup/a.txt", dir_fd=kwargs["dir_fd"])
                    raise OSError(errno.EBUSY, "injected partial cleanup failure")
                return original_rmtree(path, *args, **kwargs)

            with (
                patch.object(
                    download.shutil, "rmtree", side_effect=fail_partial_cleanup
                ),
                patch.object(self.downloader.logger, "warning") as warning,
            ):
                success, error = self._apply_overlay(
                    target, {"a.txt": "new a", "b.txt": "new b"}
                )
            self.assertTrue(success, error)
            self.assertEqual(["backup"], cleanup_calls)
            self.assertEqual("new a", (target / "a.txt").read_text())
            self.assertEqual("new b", (target / "b.txt").read_text())
            workspaces = list(target.glob(".dumb-overlay-*"))
            self.assertEqual(1, len(workspaces))
            self.assertEqual("old b.txt", (workspaces[0] / "backup/b.txt").read_text())
            self.assertIn(str(workspaces[0] / "backup"), str(warning.call_args))

    def test_directory_replacement_failure_restores_all_original_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "service")
            (target / "a-directory").mkdir(parents=True)
            (target / "a-directory/one.txt").write_text("one")
            (target / "a-directory/two.txt").write_text("two")
            (target / "z.txt").write_text("old z")
            original_replace = os.replace

            def fail_later_install(source, destination, **kwargs):
                if "/stage/" in str(source) and destination == "z.txt":
                    raise OSError(errno.EIO, "injected later failure")
                return original_replace(source, destination, **kwargs)

            with patch.object(download.os, "replace", side_effect=fail_later_install):
                success, error = self._apply_overlay(
                    target, {"a-directory": "replacement file", "z.txt": "new z"}
                )
            self.assertFalse(success)
            self.assertIn("injected later failure", error)
            self.assertEqual("one", (target / "a-directory/one.txt").read_text())
            self.assertEqual("two", (target / "a-directory/two.txt").read_text())
            self.assertEqual("old z", (target / "z.txt").read_text())
            self.assertFalse(list(target.glob(".dumb-overlay-*")))

    def test_file_to_directory_replacement_rolls_back_after_child_install_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "service")
            target.mkdir()
            (target / "folder").write_text("original file")
            original_replace = os.replace

            def fail_child_install(source, destination, **kwargs):
                if "/stage/" in str(source):
                    raise OSError(errno.EIO, "injected child failure")
                return original_replace(source, destination, **kwargs)

            with patch.object(download.os, "replace", side_effect=fail_child_install):
                success, error = self._apply_overlay(
                    target, {"folder/child.txt": "new child"}
                )
            self.assertFalse(success)
            self.assertIn("injected child failure", error)
            self.assertEqual("original file", (target / "folder").read_text())
            self.assertEqual(["folder"], [p.name for p in target.iterdir()])

    def test_directory_replacement_cannot_delete_excluded_descendant(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "service")
            (target / "storage").mkdir(parents=True)
            (target / "storage/state.db").write_text("persistent data")
            (target / "a.txt").write_text("old runtime")
            success, error = self._apply_overlay(
                target,
                {"a.txt": "new runtime", "storage": "replacement"},
                exclude_dirs=[str(target / "storage/state.db")],
            )
            self.assertFalse(success)
            self.assertIn("excluded data", error)
            self.assertEqual(
                "persistent data", (target / "storage/state.db").read_text()
            )
            self.assertEqual("old runtime", (target / "a.txt").read_text())
            self.assertFalse(list(target.glob(".dumb-overlay-*")))

    def test_replacing_directory_symlink_cannot_remove_excluded_data_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "service")
            data = Path(temporary, "protected-data")
            target.mkdir()
            data.mkdir()
            (data / "state.db").write_text("persistent data")
            (target / "storage").symlink_to(data, target_is_directory=True)
            (target / "a.txt").write_text("old runtime")
            success, error = self._apply_overlay(
                target,
                {"a.txt": "candidate", "storage": "replacement"},
                exclude_dirs=["storage/state.db"],
            )
            self.assertFalse(success)
            self.assertIn("excluded data", error)
            self.assertTrue((target / "storage").is_symlink())
            self.assertEqual(
                "persistent data", (target / "storage/state.db").read_text()
            )
            self.assertEqual("old runtime", (target / "a.txt").read_text())

    def test_existing_leaf_symlink_is_replaced_without_changing_its_referent(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("new-target.txt", "new target")
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "new-target.txt")
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "service")
            target.mkdir()
            (target / "old-target.txt").write_text("old target")
            (target / "link").symlink_to("old-target.txt")
            response = FakeResponse(
                200,
                {"Content-Disposition": "attachment; filename=app.zip"},
                buffer.getvalue(),
            )
            with patch.object(
                self.downloader, "fetch_with_retries", return_value=response
            ):
                success, error = self.downloader.download_and_extract(
                    "https://example.test/app.zip", str(target)
                )
            self.assertTrue(success, error)
            self.assertEqual("new-target.txt", os.readlink(target / "link"))
            self.assertEqual("old target", (target / "old-target.txt").read_text())
            self.assertEqual("new target", (target / "link").read_text())

    def test_existing_directory_symlink_is_rejected_before_any_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "service")
            outside = Path(temporary, "outside")
            target.mkdir()
            outside.mkdir()
            (outside / "runtime.txt").write_text("outside runtime")
            (target / "nested").symlink_to(outside, target_is_directory=True)
            (target / "a.txt").write_text("old runtime")
            success, error = self._apply_overlay(
                target, {"a.txt": "new runtime", "nested/runtime.txt": "candidate"}
            )
            self.assertFalse(success)
            self.assertIn("directory symlink", error)
            self.assertEqual("old runtime", (target / "a.txt").read_text())
            self.assertEqual("outside runtime", (outside / "runtime.txt").read_text())
            self.assertTrue((target / "nested").is_symlink())

    def test_nested_mount_preflight_preserves_entire_install(self):
        for replace_parent in (False, True):
            with (
                self.subTest(replace_parent=replace_parent),
                tempfile.TemporaryDirectory() as temporary,
            ):
                target = Path(temporary, "service")
                (target / "folder/nested").mkdir(parents=True)
                (target / "folder/nested/state.db").write_text("mounted data")
                (target / "a.txt").write_text("old runtime")
                original_mount_id = download._mount_id

                def nested_mount_id(descriptor):
                    actual = original_mount_id(descriptor)
                    path = os.readlink(f"/proc/self/fd/{descriptor}")
                    return (
                        actual + 1 if path == str(target / "folder/nested") else actual
                    )

                entries = (
                    {"a.txt": "new runtime", "folder": "replacement"}
                    if replace_parent
                    else {
                        "a.txt": "new runtime",
                        "folder/nested/state.db": "replacement",
                    }
                )
                with patch.object(download, "_mount_id", side_effect=nested_mount_id):
                    success, error = self._apply_overlay(target, entries)
                self.assertFalse(success)
                self.assertIn("nested mount", error)
                self.assertEqual("old runtime", (target / "a.txt").read_text())
                self.assertEqual(
                    "mounted data", (target / "folder/nested/state.db").read_text()
                )
                self.assertFalse(list(target.glob(".dumb-overlay-*")))

    def test_excluded_nested_mount_does_not_block_unrelated_runtime_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "service")
            (target / "data").mkdir(parents=True)
            (target / "data/state.db").write_text("mounted data")
            (target / "runtime.txt").write_text("old runtime")
            original_mount_id = download._mount_id

            def nested_mount_id(descriptor):
                actual = original_mount_id(descriptor)
                return (
                    actual + 1
                    if os.readlink(f"/proc/self/fd/{descriptor}")
                    == str(target / "data")
                    else actual
                )

            with patch.object(download, "_mount_id", side_effect=nested_mount_id):
                success, error = self._apply_overlay(
                    target,
                    {"runtime.txt": "new runtime", "data/state.db": "candidate"},
                    exclude_dirs=["data"],
                )
            self.assertTrue(success, error)
            self.assertEqual("new runtime", (target / "runtime.txt").read_text())
            self.assertEqual("mounted data", (target / "data/state.db").read_text())

    def test_real_mount_root_overlay_works_for_fresh_update_and_rollback(self):
        target = Path("/dev/shm")
        if not target.is_dir() or not os.access(target, os.W_OK):
            self.skipTest("No writable separate temporary filesystem.")
        with open("/proc/self/mountinfo", encoding="ascii") as info:
            if not any(line.split()[4] == "/dev/shm" for line in info):
                self.skipTest("/dev/shm is not a mount root.")
        self.assertNotEqual(os.stat(target).st_dev, os.stat(target.parent).st_dev)
        for existing in (False, True):
            for fail_install in (False, True):
                with (
                    self.subTest(existing=existing, fail_install=fail_install),
                    tempfile.TemporaryDirectory(
                        prefix="dumb-overlay-test-", dir=target
                    ) as temporary,
                ):
                    fixture = Path(temporary)
                    runtime = fixture / "runtime.txt"
                    if existing:
                        runtime.write_text("old runtime")
                    original_replace = os.replace
                    workspaces = []
                    replacements = []

                    def validate_stage(stage):
                        workspace = Path(stage).resolve().parent
                        workspaces.append(workspace)
                        self.assertEqual(target, workspace.parent)
                        return True, None

                    def inspect_replace(source, destination, **kwargs):
                        replacements.append((source, destination))
                        if "/stage/" in str(source) and fail_install:
                            raise OSError(errno.EIO, "injected mount-root failure")
                        return original_replace(source, destination, **kwargs)

                    try:
                        with patch.object(
                            download.os, "replace", side_effect=inspect_replace
                        ):
                            success, error = self._apply_overlay(
                                target,
                                {f"{fixture.name}/runtime.txt": "new runtime"},
                                staging_validator=validate_stage,
                            )
                        self.assertEqual(not fail_install, success, error)
                        if success:
                            self.assertEqual("new runtime", runtime.read_text())
                        elif existing:
                            self.assertEqual("old runtime", runtime.read_text())
                        else:
                            self.assertFalse(runtime.exists())
                        self.assertTrue(replacements)
                        self.assertTrue(all(not path.exists() for path in workspaces))
                    finally:
                        for workspace in workspaces:
                            shutil.rmtree(workspace, ignore_errors=True)

    def test_recovery_workspace_blocks_retry_without_modifying_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "service")
            backup = target / ".dumb-overlay-retained/backup"
            backup.mkdir(parents=True)
            (backup / "runtime.txt").write_text("recoverable original")
            (target / "runtime.txt").write_text("current runtime")
            with patch.object(download.os, "replace") as replace:
                success, error = self._apply_overlay(
                    target, {"runtime.txt": "candidate"}
                )
            self.assertFalse(success)
            self.assertIn("Unresolved archive recovery workspace", error)
            self.assertIn(str(backup.parent), error)
            replace.assert_not_called()
            self.assertEqual(
                "recoverable original", (backup / "runtime.txt").read_text()
            )
            self.assertEqual("current runtime", (target / "runtime.txt").read_text())
            self.assertEqual([backup.parent], list(target.glob(".dumb-overlay-*")))

    def test_concurrent_overlay_is_rejected_without_affecting_first_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "service")
            target.mkdir()
            (target / "runtime.txt").write_text("old runtime")
            nested_results = []

            def validate_stage(stage):
                nested_results.append(
                    self._apply_overlay(target, {"runtime.txt": "concurrent candidate"})
                )
                self.assertEqual("old runtime", (target / "runtime.txt").read_text())
                return True, None

            success, error = self._apply_overlay(
                target,
                {"runtime.txt": "first candidate"},
                staging_validator=validate_stage,
            )
            self.assertTrue(success, error)
            self.assertEqual(1, len(nested_results))
            self.assertFalse(nested_results[0][0])
            self.assertIn("already in progress", nested_results[0][1])
            self.assertEqual("first candidate", (target / "runtime.txt").read_text())
            self.assertFalse(list(target.glob(".dumb-overlay-*")))

    def test_reserved_workspace_archive_member_cannot_replace_recovery_storage(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "service")
            target.mkdir()
            (target / "runtime.txt").write_text("old runtime")
            success, error = self._apply_overlay(
                target,
                {
                    "runtime.txt": "candidate",
                    ".dumb-overlay-forged/backup/data.txt": "forged",
                },
            )
            self.assertFalse(success)
            self.assertIn("reserved recovery workspace", error)
            self.assertEqual("old runtime", (target / "runtime.txt").read_text())
            self.assertEqual(["runtime.txt"], [p.name for p in target.iterdir()])

    def test_target_rename_does_not_redirect_protected_data_checks_or_mutations(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "service")
            original = Path(temporary, "original-service")
            (target / "storage").mkdir(parents=True)
            (target / "storage/state.db").write_text("persistent original")
            (target / "a.txt").write_text("old runtime")

            def move_target(stage):
                target.rename(original)
                target.mkdir()
                (target / "a.txt").write_text("replacement root")
                return True, None

            success, error = self._apply_overlay(
                target,
                {"a.txt": "candidate", "storage": "replacement"},
                exclude_dirs=["storage/state.db"],
                staging_validator=move_target,
            )
            self.assertFalse(success)
            self.assertIn("excluded data", error)
            self.assertEqual(
                "persistent original", (original / "storage/state.db").read_text()
            )
            self.assertEqual("old runtime", (original / "a.txt").read_text())
            self.assertEqual("replacement root", (target / "a.txt").read_text())
            self.assertFalse(list(original.glob(".dumb-overlay-*")))

    def test_fresh_install_failure_removes_empty_created_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary, "new-service")
            with patch.object(
                download.os,
                "replace",
                side_effect=OSError(errno.EIO, "injected failure"),
            ):
                success, error = self._apply_overlay(
                    target, {"runtime.txt": "candidate"}
                )
            self.assertFalse(success)
            self.assertIn("injected failure", error)
            self.assertFalse(target.exists())

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
