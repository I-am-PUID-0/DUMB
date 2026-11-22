"""Active native updates remain separate from pinned migration qualification."""

from contextlib import contextmanager
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from utils import infinidysk_v2_releases as releases
from utils import infinidysk_v2_runtime as runtime


class Response(io.BytesIO):
    def __init__(
        self, content, url="https://release-assets.githubusercontent.com/example"
    ):
        super().__init__(content)
        self.url = url

    def geturl(self):
        return self.url


class NativeReleaseTests(unittest.TestCase):
    def setUp(self):
        config_loader = importlib.import_module("utils.config_loader")

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o755)
        self.service = {
            "runtime_generation": 2,
            "release_version": "latest",
            "release_version_enabled": False,
            "v2_runtime_dir": str(self.root / "program"),
            "v2_data_dir": str(self.root / "data"),
            "config_dir": str(self.root / "legacy"),
            "mount_backend": "rclone",
            "env": {},
        }
        self.tag = "v2.0.0-beta.10"
        self.commit = "a" * 40
        self.uid, self.gid = max(1, os.geteuid()), max(1, os.getegid())
        values = {"data_root": str(self.root), "puid": self.uid, "pgid": self.gid}
        manager = SimpleNamespace(
            file_path=str(self.root / "dumb_config.json"),
            get=lambda key, section=None: values.get(key),
        )
        # Old suites replace CONFIG_MANAGER globally with partial namespaces.
        # This adapter always requires the real persisted-config path contract.
        scope = patch.object(config_loader, "CONFIG_MANAGER", manager)
        scope.start()
        self.addCleanup(scope.stop)

    def archive(self, *, extra=None, version=None, commit=None):
        output = io.BytesIO()
        prefix = f"infinidysk-{self.tag}-linux-x64"
        files = {
            "infinidysk": b"official-fixture-binary",
            "LICENSE": b"fixture license",
            "version.txt": (version or self.tag).encode(),
            "commit.txt": (commit or self.commit).encode(),
        }
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for name, data in files.items():
                member = tarfile.TarInfo(f"{prefix}/{name}")
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            if extra is not None:
                archive.addfile(extra)
        return output.getvalue()

    def plan(self, content=None):
        content = content or self.archive()
        name = f"infinidysk-{self.tag}-linux-x64.tar.gz"
        return {
            "release": self.tag,
            "commit": self.commit,
            "architecture": "x64",
            "asset_name": name,
            "archive_url": f"{releases.DOWNLOAD}/{self.tag}/{name}",
            "archive_sha256": hashlib.sha256(content).hexdigest(),
            "archive_size": len(content),
        }

    def release(self, tag=None, **kwargs):
        plan = self.plan()
        tag = tag or self.tag
        name = f"infinidysk-{tag}-linux-x64.tar.gz"
        return {
            "tag_name": tag,
            "draft": False,
            "prerelease": "-" in tag,
            "target_commitish": self.commit,
            "assets": [
                {
                    "name": name,
                    "size": plan["archive_size"],
                    "digest": "sha256:" + plan["archive_sha256"],
                    "browser_download_url": f"{releases.DOWNLOAD}/{tag}/{name}",
                }
            ],
            **kwargs,
        }

    @contextmanager
    def machine(self):
        with (
            patch.object(releases.platform, "machine", return_value="x86_64"),
            patch.object(releases.platform, "system", return_value="Linux"),
            patch.object(releases.platform, "libc_ver", return_value=("glibc", "2.43")),
        ):
            yield

    @contextmanager
    def staged(self, content=None, *, plan=None, paths=None):
        content = content or self.archive()
        with (
            self.machine(),
            patch.object(
                releases.urllib.request,
                "urlopen",
                side_effect=lambda *args, **kwargs: Response(content),
            ),
            patch.object(
                releases.subprocess,
                "run",
                return_value=SimpleNamespace(
                    stdout=json.dumps(
                        {
                            "paths": (
                                paths
                                if paths is not None
                                else {
                                    "/api/v1/upgrade/status": {},
                                    "/api/v1/catalog": {},
                                }
                            )
                        }
                    ).encode()
                ),
            ),
        ):
            with runtime.stage_v2_runtime_update(
                self.service, plan or self.plan(content), uid=self.uid, gid=self.gid
            ) as value:
                yield value

    def test_latest_v2_is_semantic_and_excludes_drafts_and_other_major_versions(self):
        batch = [
            self.release("v3.0.0"),
            self.release("v2.0.0-beta.99", draft=True),
            self.release("v1.99.0"),
            self.release("v2.0.0-beta.9"),
            self.release(),
        ]
        with (
            self.machine(),
            patch.object(
                releases,
                "_json",
                side_effect=[
                    batch,
                    {
                        "ref": "refs/tags/" + self.tag,
                        "object": {"type": "commit", "sha": self.commit},
                    },
                ],
            ) as api,
        ):
            self.assertEqual(runtime.resolve_v2_release(self.service), self.plan())
        self.assertEqual(api.call_args_list[0].args[0], "/releases?per_page=100&page=1")

    def test_explicit_release_supports_annotated_tags(self):
        self.service["release_version"] = self.tag.lstrip("v")
        self.service["release_version_enabled"] = True
        object_id = "b" * 40
        with (
            self.machine(),
            patch.object(
                releases,
                "_json",
                side_effect=[
                    self.release(),
                    {
                        "ref": "refs/tags/" + self.tag,
                        "object": {"type": "tag", "sha": object_id},
                    },
                    {
                        "sha": object_id,
                        "object": {"type": "commit", "sha": self.commit},
                    },
                ],
            ),
        ):
            self.assertEqual(
                runtime.resolve_v2_release(self.service)["commit"], self.commit
            )

    def test_standard_release_controls_honor_disabled_and_enabled_selectors(self):
        for enabled, selected, expected in (
            (False, "rc", "latest"),
            (False, self.tag, "latest"),
            (True, "latest", "latest"),
            (True, "", "latest"),
            (True, self.tag.lstrip("v"), self.tag),
        ):
            with self.subTest(enabled=enabled, selected=selected):
                service = {
                    **self.service,
                    "release_version": selected,
                    "release_version_enabled": enabled,
                }
                self.assertEqual(releases.selected_release(service), expected)
                self.assertTrue(runtime.validate_active_v2_selection(service)[0])

    def test_raw_legacy_alias_remains_readable_without_changing_the_config(self):
        service = {
            **self.service,
            "v2_release_version": self.tag,
            "release_version": "rc",
            "release_version_enabled": True,
        }
        original = dict(service)
        self.assertEqual(releases.selected_release(service), self.tag)
        self.assertEqual(service, original)

    def test_semver_comparison_refuses_downgrade_and_ignores_build_metadata(self):
        self.assertEqual(
            runtime.compare_v2_releases("v2.0.0-beta.10", "v2.0.0-beta.9"), 1
        )
        self.assertEqual(runtime.compare_v2_releases("v2.0.0-beta.10", "v2.0.0"), -1)
        self.assertEqual(
            runtime.compare_v2_releases("v2.1.0+build.1", "2.1.0+build.2"), 0
        )
        with self.assertRaises(releases.NativeReleaseError):
            runtime.compare_v2_releases("v3.0.0", self.tag)

    def test_fresh_startup_resolves_latest_and_records_verified_selected_archive(self):
        from utils.infinidysk_v2_installed_release import read_installed_release

        with (
            self.machine(),
            patch.object(
                releases,
                "_json",
                side_effect=[
                    [self.release()],
                    {
                        "ref": "refs/tags/" + self.tag,
                        "object": {"type": "commit", "sha": self.commit},
                    },
                ],
            ),
            patch.object(
                releases.urllib.request,
                "urlopen",
                return_value=Response(self.archive()),
            ),
            patch.object(
                releases.subprocess,
                "run",
                return_value=SimpleNamespace(
                    stdout=json.dumps(
                        {"paths": {"/api/v1/upgrade/status": {}, "/api/v1/catalog": {}}}
                    ).encode()
                ),
            ),
            patch.object(runtime, "install_v2_runtime") as importer,
        ):
            self.assertEqual(
                runtime.ensure_active_v2_runtime(
                    SimpleNamespace(logger=Mock()), "InfiniDysk", self.service
                ),
                (True, None),
            )
            self.assertEqual(
                runtime.installed_active_v2_version(self.service), self.tag
            )
        self.assertEqual(
            read_installed_release(
                self.service, self.root / "updates" / "infinidysk-v2"
            )["release"],
            self.tag,
        )
        self.assertFalse(Path(self.service["v2_data_dir"]).exists())
        importer.assert_not_called()

    def test_invalid_selectors_forks_or_source_builds_never_query_github(self):
        for options in (
            {"release_version": "v3.0.0", "release_version_enabled": True},
            {"release_version": "v2.00.0", "release_version_enabled": True},
            {"release_version": "v2.0.0-beta.01", "release_version_enabled": True},
            {"release_version": 0, "release_version_enabled": True},
            {"release_version": "../../main", "release_version_enabled": True},
            {"release_version_enabled": "true"},
            {"branch_enabled": True},
            {"commit_sha": "a" * 40},
            {"repo_owner": "fork"},
        ):
            with self.subTest(options=options), patch.object(releases, "_json") as api:
                service = {**self.service, **options}
                self.assertFalse(runtime.validate_active_v2_selection(service)[0])
                with self.assertRaises(releases.NativeReleaseError):
                    runtime.resolve_v2_release(service)
                api.assert_not_called()
        self.assertTrue(runtime.validate_active_v2_selection(self.service)[0])
        self.assertTrue(runtime.validate_v2_selection(self.service)[0])

    def test_missing_digest_bad_asset_or_tag_mismatch_is_refused(self):
        for change in ("digest", "url", "size", "duplicate", "tag", "commit"):
            with self.subTest(change=change):
                metadata = self.release()
                if change == "digest":
                    metadata["assets"][0]["digest"] = None
                if change == "url":
                    metadata["assets"][0][
                        "browser_download_url"
                    ] = "https://example.invalid/archive.tar.gz"
                if change == "size":
                    metadata["assets"][0]["size"] = releases.MAX_ARCHIVE + 1
                if change == "duplicate":
                    metadata["assets"] *= 2
                if change == "tag":
                    metadata["tag_name"] = "v2.0.0-beta.9"
                if change == "commit":
                    metadata["target_commitish"] = "b" * 40
                with (
                    self.machine(),
                    patch.object(
                        releases,
                        "_json",
                        side_effect=[
                            metadata,
                            {
                                "ref": "refs/tags/" + self.tag,
                                "object": {"type": "commit", "sha": self.commit},
                            },
                        ],
                    ),
                ):
                    with self.assertRaises(releases.NativeReleaseError):
                        runtime.resolve_v2_release(
                            {**self.service, "v2_release_version": self.tag}
                        )

    def test_staging_is_verified_offline_launchable_and_keeps_migration_pin(self):
        program = Path(self.service["v2_runtime_dir"])
        program.mkdir()
        (program / "existing").write_bytes(b"preserve existing runtime")
        with self.staged() as (candidate, metadata):
            staged_service = {**self.service, "v2_runtime_dir": str(candidate)}
            self.assertEqual(
                runtime.installed_active_v2_version(staged_service), self.tag
            )
            self.assertIsNone(runtime.installed_v2_version(staged_service))
            self.assertEqual(metadata["format"], releases.FORMAT)
            with patch.object(
                releases, "_json", side_effect=AssertionError("startup must be offline")
            ):
                self.assertTrue(
                    runtime.prepare_v2_launch(staged_service, self.uid, self.gid)[0]
                )
                with patch.object(runtime, "install_v2_runtime") as install:
                    self.assertEqual(
                        runtime.ensure_active_v2_runtime(
                            None, "InfiniDysk", staged_service
                        ),
                        (True, None),
                    )
                    install.assert_not_called()
            self.assertEqual(
                (program / "existing").read_bytes(), b"preserve existing runtime"
            )
            self.assertFalse(Path(self.service["config_dir"]).exists())
        self.assertFalse(candidate.exists())
        self.assertEqual(
            (program / "existing").read_bytes(), b"preserve existing runtime"
        )
        self.assertEqual(runtime.RELEASE, "v2.0.0-beta.6")

    def test_provenance_detects_binary_changes_and_links(self):
        with self.staged() as (candidate, _):
            staged = {**self.service, "v2_runtime_dir": str(candidate)}
            binary = candidate / "infinidysk"
            original = binary.read_bytes()
            binary.write_bytes(b"changed")
            self.assertIsNone(runtime.installed_active_v2_version(staged))
            binary.write_bytes(original)
            binary.chmod(0o755)
            saved = candidate.parent / "binary"
            binary.rename(saved)
            binary.symlink_to(saved)
            self.assertIsNone(runtime.installed_active_v2_version(staged))

    def test_archive_digest_size_commit_and_layout_fail_before_publication(self):
        link = tarfile.TarInfo("escape")
        link.type = tarfile.SYMTYPE
        link.linkname = str(self.root / "foreign")
        duplicate = tarfile.TarInfo(f"infinidysk-{self.tag}-linux-x64/LICENSE")
        cases = [
            (self.archive(), {"archive_sha256": "0" * 64}),
            (self.archive(), {"archive_size": 1}),
            (self.archive(commit="b" * 40), {}),
            (self.archive(version="v2.0.0-beta.9"), {}),
            (self.archive(version="vv2.0.0-beta.10"), {}),
            (self.archive(extra=link), {}),
            (self.archive(extra=duplicate), {}),
        ]
        for content, changes in cases:
            with self.subTest(
                changes=changes, content=hashlib.sha256(content).hexdigest()
            ):
                with self.assertRaises(releases.NativeReleaseError):
                    with self.staged(content, plan={**self.plan(content), **changes}):
                        self.fail("invalid archive was yielded")
                self.assertFalse(Path(self.service["v2_runtime_dir"]).exists())

    def test_missing_api_or_callback_failure_keeps_prior_runtime(self):
        with self.assertRaises(releases.NativeReleaseError):
            with self.staged(paths={"/api/v1/upgrade/status": {}}):
                self.fail("incompatible runtime was yielded")
        failure = ValueError("lifecycle must preserve its own failure")
        with self.assertRaises(ValueError) as caught:
            with self.staged():
                raise failure
        self.assertIs(caught.exception, failure)

    def test_legacy_pinned_runtime_remains_recognized(self):
        program = Path(self.service["v2_runtime_dir"])
        program.mkdir()
        binary = program / "infinidysk"
        binary.write_bytes(b"old pinned fixture")
        binary.chmod(0o755)
        (program / "version.txt").write_text(runtime.RELEASE)
        (program / "commit.txt").write_text(runtime.COMMIT)
        (program / runtime.MARKER).write_text(
            json.dumps(
                {
                    "commit": runtime.COMMIT,
                    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                }
            )
        )
        self.assertEqual(
            runtime.installed_active_v2_version(self.service), runtime.RELEASE
        )
        self.assertEqual(runtime.installed_v2_version(self.service), runtime.RELEASE)

    def test_unexpected_download_redirect_is_refused(self):
        for url in (
            "http://release-assets.githubusercontent.com/archive",
            "https://example.invalid/archive",
        ):
            with (
                self.subTest(url=url),
                patch.object(
                    releases.urllib.request,
                    "urlopen",
                    return_value=Response(self.archive(), url),
                ),
            ):
                with self.assertRaises(releases.NativeReleaseError):
                    releases._download(self.plan(), self.root / "untrusted.tar.gz")
                (self.root / "untrusted.tar.gz").unlink()
