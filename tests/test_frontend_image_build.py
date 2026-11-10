import base64
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "frontend_image_resolver", ROOT / "scripts/resolve_frontend_build.py"
)
resolver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolver)
SHA = "a" * 40
TAG_SHA = "b" * 40


def package_response(version="1.91.0"):
    return {
        "type": "file",
        "encoding": "base64",
        "content": base64.b64encode(json.dumps({"version": version}).encode()).decode(),
    }


class FrontendBuildResolverTests(unittest.TestCase):
    def resolve(self, source, responses, commit="", ref="refs/heads/dev", event=None):
        calls = []

        def api(endpoint):
            calls.append(endpoint)
            result = responses[endpoint]
            if isinstance(result, Exception):
                raise result
            return result

        result = resolver.resolve_frontend_build(
            source, commit, ref, event or "workflow_dispatch", api
        )
        return result, calls

    def test_latest_release_peels_annotated_tag_then_reads_exact_commit_package(self):
        result, calls = self.resolve(
            "Latest release",
            {
                "releases/latest": {"tag_name": "v1.91.0"},
                "git/ref/tags/v1.91.0": {"object": {"type": "tag", "sha": TAG_SHA}},
                "git/tags/" + TAG_SHA: {"object": {"type": "commit", "sha": SHA}},
                "contents/package.json?ref=" + SHA: package_response(),
            },
            ref="refs/heads/master",
            event="pull_request",
        )
        self.assertEqual(result["DUMB_FRONTEND_SHA"], SHA)
        self.assertEqual(result["DUMB_FRONTEND_REF"], "v1.91.0")
        self.assertEqual(result["DUMB_FRONTEND_SOURCE_KIND"], "release")
        self.assertNotIn("DUMB_FRONTEND_IMAGE_TAG", result)
        self.assertEqual(calls[-1], "contents/package.json?ref=" + SHA)

    def test_dev_reads_branch_ref_once_and_pins_the_package_lookup(self):
        result, calls = self.resolve(
            "dmbdb dev",
            {
                "git/ref/heads/dev": {"object": {"type": "commit", "sha": SHA}},
                "contents/package.json?ref=" + SHA: package_response(),
            },
        )
        self.assertEqual(
            calls, ["git/ref/heads/dev", "contents/package.json?ref=" + SHA]
        )
        self.assertEqual(result["DUMB_FRONTEND_SOURCE_KIND"], "branch")
        self.assertEqual(result["DUMB_FRONTEND_REF"], "dev")
        self.assertNotIn("DUMB_FRONTEND_IMAGE_TAG", result)

    def test_exact_commit_verifies_identity_without_changing_image_channel(self):
        result, _ = self.resolve(
            "Exact commit",
            {
                "commits/" + SHA: {"sha": SHA},
                "contents/package.json?ref=" + SHA: package_response(),
            },
            commit=SHA.upper(),
        )
        self.assertEqual(result["DUMB_FRONTEND_SOURCE_KIND"], "commit")
        self.assertEqual(result["DUMB_FRONTEND_REF"], SHA)
        self.assertNotIn("DUMB_FRONTEND_IMAGE_TAG", result)

    def test_invalid_selection_never_reaches_github(self):
        selections = [
            ("unexpected", "", "refs/heads/dev", "workflow_dispatch"),
            ("Exact commit", "a" * 12, "refs/heads/dev", "workflow_dispatch"),
            ("Exact commit", "../dev", "refs/heads/dev", "workflow_dispatch"),
            ("Latest release", SHA, "refs/heads/dev", "workflow_dispatch"),
            ("dmbdb dev", SHA, "refs/heads/dev", "workflow_dispatch"),
        ]
        for mode in ("dmbdb dev", "Exact commit"):
            for ref in ("refs/heads/master", "refs/tags/dev", "refs/heads/feature"):
                selections.append(
                    (
                        mode,
                        SHA if mode == "Exact commit" else "",
                        ref,
                        "workflow_dispatch",
                    )
                )
            selections.append(
                (mode, SHA if mode == "Exact commit" else "", "refs/heads/dev", "push")
            )
        for selection in selections:
            with self.subTest(selection=selection):
                api = mock.Mock()
                with self.assertRaises(ValueError):
                    resolver.resolve_frontend_build(*selection, api)
                api.assert_not_called()

    def test_missing_selected_source_does_not_fall_back_to_another_channel(self):
        for mode, endpoint, commit in [
            ("Latest release", "releases/latest", ""),
            ("dmbdb dev", "git/ref/heads/dev", ""),
            ("Exact commit", "commits/" + SHA, SHA),
        ]:
            with self.subTest(mode=mode):
                api = mock.Mock(side_effect=ValueError("selected source unavailable"))
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    resolver.resolve_frontend_build(
                        mode, commit, "refs/heads/dev", "workflow_dispatch", api
                    )
                api.assert_called_once_with(endpoint)

    def test_mismatched_commit_and_tag_cycles_fail_before_source_build(self):
        with self.assertRaisesRegex(ValueError, "different"):
            self.resolve(
                "Exact commit", {"commits/" + SHA: {"sha": TAG_SHA}}, commit=SHA
            )
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.resolve(
                "Latest release",
                {
                    "releases/latest": {"tag_name": "v1.91.0"},
                    "git/ref/tags/v1.91.0": {"object": {"type": "tag", "sha": TAG_SHA}},
                    "git/tags/" + TAG_SHA: {"object": {"type": "tag", "sha": TAG_SHA}},
                },
            )

    def test_metadata_cannot_inject_output_lines_or_summary_markup(self):
        for version in (
            "1.91.0\nOTHER=value",
            "1.91.0`",
            "<script>",
            "",
            "01.91.0",
            "1.91.0+x+y",
        ):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.resolve(
                    "dmbdb dev",
                    {
                        "git/ref/heads/dev": {"object": {"type": "commit", "sha": SHA}},
                        "contents/package.json?ref=" + SHA: package_response(version),
                    },
                )
        for tag in ("tag\nOTHER=value", "$(example)", "tag`example`", ""):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                self.resolve("Latest release", {"releases/latest": {"tag_name": tag}})

    def test_cli_validation_and_output_use_the_same_guarded_resolver(self):
        env = {
            "FRONTEND_SOURCE_INPUT": "dmbdb dev",
            "GITHUB_REF": "refs/heads/dev",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
        }
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(resolver.sys, "argv", ["resolver", "--validate-only"]),
            mock.patch.object(resolver, "GitHubAPI") as client,
        ):
            self.assertEqual(resolver.main(), 0)
            client.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "github-output")
            env["GITHUB_OUTPUT"] = str(output)
            response = {
                "git/ref/heads/dev": {"object": {"type": "commit", "sha": SHA}},
                "contents/package.json?ref=" + SHA: package_response(),
            }
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(resolver.sys, "argv", ["resolver"]),
                mock.patch.object(
                    resolver, "GitHubAPI", return_value=response.__getitem__
                ),
            ):
                self.assertEqual(resolver.main(), 0)
            rows = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(rows["DUMB_FRONTEND_SHA"], SHA)
            self.assertEqual(len(rows), 5)

    def test_github_redirect_refuses_to_forward_workflow_credentials(self):
        handler = resolver.NoRedirect()
        request = resolver.urllib.request.Request(
            resolver.API_ROOT, headers={"Authorization": "Bearer fixture-only"}
        )
        with self.assertRaisesRegex(ValueError, "redirected"):
            handler.redirect_request(
                request, None, 302, "Found", {}, "https://reserved.example/"
            )


class FrontendImageWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(
            (ROOT / ".github/workflows/docker-image.yml").read_text()
        )

    def test_frontend_selection_keeps_dev_tag_and_preserves_release_tags(self):
        job = self.workflow["jobs"]["build-and-push"]
        script = next(
            step["run"]
            for step in job["steps"]
            if step.get("name") == "Determine Docker tags"
        )
        self.assertNotIn("FRONTEND_IMAGE_TAG", job["env"])
        for branch, source, expected, latest in [
            ("dev", "release", "dev", ""),
            ("dev", "dev", "dev", ""),
            ("dev", "commit", "dev", ""),
            ("master", "release", "2.24.0", "latest"),
        ]:
            with (
                self.subTest(branch=branch, source=source),
                tempfile.TemporaryDirectory() as directory,
            ):
                output = Path(directory, "env")
                env = {
                    **os.environ,
                    "GITHUB_REF_NAME": branch,
                    "BRANCH_NAME": branch,
                    "FRONTEND_SOURCE": source,
                    # An obsolete override cannot divert the normal dev tag.
                    "FRONTEND_IMAGE_TAG": "dev-frontend-dev",
                    "GITHUB_ENV": str(output),
                    "DOCKER_USER": "example",
                    "REPO_NAME": "dumb",
                    "REPO_OWNER_LOWER": "example",
                    "VERSION": "2.24.0",
                }
                subprocess.run(
                    ["bash", "-e", "-c", script],
                    env=env,
                    check=True,
                    timeout=5,
                    capture_output=True,
                )
                values = dict(
                    line.split("=", 1) for line in output.read_text().splitlines()
                )
                self.assertEqual(
                    values["DH_VERSION"], "docker.io/example/dumb:" + expected
                )
                self.assertEqual(
                    values["GH_VERSION"], "ghcr.io/example/dumb:" + expected
                )
                for key, prefix in [
                    ("DH_LATEST", "docker.io"),
                    ("GH_LATEST", "ghcr.io"),
                ]:
                    self.assertEqual(
                        values[key],
                        prefix + "/example/dumb:" + latest if latest else "",
                    )

    def test_both_architectures_receive_one_resolved_frontend_revision(self):
        jobs = self.workflow["jobs"]
        for name in ("build-amd64-digest", "build-arm64-digest"):
            job = jobs[name]
            checkout = next(
                step
                for step in job["steps"]
                if str(step.get("uses", "")).startswith("actions/checkout")
            )
            self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
            build = next(
                step["with"]
                for step in job["steps"]
                if str(step.get("uses", "")).startswith("docker/build-push-action")
            )
            self.assertIn(
                "DUMB_FRONTEND_SHA=${{ needs.fetch-latest-tags.outputs.DUMB_FRONTEND_SHA }}",
                build["build-args"],
            )
            self.assertNotIn("DUMB_FRONTEND_TAG=", build["build-args"])
        self.assertEqual(jobs["announce-dev"]["if"], "github.ref == 'refs/heads/dev'")

    def test_dev_tracking_and_announcement_include_each_frontend_source(self):
        job = self.workflow["jobs"]["announce-dev"]
        self.assertIn("build-and-push", job["needs"])
        self.assertNotIn("always()", job["if"])
        checkout = next(
            step
            for step in job["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout")
        )
        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        announcement = next(
            step
            for step in job["steps"]
            if step.get("name") == "Post dev announcement to Discord"
        )
        line = next(
            line for line in announcement["run"].splitlines() if "$(python -c '" in line
        )
        start = line.index("$(python -c '") + len("$(python -c '")
        renderer = line[start : line.rindex("')")]
        for source, reference in [
            ("release", "v1.91.0"),
            ("dev", "dev"),
            ("commit", SHA),
        ]:
            with self.subTest(source=source):
                env = {
                    **os.environ,
                    "FRONTEND_SOURCE": source,
                    "FRONTEND_REF": reference,
                    "FRONTEND_SHA": SHA,
                    "FRONTEND_PACKAGE_VERSION": "1.91.0",
                    "DEV_VERSION": "2.24.0-dev.34",
                    "BASE_LABEL": "Changes since last dev build",
                    "COMMITS": "- backend change\n" * 300,
                }
                result = subprocess.run(
                    [sys.executable, "-B", "-c", renderer],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=True,
                )
                self.assertIn(source + " / " + reference + " / v1.91.0", result.stdout)
                self.assertIn("Commit: " + SHA, result.stdout)
                self.assertLessEqual(len(result.stdout.rstrip()), 1900)


if __name__ == "__main__":
    unittest.main()
