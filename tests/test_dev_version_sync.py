"""Guarded dev-version metadata publication without real GitHub mutations."""

import contextlib
import copy
import io
import json
from pathlib import Path
import runpy
import tempfile
import tomllib
import unittest
from unittest import mock
import urllib.error

from scripts import sync_dev_version as sync

SOURCE = "a" * 40
STABLE = "b" * 40
NEXT = "c" * 40
CONCURRENT = "d" * 40
TREE = "e" * 40
NEW_TREE = "f" * 40
TOKEN = "fixture-only-token"


def version_plan():
    return {
        "sourceHead": SOURCE,
        "stableSha": STABLE,
        "cutoffSha": STABLE,
        "stableVersion": "2.23.0",
        "stableTag": "2.23.0",
        "candidateVersion": "2.24.0",
        "coreVersion": "2.24.0",
        "displayCount": 37,
    }


class FakeGitHub:
    """Model a non-force ref update, including a competing branch writer."""

    def __init__(self, tip=SOURCE, *, move_on_read=None, race_at_patch=False):
        self.tip = tip
        self.calls = []
        self.reads = 0
        self.move_on_read = move_on_read
        self.race_at_patch = race_at_patch
        self.patch_error = None

    def call(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if (method, path) == ("GET", "/git/ref/heads/dev"):
            self.reads += 1
            if self.reads == self.move_on_read:
                self.tip = CONCURRENT
            return {"object": {"sha": self.tip}}
        if (method, path) == ("GET", "/git/commits/" + SOURCE):
            return {"tree": {"sha": TREE}}
        if (method, path) == ("POST", "/git/trees"):
            return {"sha": NEW_TREE}
        if (method, path) == ("POST", "/git/commits"):
            return {"sha": NEXT}
        if (method, path) == ("PATCH", "/git/refs/heads/dev"):
            if self.race_at_patch:
                self.tip = CONCURRENT
            if self.patch_error:
                raise self.patch_error
            if self.tip != SOURCE:
                raise sync.VersionSyncError("Non-fast-forward update refused.")
            self.tip = payload["sha"]
            return {"object": {"sha": self.tip}}
        raise AssertionError(f"Unexpected fake request: {method} {path}")


class DevVersionSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.plan = version_plan()
        self._write_metadata()
        self.root.joinpath("untouched.txt").write_text("retained\n", encoding="utf-8")

    def _write_metadata(self, source="2.23.0", released="2.24.0"):
        self.root.joinpath("pyproject.toml").write_text(
            '[project]\nname = "DUMB"\n'
            f'version = "{source}" # candidate\n'
            '[tool.example]\nversion = "9.9.9"\n',
            encoding="utf-8",
        )
        self.root.joinpath(".github").mkdir(exist_ok=True)
        self.root.joinpath(sync.TARGETS[1]).write_text(
            json.dumps({".": released}, indent=2) + "\n", encoding="utf-8"
        )

    def _contents(self):
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def _publish(self, api, plan=None):
        with mock.patch.object(sync, "require_checkout") as checkout:
            result = sync.publish_plan(self.root, plan or self.plan, api)
        checkout.assert_called_once_with(self.root, plan or self.plan, clean=True)
        return result

    def test_prepare_is_read_only_and_does_not_construct_an_api(self):
        before = self._contents()
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        with (
            mock.patch.object(sync, "require_checkout") as checkout,
            mock.patch.object(sync, "GitHubVersionAPI") as api,
            io.StringIO() as stdout,
            contextlib.redirect_stdout(stdout),
        ):
            sync.main(["--plan", str(plan_path), "--root", str(self.root)])
            result = json.loads(stdout.getvalue())
        checkout.assert_called_once_with(self.root, self.plan)
        api.assert_not_called()
        self.assertFalse(result["stamped"])
        self.assertEqual(result["version"], "2.24.0")
        self.assertEqual(result["dev_version"], "2.24.0-dev.37")
        after = self._contents()
        after.pop("plan.json")
        self.assertEqual(after, before)

    def test_stamp_is_idempotent_and_separates_api_core_from_display_version(self):
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        output_path = self.root / "github-output"
        argv = [
            "--plan",
            str(plan_path),
            "--root",
            str(self.root),
            "--stamp",
            "--github-output",
            str(output_path),
        ]
        with (
            mock.patch.object(sync, "require_checkout"),
            mock.patch.object(sync, "GitHubVersionAPI") as api,
            mock.patch.dict("os.environ", {"DUMB_VERSION": "9.9.9-dev.999"}),
        ):
            previous = None
            for attempt in range(2):
                with io.StringIO() as stdout, contextlib.redirect_stdout(stdout):
                    sync.main(argv)
                    result = json.loads(stdout.getvalue())
                self.assertTrue(result["stamped"])
                self.assertEqual(result["dev_version"], "2.24.0-dev.37")
                content = {
                    name: (self.root / name).read_bytes() for name in sync.TARGETS
                }
                if attempt:
                    self.assertEqual(content, previous)
                previous = content
            api.assert_not_called()
        project = tomllib.loads(self.root.joinpath("pyproject.toml").read_text())
        self.assertEqual(project["project"]["version"], "2.24.0")
        self.assertEqual(project["tool"]["example"]["version"], "9.9.9")
        self.assertIn("# candidate\n", self.root.joinpath("pyproject.toml").read_text())
        self.assertEqual(
            json.loads(self.root.joinpath(sync.TARGETS[1]).read_text()), {".": "2.23.0"}
        )
        self.assertEqual(self.root.joinpath("untouched.txt").read_text(), "retained\n")
        self.assertIn("dev_version=2.24.0-dev.37\n", output_path.read_text())

    def test_aligned_publication_is_a_noop_on_repeated_runs(self):
        self._write_metadata(source="2.24.0", released="2.23.0")
        before = self._contents()
        api = FakeGitHub()
        for _ in range(2):
            result = self._publish(api)
            self.assertFalse(result["changed"])
            self.assertFalse(result["superseded"])
            self.assertEqual(result["committed_sha"], SOURCE)
        self.assertTrue(all(method == "GET" for method, _, _ in api.calls))
        self.assertEqual(self._contents(), before)

    def test_publication_writes_only_two_paths_and_uses_server_commit_identity(self):
        before = self._contents()
        api = FakeGitHub()
        result = self._publish(api)
        self.assertTrue(result["changed"])
        self.assertEqual(result["source_sha"], SOURCE)
        self.assertEqual(result["committed_sha"], NEXT)
        self.assertEqual(api.tip, NEXT)
        tree = next(
            payload
            for method, path, payload in api.calls
            if (method, path) == ("POST", "/git/trees")
        )
        self.assertEqual(tree["base_tree"], TREE)
        self.assertEqual({item["path"] for item in tree["tree"]}, set(sync.TARGETS))
        self.assertTrue(
            all(
                item["mode"] == "100644" and item["type"] == "blob"
                for item in tree["tree"]
            )
        )
        contents = {item["path"]: item["content"] for item in tree["tree"]}
        self.assertEqual(
            tomllib.loads(contents["pyproject.toml"])["project"]["version"], "2.24.0"
        )
        self.assertEqual(json.loads(contents[sync.TARGETS[1]]), {".": "2.23.0"})
        commit = next(
            payload
            for method, path, payload in api.calls
            if (method, path) == ("POST", "/git/commits")
        )
        self.assertEqual(set(commit), {"message", "tree", "parents"})
        self.assertEqual(commit["parents"], [SOURCE])
        self.assertEqual(commit["tree"], NEW_TREE)
        self.assertIn("chore(dev): track next release 2.24.0", commit["message"])
        patch = next(payload for method, _, payload in api.calls if method == "PATCH")
        self.assertEqual(patch, {"sha": NEXT, "force": False})
        self.assertEqual(
            self._contents(), before, "Publishing never rewrites the local checkout"
        )
        again = self._publish(api)
        self.assertTrue(again["superseded"])
        self.assertFalse(again["changed"])
        self.assertEqual(sum(method == "PATCH" for method, _, _ in api.calls), 1)

    def test_stale_head_returns_before_creating_any_git_objects(self):
        api = FakeGitHub(tip=CONCURRENT)
        result = self._publish(api)
        self.assertTrue(result["superseded"])
        self.assertFalse(result["changed"])
        self.assertEqual(api.calls, [("GET", "/git/ref/heads/dev", None)])
        self.assertEqual(api.tip, CONCURRENT)

    def test_concurrent_push_after_commit_creation_prevents_ref_update(self):
        api = FakeGitHub(move_on_read=2)
        result = self._publish(api)
        self.assertTrue(result["superseded"])
        self.assertFalse(result["changed"])
        self.assertTrue(any(path == "/git/commits" for _, path, _ in api.calls))
        self.assertFalse(any(method == "PATCH" for method, _, _ in api.calls))
        self.assertEqual(api.tip, CONCURRENT)

    def test_race_at_ref_update_cannot_overwrite_the_concurrent_push(self):
        api = FakeGitHub(race_at_patch=True)
        result = self._publish(api)
        self.assertTrue(result["superseded"])
        self.assertFalse(result["changed"])
        self.assertEqual(api.tip, CONCURRENT)
        self.assertEqual(
            [payload for method, _, payload in api.calls if method == "PATCH"],
            [{"sha": NEXT, "force": False}],
        )

    def test_ref_failure_with_unchanged_head_is_not_reported_as_success(self):
        api = FakeGitHub()
        api.patch_error = sync.VersionSyncError("Branch rules denied the update.")
        with self.assertRaisesRegex(sync.VersionSyncError, "Branch rules"):
            self._publish(api)
        self.assertEqual(api.tip, SOURCE)

    def test_strict_plan_validation_precedes_checkout_and_network_calls(self):
        invalid = [None, [], {}]
        for key in ("sourceHead", "stableSha", "cutoffSha"):
            for value in (None, 12, "a" * 39, "A" * 40, "a" * 40 + "\n"):
                invalid.append({**self.plan, key: value})
        for key in ("stableVersion", "candidateVersion"):
            for value in (
                None,
                2.24,
                "2.24",
                "02.24.0",
                "2.24.0-dev.1",
                "2.24.0+build",
            ):
                invalid.append({**self.plan, key: value})
        for patch in (
            {"candidateVersion": "2.23.0", "coreVersion": "2.23.0"},
            {"candidateVersion": "2.22.9", "coreVersion": "2.22.9"},
            {"coreVersion": "2.24.0-dev.37"},
            {"stableTag": "2.22.0"},
            {"displayCount": True},
            {"displayCount": "37"},
            {"displayCount": 3.7},
            {"displayCount": -1},
        ):
            invalid.append({**self.plan, **patch})
        for plan in invalid:
            with self.subTest(plan=plan):
                api = FakeGitHub()
                with mock.patch.object(sync, "require_checkout") as checkout:
                    with self.assertRaises(sync.VersionSyncError):
                        sync.publish_plan(self.root, plan, api)
                checkout.assert_not_called()
                self.assertEqual(api.calls, [])
        self.assertEqual(
            sync.validate_plan({**self.plan, "stableTag": "v2.23.0"}),
            {**self.plan, "stableTag": "v2.23.0"},
        )

    def test_mismatched_or_dirty_checkout_is_rejected(self):
        with mock.patch.object(
            sync.subprocess, "check_output", return_value=CONCURRENT
        ):
            with self.assertRaisesRegex(sync.VersionSyncError, "calculated head"):
                sync.require_checkout(self.root, self.plan, clean=True)
        with mock.patch.object(
            sync.subprocess, "check_output", side_effect=[SOURCE, " M pyproject.toml\n"]
        ):
            with self.assertRaisesRegex(sync.VersionSyncError, "modified checkout"):
                sync.require_checkout(self.root, self.plan, clean=True)

    def test_image_stamp_preserves_every_other_byte_without_git_or_manifest(self):
        manifest = self.root / sync.TARGETS[1]
        manifest.unlink()
        original = (
            b'[project]\r\nname = "DUMB"\r\n'
            b'version = "2.23.0" # candidate\r\n'
            b'[tool.example]\r\nversion = "9.9.9"\r\n'
        )
        self.root.joinpath("pyproject.toml").write_bytes(original)
        before = self._contents()
        with (
            mock.patch.object(
                sync,
                "require_checkout",
                side_effect=AssertionError("No Git checkout allowed"),
            ),
            mock.patch.object(
                sync.subprocess,
                "check_output",
                side_effect=AssertionError("No Git process allowed"),
            ),
            mock.patch.object(
                sync, "GitHubVersionAPI", side_effect=AssertionError("No API allowed")
            ),
        ):
            for _ in range(2):
                self.assertEqual(
                    sync.stamp_image_version(self.root, "2.24.0"),
                    {"version": "2.24.0", "stamped": True},
                )
                after = self._contents()
                self.assertEqual(set(after), set(before))
                self.assertEqual(
                    after["pyproject.toml"], original.replace(b"2.23.0", b"2.24.0", 1)
                )
                self.assertEqual(after["untouched.txt"], before["untouched.txt"])
        self.assertFalse(manifest.exists())

    def test_image_stamp_requires_strict_stable_core_before_any_write(self):
        before = self._contents()
        for candidate in (
            None,
            True,
            2.24,
            "",
            "2.24",
            "v2.24.0",
            "02.24.0",
            "2.024.0",
            "2.24.00",
            "2.24.0-dev.37",
            "2.24.0+build",
            "2.24.0\n",
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(sync.VersionSyncError):
                    sync.stamp_image_version(self.root, candidate)
                self.assertEqual(self._contents(), before)

    def test_image_cli_needs_no_plan_and_never_touches_git_network_or_manifest(self):
        manifest = self.root.joinpath(sync.TARGETS[1]).read_bytes()
        with (
            mock.patch.object(
                sync,
                "require_checkout",
                side_effect=AssertionError("No Git checkout allowed"),
            ),
            mock.patch.object(
                sync.subprocess,
                "check_output",
                side_effect=AssertionError("No Git process allowed"),
            ),
            mock.patch.object(
                sync, "GitHubVersionAPI", side_effect=AssertionError("No API allowed")
            ),
            io.StringIO() as stdout,
            contextlib.redirect_stdout(stdout),
        ):
            sync.main(
                ["--root", str(self.root), "--stamp-image", "--version", "2.24.0"]
            )
            self.assertEqual(
                json.loads(stdout.getvalue()), {"version": "2.24.0", "stamped": True}
            )
        self.assertEqual(self.root.joinpath(sync.TARGETS[1]).read_bytes(), manifest)
        project = tomllib.loads(self.root.joinpath("pyproject.toml").read_text())
        self.assertEqual(project["project"]["version"], "2.24.0")
        self.assertNotIn("-dev.", project["project"]["version"])

    def test_image_cli_rejects_missing_version_or_release_plan(self):
        before = self._contents()
        for extra in (
            [],
            ["--plan", str(self.root / "absent-plan.json"), "--version", "2.24.0"],
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(sync.VersionSyncError):
                    sync.main(["--root", str(self.root), "--stamp-image", *extra])
                self.assertEqual(self._contents(), before)

    def test_metadata_symlinks_and_multi_package_manifests_are_rejected(self):
        manifest = self.root / sync.TARGETS[1]
        manifest.write_text(json.dumps({".": "2.23.0", "other": "1.0.0"}))
        with self.assertRaisesRegex(sync.VersionSyncError, "single root package"):
            sync.prepared_files(self.root, self.plan)
        manifest.unlink()
        manifest.symlink_to(self.root / "untouched.txt")
        with self.assertRaisesRegex(sync.VersionSyncError, "regular files"):
            sync.prepared_files(self.root, self.plan)


class GitHubVersionTransportTests(unittest.TestCase):
    def test_missing_authentication_and_invalid_repository_fail_locally(self):
        for repository, token in (("example/dumb", ""), ("../example/dumb", TOKEN)):
            with self.subTest(repository=repository):
                with self.assertRaises(sync.VersionSyncError) as error:
                    sync.GitHubVersionAPI(repository, token)
                self.assertNotIn(TOKEN, str(error.exception))

    def test_api_errors_omit_transport_details_and_credentials(self):
        api = sync.GitHubVersionAPI("example/dumb", TOKEN)
        for error in (
            urllib.error.HTTPError("https://api.github.com/", 401, TOKEN, {}, None),
            urllib.error.HTTPError("https://api.github.com/", 403, TOKEN, {}, None),
            urllib.error.HTTPError("https://api.github.com/", 409, TOKEN, {}, None),
            urllib.error.HTTPError("https://api.github.com/", 422, TOKEN, {}, None),
            urllib.error.URLError(TOKEN),
        ):
            with self.subTest(
                error=type(error).__name__, code=getattr(error, "code", None)
            ):
                opener = mock.Mock()
                opener.open.side_effect = error
                with mock.patch.object(
                    sync.urllib.request, "build_opener", return_value=opener
                ):
                    with self.assertRaises(sync.VersionSyncError) as raised:
                        api.call("GET", "/git/ref/heads/dev")
                self.assertNotIn(TOKEN, str(raised.exception))

    def test_transport_stays_on_fixed_origin_and_refuses_redirects(self):
        api = sync.GitHubVersionAPI("example/dumb", TOKEN)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'{"object":{"sha":"' + SOURCE.encode() + b'"}}'
        )
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(
            sync.urllib.request, "build_opener", return_value=opener
        ) as build:
            self.assertEqual(
                api.call("GET", "/git/ref/heads/dev")["object"]["sha"], SOURCE
            )
        request = opener.open.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/example/dumb/git/ref/heads/dev",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        response.__enter__.return_value.read.assert_called_once_with(sync.MAX_BYTES + 1)
        handler = build.call_args.args[0]
        self.assertIsNone(
            handler.redirect_request(
                request, None, 302, "redirect", {}, "https://example.invalid/"
            )
        )

    def test_cli_failure_does_not_print_api_reason_or_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath(".github").mkdir()
            root.joinpath("pyproject.toml").write_text('[project]\nversion="2.23.0"\n')
            root.joinpath(sync.TARGETS[1]).write_text('{".":"2.23.0"}')
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(version_plan()))
            opener = mock.Mock()
            opener.open.side_effect = urllib.error.HTTPError(
                "https://api.github.com/", 403, TOKEN, {}, None
            )
            with (
                mock.patch(
                    "sys.argv",
                    [
                        str(Path(sync.__file__)),
                        "--plan",
                        str(plan_path),
                        "--root",
                        str(root),
                        "--publish",
                    ],
                ),
                mock.patch.dict(
                    "os.environ",
                    {"GITHUB_REPOSITORY": "example/dumb", "DEV_VERSION_TOKEN": TOKEN},
                    clear=True,
                ),
                mock.patch.object(
                    sync.subprocess, "check_output", side_effect=[SOURCE, ""]
                ),
                mock.patch.object(
                    sync.urllib.request, "build_opener", return_value=opener
                ),
                io.StringIO() as stdout,
                io.StringIO() as stderr,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                with self.assertRaises(SystemExit) as raised:
                    runpy.run_path(sync.__file__, run_name="__main__")
                self.assertEqual(raised.exception.code, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotIn(TOKEN, stderr.getvalue())
                self.assertIn("Dev version synchronization failed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
