import threading
import unittest
from unittest.mock import Mock, patch

from utils.auto_update import Update
from utils.infinidysk_release_line import (
    validate_infinidysk_release,
    validate_infinidysk_source,
)


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _downloader(compare_status="ahead"):
    downloader = Mock()
    downloader.get_headers.return_value = {}
    downloader.get_ref_commit_sha.return_value = ("a" * 40, None)
    downloader.fetch_with_retries.return_value = FakeResponse(
        200, {"status": compare_status}
    )
    return downloader


def _official(**service):
    return {"repo_owner": "infinidysk", "repo_name": "infinidysk", **service}


class InfinidyskReleaseLineTests(unittest.TestCase):
    def test_version_tags_are_judged_without_github(self):
        downloader = _downloader()

        self.assertEqual(
            (True, None), validate_infinidysk_release("v1.7.2", downloader)
        )
        supported, error = validate_infinidysk_release("v2.0.0", downloader)

        self.assertFalse(supported)
        self.assertIn("1.x only", error)
        downloader.get_ref_commit_sha.assert_not_called()

    def test_named_refs_must_be_part_of_v1(self):
        for status, expected in (("ahead", True), ("diverged", False)):
            with self.subTest(status=status):
                downloader = _downloader(status)

                supported, _ = validate_infinidysk_release("rc", downloader)

                self.assertEqual(expected, supported)
                url = downloader.fetch_with_retries.call_args.args[0]
                self.assertTrue(url.endswith(f"compare/{'a' * 40}...v1"))

    def test_fixed_sources_are_checked_and_moving_channels_are_not(self):
        cases = [
            (_official(commit_sha="b" * 40), "diverged", False),
            (_official(branch_enabled=True, branch="main"), "diverged", False),
            (_official(branch_enabled=True, branch="v1"), "identical", True),
            (
                _official(release_version_enabled=True, release_version="v2.0.0"),
                "ahead",
                False,
            ),
            (
                _official(release_version_enabled=True, release_version="latest"),
                "diverged",
                True,
            ),
            (
                _official(release_version_enabled=True, release_version="prerelease"),
                "diverged",
                True,
            ),
            (_official(), "diverged", True),
            (
                {"repo_owner": "someone", "repo_name": "fork", "commit_sha": "c" * 40},
                "diverged",
                True,
            ),
        ]
        for service, status, expected in cases:
            with self.subTest(service=service):
                supported, _ = validate_infinidysk_source(service, _downloader(status))

                self.assertEqual(expected, supported)

    def test_commit_must_be_a_full_sha_before_any_lookup(self):
        for commit in ("v1", "abc1234", "a" * 39 + "g"):
            with self.subTest(commit=commit):
                downloader = _downloader("identical")

                supported, error = validate_infinidysk_source(
                    _official(commit_sha=commit), downloader
                )

                self.assertFalse(supported)
                self.assertIn("40-character", error)
                downloader.fetch_with_retries.assert_not_called()

    def test_nightly_selectors_are_checked_like_install_resolves_them(self):
        cases = (
            ("v2.0.0-nightly.3", "ahead", False, False),
            ("v1.4.0-nightly.3", "diverged", True, False),
            ("nightly", "ahead", True, True),
            ("nightly-v2", "diverged", False, True),
        )
        for release, compare_status, expected, looked_up in cases:
            with self.subTest(release=release):
                downloader = _downloader(compare_status)

                supported, _ = validate_infinidysk_source(
                    _official(release_version_enabled=True, release_version=release),
                    downloader,
                )

                self.assertEqual(expected, supported)
                self.assertEqual(looked_up, downloader.fetch_with_retries.called)

    def test_github_failure_refuses_fixed_source(self):
        downloader = _downloader()
        downloader.fetch_with_retries.return_value = FakeResponse(502)

        supported, error = validate_infinidysk_source(
            _official(commit_sha="b" * 40), downloader
        )

        self.assertFalse(supported)
        self.assertIn("502", error)

    def test_pinned_2x_update_is_refused_before_the_service_stops(self):
        updater = Update.__new__(Update)
        updater.downloader = _downloader()
        updater.process_handler = Mock(
            process_names=["InfiniDysk"],
            setup_tracker=["InfiniDysk"],
            setup_tracker_lock=threading.Lock(),
        )
        updater.stop_process = Mock()
        config = _official(
            auto_update=True, release_version_enabled=True, release_version="v2.0.0"
        )

        with patch("utils.auto_update.setup_release_version") as install:
            success, message = updater.update_check(
                "InfiniDysk", config, "infinidysk", None
            )

        self.assertFalse(success)
        self.assertIn("1.x only", message)
        updater.stop_process.assert_not_called()
        install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
