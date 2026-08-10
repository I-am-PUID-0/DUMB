import sys
import tempfile
import types
import unittest


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeDownloader:
    latest_calls = 0

    def __init__(self):
        self.headers = {"Accept": "application/vnd.github.v3+json"}
        self.branch_response = FakeResponse(200, {"sha": "abcdef1234567890"})

    def get_headers(self):
        return self.headers

    def fetch_with_retries(self, url, headers):
        self.last_url = url
        return self.branch_response

    def get_latest_release(
        self, repo_owner, repo_name, nightly=False, prerelease=False
    ):
        FakeDownloader.latest_calls += 1
        return "v2.5.1", None

    def get_ref_commit_sha(self, repo_owner, repo_name, ref):
        return "abcdef1234567890abcdef1234567890abcdef12", None


def _install_runtime_stubs():
    global_logger = types.ModuleType("utils.global_logger")
    global_logger.logger = _Logger()
    sys.modules["utils.global_logger"] = global_logger

    download = types.ModuleType("utils.download")
    download.Downloader = FakeDownloader
    sys.modules["utils.download"] = download

    config_loader = types.ModuleType("utils.config_loader")
    config_loader.CONFIG_MANAGER = types.SimpleNamespace(
        get=lambda *args, **kwargs: {},
        get_instance=lambda *args, **kwargs: {},
    )
    sys.modules["utils.config_loader"] = config_loader

    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_stub


_install_runtime_stubs()

sys.modules.pop("utils.versions", None)
from utils.versions import Versions

versions_module = sys.modules["utils.versions"]


class VersionsHelperTests(unittest.TestCase):
    def setUp(self):
        Versions._latest_release_cache = {}
        FakeDownloader.latest_calls = 0

    def test_parse_version_tuple_ignores_prefixes_and_text(self):
        self.assertEqual(Versions._parse_version_tuple("v2.5.1"), (2, 5, 1))
        self.assertEqual(
            Versions._parse_version_tuple("release-10.2-beta3"), (10, 2, 3)
        )
        self.assertIsNone(Versions._parse_version_tuple("latest"))
        self.assertIsNone(Versions._parse_version_tuple(None))

    def test_normalize_arr_version_collapses_non_digit_separators(self):
        self.assertEqual(Versions._normalize_arr_version("v4.0.15.2940"), "4.0.15.2940")
        self.assertEqual(
            Versions._normalize_arr_version("4-0-15 beta 2940"), "4.0.15.2940"
        )
        self.assertEqual(Versions._normalize_arr_version("nightly"), "nightly")

    def test_nzbdav_stable_release_hides_internal_commit_suffix(self):
        self.assertEqual(
            Versions.display_version("nzbdav", "v0.10.0-0dec23ac"),
            "v0.10.0",
        )
        self.assertEqual(
            Versions.display_version("nzbdav", "2026.08.05-0dec23ac"),
            "2026.08.05",
        )

    def test_nzbdav_rolling_versions_keep_commit_suffix(self):
        self.assertEqual(
            Versions.display_version("nzbdav", "v0.10.0-rc.3-cf468605"),
            "v0.10.0-rc.3-cf468605",
        )
        self.assertEqual(
            Versions.display_version("nzbdav", "dev-cf468605"),
            "dev-cf468605",
        )
        self.assertEqual(
            Versions.display_version("nzbdav", "main-cf468605"),
            "main-cf468605",
        )

    def test_non_nzbdav_versions_are_unchanged(self):
        self.assertEqual(
            Versions.display_version("decypharr", "v2.4-0dec23ac"),
            "v2.4-0dec23ac",
        )

    def test_control_plane_comparison_includes_release_distance(self):
        versions = Versions()
        versions.downloader.get_latest_release = lambda *args, **kwargs: (
            "v2.5.1",
            None,
        )
        versions.downloader.count_releases_behind = lambda *args, **kwargs: (4, None)
        versions.version_check = lambda *args, **kwargs: ("v2.1.0", None)

        update_needed, info = versions.compare_versions(
            "DUMB API",
            "I-am-PUID-0",
            "DUMB",
            None,
            "dumb_api_service",
        )

        self.assertTrue(update_needed)
        self.assertEqual(4, info["releases_behind"])

    def test_api_dev_build_is_not_downgraded_to_older_stable_release(self):
        versions = Versions()
        versions.downloader.get_latest_release = lambda *args, **kwargs: (
            "v2.10.0",
            None,
        )
        versions.version_check = lambda *args, **kwargs: ("2.11.0-dev.12", None)

        update_needed, info = versions.compare_versions(
            "DUMB API",
            "I-am-PUID-0",
            "DUMB",
            None,
            "dumb_api_service",
        )

        self.assertFalse(update_needed)
        self.assertIn("ahead", info["message"])

    def test_is_latest_release_gt_uses_cache_after_first_lookup(self):
        versions = Versions()

        first = versions.is_latest_release_gt("owner", "repo", "2.5.0")
        second = versions.is_latest_release_gt("owner", "repo", "2.5.1")

        self.assertEqual(first, (True, "v2.5.1", None))
        self.assertEqual(second, (True, "v2.5.1", None))
        self.assertEqual(FakeDownloader.latest_calls, 1)

    def test_is_latest_release_gt_reports_invalid_base_versions(self):
        versions = Versions()

        self.assertEqual(
            versions.is_latest_release_gt("owner", "repo", "not-a-version"),
            (False, "v2.5.1", "Invalid version format for comparison"),
        )

    def test_get_branch_head_marker_returns_branch_short_sha(self):
        versions = Versions()

        marker, error = versions._get_branch_head_marker(
            "owner", "repo", "feature/test"
        )

        self.assertEqual(marker, "feature/test-abcdef12")
        self.assertIsNone(error)
        self.assertIn("feature%2Ftest", versions.downloader.last_url)

    def test_get_branch_head_marker_reports_non_200_response(self):
        versions = Versions()
        versions.downloader.branch_response = FakeResponse(404, {})

        marker, error = versions._get_branch_head_marker("owner", "repo", "missing")

        self.assertIsNone(marker)
        self.assertEqual(error, "Unable to resolve branch head sha (status: 404)")

    def test_nzbdav_prerelease_marker_matches_same_tag_commit(self):
        versions = Versions()
        versions.downloader.get_latest_release = lambda *args, **kwargs: (
            "v0.10.0-rc.3",
            None,
        )
        versions.downloader.get_ref_commit_sha = lambda *args, **kwargs: (
            "cf468605" + ("a" * 32),
            None,
        )
        versions.version_check = lambda *args, **kwargs: (
            "v0.10.0-rc.3-cf468605",
            None,
        )

        update_needed, info = versions.compare_versions(
            "NzbDAV",
            "nzbdav",
            "nzbdav",
            None,
            "nzbdav",
            prerelease=True,
        )

        self.assertFalse(update_needed)
        self.assertEqual(info["message"], "No updates available")
        self.assertEqual(info["current_version"], "v0.10.0-rc.3-cf468605")
        self.assertEqual(info["latest_version"], "v0.10.0-rc.3")

    def test_nzbdav_stable_marker_compares_by_commit_but_displays_release(self):
        versions = Versions()
        versions.downloader.get_latest_release = lambda *args, **kwargs: (
            "v0.10.0",
            None,
        )
        versions.downloader.get_ref_commit_sha = lambda *args, **kwargs: (
            "0dec23ac" + ("a" * 32),
            None,
        )
        versions.version_check = lambda *args, **kwargs: (
            "v0.10.0-0dec23ac",
            None,
        )

        update_needed, info = versions.compare_versions(
            "NzbDAV",
            "nzbdav",
            "nzbdav",
            None,
            "nzbdav",
        )

        self.assertFalse(update_needed)
        self.assertEqual(info["current_version"], "v0.10.0")
        self.assertEqual(info["latest_version"], "v0.10.0")

    def test_nzbdav_prerelease_marker_detects_moved_tag(self):
        versions = Versions()
        versions.downloader.get_latest_release = lambda *args, **kwargs: (
            "v0.10.0-rc.3",
            None,
        )
        versions.downloader.get_ref_commit_sha = lambda *args, **kwargs: (
            "cf468605" + ("a" * 32),
            None,
        )
        versions.version_check = lambda *args, **kwargs: (
            "v0.10.0-rc.3-deadbeef",
            None,
        )

        update_needed, info = versions.compare_versions(
            "NzbDAV",
            "nzbdav",
            "nzbdav",
            None,
            "nzbdav",
            prerelease=True,
        )

        self.assertTrue(update_needed)
        self.assertEqual(info["latest_version"], "v0.10.0-rc.3")

    def test_nzbdav_plain_tag_marker_reinstalls_once_for_commit_tracking(self):
        versions = Versions()
        versions.downloader.get_latest_release = lambda *args, **kwargs: (
            "v0.10.0-rc.3",
            None,
        )
        versions.downloader.get_ref_commit_sha = lambda *args, **kwargs: (
            "cf468605" + ("a" * 32),
            None,
        )
        versions.version_check = lambda *args, **kwargs: (
            "v0.10.0-rc.3",
            None,
        )

        update_needed, _ = versions.compare_versions(
            "NzbDAV",
            "nzbdav",
            "nzbdav",
            None,
            "nzbdav",
            prerelease=True,
        )

        self.assertTrue(update_needed)

    def test_nzbdav_release_comparison_fails_closed_without_commit_resolution(self):
        versions = Versions()
        versions.downloader.get_latest_release = lambda *args, **kwargs: (
            "v0.10.0-rc.3",
            None,
        )
        versions.downloader.get_ref_commit_sha = lambda *args, **kwargs: (
            None,
            "GitHub commit lookup unavailable.",
        )
        versions.version_check = lambda *args, **kwargs: (
            "v0.10.0-rc.3-cf468605",
            None,
        )

        update_needed, error = versions.compare_versions(
            "NzbDAV",
            "nzbdav",
            "nzbdav",
            None,
            "nzbdav",
            prerelease=True,
        )

        self.assertFalse(update_needed)
        self.assertEqual(error, "GitHub commit lookup unavailable.")

    def test_altmount_version_check_reads_version_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(f"{tmpdir}/version.txt", "w", encoding="utf-8") as handle:
                handle.write("v0.2.0")

            versions = Versions()
            original_config_manager = versions_module.CONFIG_MANAGER
            versions_module.CONFIG_MANAGER = types.SimpleNamespace(
                get_instance=lambda *args, **kwargs: {"config_dir": tmpdir}
            )
            self.addCleanup(
                lambda: setattr(
                    versions_module, "CONFIG_MANAGER", original_config_manager
                )
            )
            version, error = versions.version_check(
                process_name="AltMount",
                key="altmount",
            )

            self.assertEqual(version, "v0.2.0")
            self.assertIsNone(error)

    def test_commit_pin_prefers_managed_marker_over_upstream_version_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(f"{tmpdir}/version.txt", "w", encoding="utf-8") as handle:
                handle.write("commit-aaaaaaaaaaaa")

            versions = Versions()
            original_config_manager = versions_module.CONFIG_MANAGER
            versions_module.CONFIG_MANAGER = types.SimpleNamespace(
                get_instance=lambda *args, **kwargs: {
                    "config_dir": tmpdir,
                    "commit_sha": "a" * 40,
                }
            )
            self.addCleanup(
                lambda: setattr(
                    versions_module, "CONFIG_MANAGER", original_config_manager
                )
            )

            version, error = versions.version_check(
                process_name="Riven Backend",
                key="riven_backend",
            )

            self.assertEqual(version, "commit-aaaaaaaaaaaa")
            self.assertIsNone(error)

    def test_bazarr_version_check_reads_release_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(f"{tmpdir}/VERSION", "w", encoding="utf-8") as handle:
                handle.write("v1.6.0")

            versions = Versions()
            original_config_manager = versions_module.CONFIG_MANAGER
            versions_module.CONFIG_MANAGER = types.SimpleNamespace(
                get_instance=lambda *args, **kwargs: {"config_dir": tmpdir}
            )
            self.addCleanup(
                lambda: setattr(
                    versions_module, "CONFIG_MANAGER", original_config_manager
                )
            )
            version, error = versions.version_check(
                process_name="Bazarr",
                key="bazarr",
            )

            self.assertEqual(version, "v1.6.0")
            self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
