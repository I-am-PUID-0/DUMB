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


if __name__ == "__main__":
    unittest.main()
