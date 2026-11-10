import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from utils import audiobookshelf_installer as installer


class AudiobookshelfInstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dumb-abs-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {
            "runtime_dir": str(self.root / "runtime"),
            "config_dir": str(self.root / "config"),
            "metadata_dir": str(self.root / "metadata"),
        }
        for key in ("config_dir", "metadata_dir"):
            Path(self.config[key]).mkdir()
        self.downloader = Mock()
        self.downloader.get_latest_release.return_value = ("v2.37.1", None)
        self.downloader.fetch_github_release_info.return_value = (
            {"tag_name": "v2.37.1", "draft": False, "prerelease": False},
            None,
        )
        self.downloader.get_ref_commit_sha.return_value = ("a" * 40, None)
        self.downloader.get_headers.return_value = {}
        self.package_response = Mock(status_code=200)
        self.package_response.json.return_value = {
            "name": "audiobookshelf",
            "version": "2.37.1",
        }
        self.downloader.fetch_with_retries.return_value = self.package_response
        for target in (
            "utils.audiobookshelf_installer.install_cache_root",
            "utils.transactional_install.install_cache_root",
        ):
            self.enterContext(patch(target, return_value=self.root / "cache"))

    def _source(self, root, version="2.37.1"):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        (root / "client").mkdir(exist_ok=True)
        for path, name in (
            (root, "audiobookshelf"),
            (root / "client", "audiobookshelf-client"),
        ):
            (path / "package.json").write_text(
                json.dumps({"name": name, "version": version})
            )
            (path / "package-lock.json").write_text("{}")

    def _compiled(self, root, version="2.37.1"):
        self._source(root, version)
        for relative, content in (
            ("dist-server/index.js", "server fixture"),
            ("client/dist/index.html", "<html>client fixture</html>"),
            ("node_modules/sqlite3/package.json", '{"name":"sqlite3"}'),
            ("node_modules/sqlite3/build/Release/node_sqlite3.node", "native fixture"),
        ):
            path = Path(root) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def _installed(
        self,
        version="2.37.1",
        selector="latest",
        *,
        source_kind=None,
        branch="",
        commit="a" * 40,
    ):
        root = Path(self.config["runtime_dir"])
        self._compiled(root, version)
        metadata = {
            "version": version,
            "release_tag": "v" + version,
            "commit_sha": commit,
            "install_selector": selector,
            "node_major": 24,
        }
        if source_kind is not None:
            metadata["source_kind"] = source_kind
            if source_kind != "release":
                metadata["release_tag"] = ""
                metadata["branch"] = branch
        (root / ".dumb-audiobookshelf.json").write_text(json.dumps(metadata))
        return root

    def _download(self, url, target_dir, **kwargs):
        self.assertIn("/archive/" + "a" * 40 + ".tar.gz", url)
        self.assertEqual("audiobookshelf-" + "a" * 40, kwargs["zip_folder_name"])
        self._source(target_dir)
        return True, None

    def _build(self, root, env, expected):
        self.assertEqual("2.37.1", expected)
        self.assertEqual("0", env["CYPRESS_INSTALL_BINARY"])
        self.assertEqual("/audiobookshelf", env["ROUTER_BASE_PATH"])
        self.assertTrue(Path(env["npm_config_cache"]).is_relative_to(self.root))
        self._compiled(root)

    def test_selectors_use_latest_by_default_and_require_published_release_shape(self):
        self.assertEqual("latest", installer.audiobookshelf_install_selector({}))
        for tag in ("v2.37.1", "2.37.1", "v2.37.1-beta.1"):
            config = {"release_version_enabled": True, "release_version": tag}
            self.assertEqual(tag, installer.audiobookshelf_install_selector(config))
        for changes in (
            {"release_version_enabled": True, "release_version": ""},
            {"release_version_enabled": True, "release_version": "../master"},
            {"release_version_enabled": True, "release_version": "master"},
            {"branch_enabled": True},
            {"commit_sha": "invalid"},
            {"repo_owner": "unrelated"},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(installer.AudiobookshelfInstallError),
            ):
                installer.audiobookshelf_install_selector(changes)

    def test_commit_and_branch_selection_take_precedence_over_releases(self):
        config = {
            "release_version_enabled": True,
            "release_version": "v2.37.1",
            "branch_enabled": True,
            "branch": "feature/native-player",
        }
        self.assertEqual(
            "branch:feature/native-player",
            installer.audiobookshelf_install_selector(config),
        )
        self.assertEqual(
            "commit:" + "b" * 40,
            installer.audiobookshelf_install_selector(
                {**config, "commit_sha": "B" * 40}
            ),
        )
        self.assertEqual(
            "commit:" + "b" * 40,
            installer.audiobookshelf_install_selector(
                {"branch_enabled": False, "commit_sha": "b" * 40}
            ),
        )
        with self.assertRaisesRegex(installer.AudiobookshelfInstallError, "official"):
            installer.audiobookshelf_install_selector(
                {**config, "repo_name": "untrusted", "commit_sha": "b" * 40}
            )

    def test_invalid_branch_revision_expressions_and_short_commit_pins_are_rejected(
        self,
    ):
        for branch in (
            "",
            "../master",
            "master~1",
            "master^",
            "master:package.json",
            "master@{1}",
            "master.lock",
            "feature/.hidden",
            "feature//test",
            "/master",
            "-master",
            "master/",
            "master.",
            "master test",
            "master\n",
            "master?test",
            "master[test",
            "master\\test",
        ):
            with (
                self.subTest(branch=branch),
                self.assertRaises(installer.AudiobookshelfInstallError),
            ):
                installer.audiobookshelf_install_selector(
                    {"branch_enabled": True, "branch": branch}
                )
        for commit in ("abcdef0", "a" * 39, "g" * 40, "a" * 40 + "^"):
            with (
                self.subTest(commit=commit),
                self.assertRaises(installer.AudiobookshelfInstallError),
            ):
                installer.audiobookshelf_install_selector({"commit_sha": commit})

    def test_published_release_resolves_explicit_tag_namespace(self):
        status = installer.audiobookshelf_target_status(
            self.config, downloader=self.downloader
        )
        self.downloader.get_ref_commit_sha.assert_called_once_with(
            "advplyr", "audiobookshelf", "refs/tags/v2.37.1"
        )
        self.assertEqual("release", status["source_kind"])
        self.assertEqual("2.37.1", status["available_version"])
        self.downloader.fetch_with_retries.assert_not_called()

    def test_branch_resolves_explicit_heads_ref_and_immutable_package_version(self):
        status = installer.audiobookshelf_target_status(
            {**self.config, "branch_enabled": True, "branch": "feature/player"},
            downloader=self.downloader,
        )
        self.downloader.get_ref_commit_sha.assert_called_once_with(
            "advplyr", "audiobookshelf", "refs/heads/feature/player"
        )
        self.downloader.get_latest_release.assert_not_called()
        self.downloader.fetch_github_release_info.assert_not_called()
        url, headers = self.downloader.fetch_with_retries.call_args.args
        self.assertEqual(
            "https://api.github.com/repos/advplyr/audiobookshelf/contents/"
            "package.json?ref=" + "a" * 40,
            url,
        )
        self.assertEqual("application/vnd.github.raw+json", headers["Accept"])
        self.assertEqual("branch", status["source_kind"])
        self.assertEqual("feature/player-" + "a" * 12, status["available_version"])
        self.assertEqual("2.37.1", status["available_package_version"])

    def test_branch_head_movement_is_detected_even_without_a_package_version_bump(self):
        self._installed(selector="branch:master", source_kind="branch", branch="master")
        config = {**self.config, "branch_enabled": True, "branch": "master"}
        status = installer.audiobookshelf_target_status(
            config, downloader=self.downloader
        )
        self.assertTrue(status["installed"])
        self.assertEqual("master-" + "a" * 12, status["current_version"])
        self.downloader.get_ref_commit_sha.return_value = ("b" * 40, None)
        status = installer.audiobookshelf_target_status(
            config, downloader=self.downloader
        )
        self.assertFalse(status["installed"])
        self.assertEqual("master-" + "b" * 12, status["available_version"])
        self.assertEqual("a" * 40, status["current_commit_sha"])
        self.assertEqual(
            status["current_package_version"], status["available_package_version"]
        )

    def test_release_branch_and_commit_runtime_identities_are_not_interchangeable(self):
        runtime = self._installed()
        for selection in ("branch:master", "commit:" + "a" * 40):
            self.assertFalse(
                installer.audiobookshelf_runtime_matches_selection(runtime, selection)
            )
        self._installed(selector="branch:master", source_kind="branch", branch="master")
        for selection in (
            "latest",
            "v2.37.1",
            "branch:develop",
            "commit:" + "a" * 40,
        ):
            self.assertFalse(
                installer.audiobookshelf_runtime_matches_selection(runtime, selection)
            )
        self.assertTrue(
            installer.audiobookshelf_runtime_matches_selection(runtime, "branch:master")
        )
        self._installed(selector="commit:" + "a" * 40, source_kind="commit")
        self.assertTrue(
            installer.audiobookshelf_runtime_matches_selection(
                runtime, "commit:" + "a" * 40
            )
        )
        self.assertFalse(
            installer.audiobookshelf_runtime_matches_selection(
                runtime, "commit:" + "b" * 40
            )
        )
        self.assertFalse(
            installer.audiobookshelf_runtime_matches_selection(runtime, "branch:master")
        )

    def test_matching_source_runtime_is_reused_offline_at_startup(self):
        for kind, changes, selector in (
            (
                "branch",
                {"branch_enabled": True, "branch": "master"},
                "branch:master",
            ),
            ("commit", {"commit_sha": "a" * 40}, "commit:" + "a" * 40),
        ):
            with self.subTest(kind=kind):
                self._installed(
                    selector=selector,
                    source_kind=kind,
                    branch="master" if kind == "branch" else "",
                )
                with patch.object(installer, "_check_node"):
                    result = installer.install_audiobookshelf_runtime(
                        {**self.config, **changes}, downloader=self.downloader
                    )
                self.assertEqual("2.37.1", result["version"])
                self.assertEqual(kind, result["source_kind"])
                self.assertEqual(
                    ("master" if kind == "branch" else "commit") + "-" + "a" * 12,
                    result["display_version"],
                )
                self.assertEqual([], self.downloader.mock_calls)

    def test_commit_pin_is_resolved_exactly_and_reports_distinct_source_identity(self):
        self._installed(selector="commit:" + "a" * 40, source_kind="commit")
        status = installer.audiobookshelf_target_status(
            {**self.config, "commit_sha": "a" * 40},
            downloader=self.downloader,
        )
        self.assertTrue(status["installed"])
        self.assertEqual("commit-" + "a" * 12, status["available_version"])
        self.downloader.get_ref_commit_sha.assert_called_once_with(
            "advplyr", "audiobookshelf", "a" * 40
        )
        self.downloader.get_ref_commit_sha.return_value = ("b" * 40, None)
        with self.assertRaisesRegex(
            installer.AudiobookshelfInstallError, "source commit"
        ):
            installer.audiobookshelf_target_status(
                {**self.config, "commit_sha": "a" * 40},
                downloader=self.downloader,
            )

    def test_unresolvable_branch_and_invalid_source_package_fail_before_download(self):
        config = {**self.config, "branch_enabled": True, "branch": "master"}
        self.downloader.get_ref_commit_sha.return_value = (None, "branch missing")
        with self.assertRaisesRegex(
            installer.AudiobookshelfInstallError, "branch missing"
        ):
            installer.audiobookshelf_target_status(config, downloader=self.downloader)
        self.downloader.get_ref_commit_sha.return_value = ("a" * 40, None)
        for package in (
            {},
            [],
            {"name": "other", "version": "2.37.1"},
            {"name": "audiobookshelf", "version": "master"},
            {"name": "audiobookshelf", "version": "v2.37.1"},
        ):
            with (
                self.subTest(package=package),
                self.assertRaisesRegex(
                    installer.AudiobookshelfInstallError, "package version"
                ),
            ):
                self.package_response.json.return_value = package
                installer.audiobookshelf_target_status(
                    config, downloader=self.downloader
                )
        self.downloader.download_and_extract.assert_not_called()

    def test_switching_release_to_branch_builds_a_new_runtime_at_the_same_version(self):
        runtime = self._installed()
        self.downloader.download_and_extract.side_effect = self._download
        before = Mock()
        with (
            patch.object(installer, "_check_node"),
            patch.object(installer, "_build_candidate", side_effect=self._build),
        ):
            result = installer.install_audiobookshelf_runtime(
                {**self.config, "branch_enabled": True, "branch": "feature/player"},
                before_activate=before,
                downloader=self.downloader,
            )
        before.assert_called_once_with()
        self.assertEqual("branch", result["source_kind"])
        self.assertEqual("feature/player", result["branch"])
        self.assertEqual("2.37.1", result["version"])
        self.assertEqual("a" * 40, result["commit_sha"])
        self.assertEqual(
            "feature/player-" + "a" * 12 + "\n",
            (runtime / "version.txt").read_text(),
        )
        self.assertEqual(
            "branch:feature/player\n", (runtime / "install-selector.txt").read_text()
        )
        self.assertTrue(installer.audiobookshelf_runtime_ready(runtime))
        self.assertFalse(list(runtime.parent.glob(".runtime.dumb-*")))

    def test_branch_to_release_switch_is_built_even_at_identical_source_commit(self):
        runtime = self._installed(
            selector="branch:master", source_kind="branch", branch="master"
        )
        self.downloader.download_and_extract.side_effect = self._download
        with (
            patch.object(installer, "_check_node"),
            patch.object(installer, "_build_candidate", side_effect=self._build),
        ):
            result = installer.install_audiobookshelf_runtime(
                self.config,
                downloader=self.downloader,
            )
        self.downloader.download_and_extract.assert_called_once()
        self.assertEqual("release", result["source_kind"])
        self.assertEqual("2.37.1", result["display_version"])
        self.assertEqual(["2.37.1"], (runtime / "version.txt").read_text().splitlines())

    def test_commit_pin_builds_the_exact_resolved_archive_and_persists_identity(self):
        runtime = self._installed()
        self.downloader.download_and_extract.side_effect = self._download
        with (
            patch.object(installer, "_check_node"),
            patch.object(installer, "_build_candidate", side_effect=self._build),
        ):
            result = installer.install_audiobookshelf_runtime(
                {
                    **self.config,
                    "commit_sha": "a" * 40,
                    "branch_enabled": True,
                    "branch": "ignored",
                },
                downloader=self.downloader,
            )
        self.downloader.get_ref_commit_sha.assert_called_once_with(
            "advplyr", "audiobookshelf", "a" * 40
        )
        self.assertEqual("commit", result["source_kind"])
        self.assertEqual("", result["branch"])
        self.assertEqual("a" * 40, result["commit_sha"])
        self.assertEqual("commit-" + "a" * 12, result["display_version"])
        marker = json.loads((runtime / ".dumb-audiobookshelf.json").read_text())
        self.assertEqual("commit:" + "a" * 40, marker["install_selector"])
        self.assertEqual(result["display_version"], marker["display_version"])
        self.assertTrue(installer.audiobookshelf_runtime_ready(runtime))

    def test_different_commit_pin_cannot_reuse_or_overwrite_an_installed_source(self):
        runtime = self._installed(selector="commit:" + "a" * 40, source_kind="commit")
        original = (runtime / ".dumb-audiobookshelf.json").read_bytes()
        before = Mock()
        # A mismatched API response cannot weaken an exact commit pin.
        with (
            patch.object(installer, "_check_node"),
            self.assertRaisesRegex(
                installer.AudiobookshelfInstallError, "source commit"
            ),
        ):
            installer.install_audiobookshelf_runtime(
                {**self.config, "commit_sha": "b" * 40},
                before_activate=before,
                downloader=self.downloader,
            )
        before.assert_not_called()
        self.downloader.download_and_extract.assert_not_called()
        self.assertEqual(original, (runtime / ".dumb-audiobookshelf.json").read_bytes())

    def test_failed_branch_update_keeps_runtime_and_source_identity_without_stopping(
        self,
    ):
        runtime = self._installed(
            selector="branch:master", source_kind="branch", branch="master"
        )
        original = (runtime / ".dumb-audiobookshelf.json").read_bytes()
        self.downloader.download_and_extract.side_effect = self._download
        before = Mock()
        with (
            patch.object(installer, "_check_node"),
            patch.object(
                installer,
                "_build_candidate",
                side_effect=installer.AudiobookshelfInstallError("branch build failed"),
            ),
            self.assertRaisesRegex(
                installer.AudiobookshelfInstallError, "branch build failed"
            ),
        ):
            installer.install_audiobookshelf_runtime(
                {**self.config, "branch_enabled": True, "branch": "master"},
                force=True,
                before_activate=before,
                downloader=self.downloader,
            )
        before.assert_not_called()
        self.assertEqual(original, (runtime / ".dumb-audiobookshelf.json").read_bytes())
        self.assertTrue(installer.audiobookshelf_runtime_ready(runtime))
        self.assertFalse(list(runtime.parent.glob(".runtime.dumb-*")))

    def test_invalid_source_marker_cannot_authorize_replacing_runtime_contents(self):
        runtime = self._installed()
        marker = runtime / ".dumb-audiobookshelf.json"
        original = json.loads(marker.read_text())
        for changes in (
            {"source_kind": "unknown"},
            {"source_kind": "branch", "branch": "master", "release_tag": ""},
            {
                "source_kind": "branch",
                "branch": "../master",
                "release_tag": "",
                "install_selector": "branch:../master",
            },
            {
                "source_kind": "commit",
                "release_tag": "",
                "install_selector": "commit:" + "b" * 40,
            },
        ):
            marker.write_text(json.dumps({**original, **changes}))
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(
                    installer.AudiobookshelfInstallError, "unrecognized"
                ),
            ):
                installer.install_audiobookshelf_runtime(
                    self.config, downloader=self.downloader
                )
        self.assertEqual([], self.downloader.mock_calls)

    def test_incomplete_or_incoherent_runtime_is_never_reused(self):
        runtime = self._installed()
        self.assertTrue(installer.audiobookshelf_runtime_ready(runtime))
        (runtime / "client/dist/index.html").unlink()
        self.assertFalse(installer.audiobookshelf_runtime_ready(runtime))
        self._compiled(runtime)
        (runtime / "package.json").write_text(
            '{"name":"audiobookshelf","version":"2.38.0"}'
        )
        self.assertFalse(installer.audiobookshelf_runtime_ready(runtime))

    def test_ownership_recognizes_incomplete_runtime_but_rejects_foreign_contents(self):
        runtime = self._installed(
            selector="branch:master", source_kind="branch", branch="master"
        )
        (runtime / "client/dist/index.html").unlink()
        self.assertFalse(installer.audiobookshelf_runtime_ready(runtime))
        self.assertTrue(installer.audiobookshelf_runtime_owned(runtime))
        unrelated = self.root / "another-application"
        unrelated.mkdir()
        sentinel = unrelated / "database.sqlite"
        sentinel.write_bytes(b"unrelated persistent data")
        self.assertFalse(installer.audiobookshelf_runtime_owned(unrelated))
        self.assertEqual(b"unrelated persistent data", sentinel.read_bytes())
        link = self.root / "linked-runtime"
        link.symlink_to(runtime, target_is_directory=True)
        self.assertFalse(installer.audiobookshelf_runtime_owned(link))
        marker = runtime / ".dumb-audiobookshelf.json"
        marker.chmod(0o666)
        self.assertFalse(installer.audiobookshelf_runtime_owned(runtime))
        self.assertFalse(
            installer.audiobookshelf_runtime_owned(self.root / "missing-runtime")
        )
        self.assertFalse((self.root / "missing-runtime").exists())

    def test_selection_matches_actual_version_and_latest_reuses_valid_installed_runtime(
        self,
    ):
        runtime = self._installed(selector="v2.37.1")
        self.assertTrue(
            installer.audiobookshelf_runtime_matches_selection(runtime, "latest")
        )
        self.assertTrue(
            installer.audiobookshelf_runtime_matches_selection(runtime, "v2.37.1")
        )
        self.assertFalse(
            installer.audiobookshelf_runtime_matches_selection(runtime, "v2.38.0")
        )

    def test_existing_runtime_can_be_reused_without_a_release_network_lookup(self):
        runtime = self._installed()
        with patch.object(installer, "_check_node"):
            result = installer.install_audiobookshelf_runtime(
                self.config, downloader=self.downloader
            )
        self.assertEqual("2.37.1", result["version"])
        self.assertEqual(str(runtime), result["runtime_dir"])
        self.assertEqual([], self.downloader.mock_calls)

    def test_status_compares_published_source_commit_and_version(self):
        self._installed()
        status = installer.audiobookshelf_target_status(
            self.config, downloader=self.downloader
        )
        self.assertTrue(status["installed"])
        self.assertEqual("2.37.1", status["current_version"])
        self.downloader.get_ref_commit_sha.return_value = ("b" * 40, None)
        status = installer.audiobookshelf_target_status(
            self.config, downloader=self.downloader
        )
        self.assertFalse(status["installed"])

    def test_unpublished_or_invalid_commit_is_refused(self):
        for release, commit in (
            ({"tag_name": "v2.37.1", "draft": True}, "a" * 40),
            ({"tag_name": "v2.37.0"}, "a" * 40),
            ({"tag_name": "v2.37.1"}, "not-a-sha"),
        ):
            with self.subTest(release=release, commit=commit):
                self.downloader.fetch_github_release_info.return_value = (release, None)
                self.downloader.get_ref_commit_sha.return_value = (commit, None)
                with self.assertRaises(installer.AudiobookshelfInstallError):
                    installer.audiobookshelf_target_status(
                        self.config, downloader=self.downloader
                    )

    def test_candidate_build_preserves_data_and_stops_only_after_validation(self):
        runtime = self._installed("2.36.0")
        for key in ("config_dir", "metadata_dir"):
            (Path(self.config[key]) / "sentinel").write_bytes(b"persistent data")
        old = (runtime / "package.json").read_bytes()
        self.downloader.download_and_extract.side_effect = self._download

        def activate():
            self.assertEqual(old, (runtime / "package.json").read_bytes())
            candidates = list(runtime.parent.glob(".runtime.dumb-candidate-*"))
            self.assertEqual(1, len(candidates))
            self.assertTrue(installer.audiobookshelf_runtime_ready(candidates[0]))

        before = Mock(side_effect=activate)
        with (
            patch.object(installer, "_check_node"),
            patch.object(installer, "_build_candidate", side_effect=self._build),
        ):
            result = installer.install_audiobookshelf_runtime(
                self.config,
                force=True,
                before_activate=before,
                downloader=self.downloader,
            )
        before.assert_called_once_with()
        self.assertEqual("2.37.1", result["version"])
        self.assertTrue(installer.audiobookshelf_runtime_ready(runtime))
        for key in ("config_dir", "metadata_dir"):
            self.assertEqual(
                b"persistent data", (Path(self.config[key]) / "sentinel").read_bytes()
            )
        self.assertFalse(list(runtime.parent.glob(".runtime.dumb-*")))

    def test_failed_build_keeps_old_runtime_without_stopping_service(self):
        runtime = self._installed("2.36.0")
        original = (runtime / "package.json").read_bytes()
        self.downloader.download_and_extract.side_effect = self._download
        before = Mock()
        with (
            patch.object(installer, "_check_node"),
            patch.object(
                installer,
                "_build_candidate",
                side_effect=installer.AudiobookshelfInstallError(
                    "fixture build failed"
                ),
            ),
        ):
            with self.assertRaisesRegex(
                installer.AudiobookshelfInstallError, "fixture build failed"
            ):
                installer.install_audiobookshelf_runtime(
                    self.config,
                    force=True,
                    before_activate=before,
                    downloader=self.downloader,
                )
        before.assert_not_called()
        self.assertEqual(original, (runtime / "package.json").read_bytes())
        self.assertFalse(list(runtime.parent.glob(".runtime.dumb-*")))

    def test_activation_failure_restores_old_runtime(self):
        runtime = self._installed("2.36.0")
        original = (runtime / "package.json").read_bytes()
        self.downloader.download_and_extract.side_effect = self._download
        actual = installer.DirectoryReleaseTransaction.activate

        def activate_then_fail(transaction):
            actual(transaction)
            raise OSError("fixture activation failure")

        with (
            patch.object(installer, "_check_node"),
            patch.object(installer, "_build_candidate", side_effect=self._build),
            patch.object(
                installer.DirectoryReleaseTransaction, "activate", activate_then_fail
            ),
        ):
            with self.assertRaisesRegex(OSError, "fixture activation failure"):
                installer.install_audiobookshelf_runtime(
                    self.config, force=True, downloader=self.downloader
                )
        self.assertEqual(original, (runtime / "package.json").read_bytes())
        self.assertFalse(list(runtime.parent.glob(".runtime.dumb-*")))

    def test_runtime_and_persistent_data_overlap_is_refused(self):
        for field in ("config_dir", "metadata_dir"):
            for value in (
                self.config["runtime_dir"],
                str(Path(self.config["runtime_dir"]) / "data"),
            ):
                with (
                    self.subTest(field=field, value=value),
                    patch.object(installer, "_check_node"),
                ):
                    config = {**self.config, field: value}
                    with self.assertRaisesRegex(
                        installer.AudiobookshelfInstallError, "separate"
                    ):
                        installer.install_audiobookshelf_runtime(
                            config, downloader=self.downloader
                        )
        self.downloader.get_latest_release.assert_not_called()

    def test_group_writable_runtime_parent_is_rejected_before_download(self):
        parent = self.root / "unsafe"
        parent.mkdir(mode=0o777)
        parent.chmod(0o777)
        config = {**self.config, "runtime_dir": str(parent / "runtime")}
        with (
            patch.object(installer, "_check_node"),
            self.assertRaisesRegex(
                installer.AudiobookshelfInstallError, "controller-owned"
            ),
        ):
            installer.install_audiobookshelf_runtime(config, downloader=self.downloader)
        self.downloader.get_latest_release.assert_not_called()

    def test_build_uses_frozen_locks_and_validates_native_sqlite_before_ready(self):
        source = self.root / "source"
        self._source(source)
        self._compiled(source)
        (source / "client/node_modules").mkdir()
        (source / "client/.nuxt").mkdir()
        commands = []
        with patch.object(
            installer,
            "_run",
            side_effect=lambda command, *_args, **_kwargs: commands.append(command),
        ):
            installer._build_candidate(source, {}, "2.37.1")
        self.assertEqual(
            ["npm", "ci", "--include=dev", "--no-audit", "--no-fund"], commands[0]
        )
        self.assertEqual(["npm", "run", "build:server"], commands[1])
        self.assertEqual(["npm", "run", "generate"], commands[3])
        self.assertEqual(
            ["npm", "prune", "--omit=dev", "--no-audit", "--no-fund"], commands[4]
        )
        self.assertIn("require('sqlite3')", commands[5][2])
        self.assertFalse((source / "client/node_modules").exists())
        self.assertFalse((source / "client/.nuxt").exists())

    def test_source_version_mismatch_is_rejected_before_any_install_script(self):
        source = self.root / "source"
        self._source(source, "2.36.0")
        with patch.object(installer, "_run") as run:
            with self.assertRaisesRegex(
                installer.AudiobookshelfInstallError, "does not match"
            ):
                installer._build_candidate(source, {}, "2.37.1")
        run.assert_not_called()

    def test_node_major_must_match_supported_runtime(self):
        with patch.object(installer, "_run", return_value="v22.0.0\n"):
            with self.assertRaisesRegex(
                installer.AudiobookshelfInstallError, "Node.js 24"
            ):
                installer._check_node({})

    def test_real_command_timeout_terminates_and_reaps_child(self):
        with self.assertRaisesRegex(installer.AudiobookshelfInstallError, "timed out"):
            installer._run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                self.root,
                os.environ.copy(),
                timeout=0.05,
            )

    def test_cache_namespace_symlink_cannot_redirect_privileged_npm_writes(self):
        first = Path(installer._npm_cache())
        npm_root = first.parent.parent
        saved = npm_root.with_name("saved-npm")
        npm_root.rename(saved)
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        sentinel = unrelated / "preserve"
        sentinel.write_bytes(b"untouched")
        before = sentinel.stat().st_mode
        npm_root.symlink_to(unrelated, target_is_directory=True)
        with self.assertRaisesRegex(
            installer.AudiobookshelfInstallError, "safely prepare"
        ):
            installer._npm_cache()
        self.assertEqual(b"untouched", sentinel.read_bytes())
        self.assertEqual(before, sentinel.stat().st_mode)
        self.assertEqual(["preserve"], [path.name for path in unrelated.iterdir()])

    def test_existing_shared_cache_ownership_and_write_mode_are_repaired(self):
        cache = Path(installer._npm_cache())
        sentinel = cache / "cached-package"
        sentinel.write_bytes(b"cached bytes")
        cache.chmod(0o777)
        if os.geteuid() == 0:
            os.chown(cache, 1000, 1000)
            os.chown(sentinel, 1000, 1000)
        repaired = Path(installer._npm_cache())
        self.assertEqual(cache, repaired)
        self.assertEqual(os.geteuid(), repaired.stat().st_uid)
        self.assertEqual(os.geteuid(), sentinel.stat().st_uid)
        self.assertEqual(0o700, repaired.stat().st_mode & 0o777)
        self.assertEqual(b"cached bytes", sentinel.read_bytes())

    def test_cleanup_failure_keeps_validated_new_runtime_instead_of_partial_backup(
        self,
    ):
        runtime = self._installed("2.36.0")
        self.downloader.download_and_extract.side_effect = self._download

        def partial_cleanup(transaction):
            package = transaction.previous / "package.json"
            package.unlink()
            raise OSError("fixture backup cleanup failure")

        with (
            patch.object(installer, "_check_node"),
            patch.object(installer, "_build_candidate", side_effect=self._build),
            patch.object(
                installer.DirectoryReleaseTransaction, "commit", partial_cleanup
            ),
            patch.object(installer.DirectoryReleaseTransaction, "rollback") as rollback,
        ):
            with self.assertRaisesRegex(
                installer.AudiobookshelfInstallError, "was activated"
            ):
                installer.install_audiobookshelf_runtime(
                    self.config, force=True, downloader=self.downloader
                )
        rollback.assert_not_called()
        self.assertEqual(
            "2.37.1", json.loads((runtime / "package.json").read_text())["version"]
        )
        self.assertTrue(installer.audiobookshelf_runtime_ready(runtime))
        self.assertTrue(list(runtime.parent.glob(".runtime.dumb-previous-*")))

    def test_unrelated_nonempty_runtime_is_preserved_before_cache_or_download(self):
        runtime = Path(self.config["runtime_dir"])
        runtime.mkdir()
        sentinel = runtime / "another-service-data"
        sentinel.write_bytes(b"never replace this directory")
        with (
            patch.object(installer, "_check_node") as node,
            patch.object(installer, "_npm_cache") as cache,
        ):
            with self.assertRaisesRegex(
                installer.AudiobookshelfInstallError, "unrelated"
            ):
                installer.install_audiobookshelf_runtime(
                    self.config, downloader=self.downloader
                )
        node.assert_not_called()
        cache.assert_not_called()
        self.assertEqual([], self.downloader.mock_calls)
        self.assertEqual(b"never replace this directory", sentinel.read_bytes())

    def test_runtime_regular_file_and_symbolic_link_are_refused_without_mutation(self):
        runtime = Path(self.config["runtime_dir"])
        runtime.write_bytes(b"preserve file")
        with self.assertRaisesRegex(
            installer.AudiobookshelfInstallError, "must be a directory"
        ):
            installer.install_audiobookshelf_runtime(
                self.config, downloader=self.downloader
            )
        self.assertEqual(b"preserve file", runtime.read_bytes())
        runtime.unlink()
        target = self._installed()
        link = self.root / "linked-runtime"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(
            installer.AudiobookshelfInstallError, "symbolic link"
        ):
            installer.install_audiobookshelf_runtime(
                {**self.config, "runtime_dir": str(link)}, downloader=self.downloader
            )
        self.assertTrue(link.is_symlink())
        self.assertTrue(installer.audiobookshelf_runtime_ready(target))
        self.assertEqual([], self.downloader.mock_calls)

    def test_existing_runtime_cannot_be_writable_by_or_owned_by_a_service_account(self):
        runtime = self._installed()
        runtime.chmod(0o777)
        with self.assertRaisesRegex(
            installer.AudiobookshelfInstallError, "DUMB controller"
        ):
            installer.install_audiobookshelf_runtime(
                self.config, downloader=self.downloader
            )
        runtime.chmod(0o755)
        controller = os.geteuid()
        with patch.object(installer.os, "geteuid", return_value=controller + 1):
            with self.assertRaisesRegex(
                installer.AudiobookshelfInstallError, "DUMB controller"
            ):
                installer.install_audiobookshelf_runtime(
                    self.config, downloader=self.downloader
                )
        self.assertEqual([], self.downloader.mock_calls)

    def test_invalid_or_writable_ownership_marker_does_not_authorize_replacement(self):
        runtime = self._installed()
        marker = runtime / ".dumb-audiobookshelf.json"
        metadata = json.loads(marker.read_text())
        metadata["commit_sha"] = "invalid"
        marker.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(
            installer.AudiobookshelfInstallError, "unrecognized"
        ):
            installer.install_audiobookshelf_runtime(
                self.config, downloader=self.downloader
            )
        metadata["commit_sha"] = "a" * 40
        marker.write_text(json.dumps(metadata))
        marker.chmod(0o666)
        with self.assertRaisesRegex(
            installer.AudiobookshelfInstallError, "unrecognized"
        ):
            installer.install_audiobookshelf_runtime(
                self.config, downloader=self.downloader
            )
        self.assertEqual([], self.downloader.mock_calls)

    def test_valid_marker_allows_repair_of_a_known_incomplete_runtime(self):
        runtime = self._installed()
        (runtime / "package.json").unlink()
        (runtime / "dist-server/index.js").unlink()
        self.assertFalse(installer.audiobookshelf_runtime_ready(runtime))
        self.downloader.download_and_extract.side_effect = self._download
        with (
            patch.object(installer, "_check_node"),
            patch.object(installer, "_build_candidate", side_effect=self._build),
        ):
            result = installer.install_audiobookshelf_runtime(
                self.config, downloader=self.downloader
            )
        self.assertEqual("2.37.1", result["version"])
        self.assertTrue(installer.audiobookshelf_runtime_ready(runtime))

    def test_present_foreign_package_with_copied_marker_is_not_a_known_runtime(self):
        runtime = self._installed()
        (runtime / "package.json").write_text(
            '{"name":"other-service","version":"2.37.1"}'
        )
        with self.assertRaisesRegex(
            installer.AudiobookshelfInstallError, "unrecognized"
        ):
            installer.install_audiobookshelf_runtime(
                self.config, downloader=self.downloader
            )
        self.assertEqual([], self.downloader.mock_calls)
        self.assertEqual(
            "other-service", json.loads((runtime / "package.json").read_text())["name"]
        )


if __name__ == "__main__":
    unittest.main()
