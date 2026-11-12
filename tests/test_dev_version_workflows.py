"""Regression contracts for dev metadata sync and immutable image version inputs."""

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_workflow(name):
    return yaml.safe_load(ROOT.joinpath(".github", "workflows", name).read_text())


def workflow_events(workflow):
    # PyYAML's YAML 1.1 loader treats the Actions key "on" as boolean True.
    return workflow.get("on", workflow.get(True))


def action_step(job, action):
    return next(
        step for step in job["steps"] if step.get("uses", "").startswith(action + "@")
    )


def step_by_id(job, name):
    return next(step for step in job["steps"] if step.get("id") == name)


class DevVersionWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sync = load_workflow("dev-version.yml")
        cls.image = load_workflow("docker-image.yml")
        cls.ci = load_workflow("ci.yml")

    def test_sync_events_only_target_dev_and_actual_stable_releases(self):
        events = workflow_events(self.sync)
        self.assertEqual(set(events), {"push", "release", "workflow_dispatch"})
        self.assertEqual(events["push"]["branches"], ["dev"])
        self.assertEqual(events["release"]["types"], ["published"])
        dry = events["workflow_dispatch"]["inputs"]["dry_run"]
        self.assertEqual(dry["type"], "boolean")
        self.assertIs(dry["default"], False)
        condition = self.sync["jobs"]["sync"]["if"]
        self.assertIn("github.event_name != 'release'", condition)
        self.assertIn("github.event.release.draft == false", condition)
        self.assertIn("github.event.release.prerelease == false", condition)

    def test_sync_checkout_uses_push_sha_and_other_events_resolve_dev(self):
        job = self.sync["jobs"]["sync"]
        checkout = action_step(job, "actions/checkout")["with"]
        self.assertEqual(
            checkout["ref"], "${{ github.event_name == 'push' && github.sha || 'dev' }}"
        )
        self.assertEqual(checkout["fetch-depth"], 0)
        self.assertIs(checkout["fetch-tags"], True)
        self.assertIs(checkout["persist-credentials"], False)
        self.assertEqual(self.sync["permissions"]["contents"], "read")
        self.assertEqual(self.sync["permissions"]["pull-requests"], "read")
        runs = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn('--head "$SOURCE_SHA"', runs)
        self.assertIn("SOURCE_SHA=$(git rev-parse HEAD)", runs)
        self.assertIn("master:refs/remotes/origin/master", runs)
        self.assertNotRegex(runs, r"git\s+(?:push|checkout|switch|reset)\b")
        self.assertNotRegex(
            runs, r"\b(?:docker\s+(?:build|push)|gh\s+(?:workflow|release))\b"
        )

    def test_only_publish_step_receives_existing_write_token_and_dry_run_guards_it(
        self,
    ):
        job = self.sync["jobs"]["sync"]
        publish = [step for step in job["steps"] if "--publish" in step.get("run", "")]
        self.assertEqual(len(publish), 1)
        self.assertEqual(
            publish[0]["env"],
            {"DEV_VERSION_TOKEN": "${{ secrets.RELEASE_PLEASE_TOKEN }}"},
        )
        self.assertEqual(
            publish[0]["if"],
            "github.event_name != 'workflow_dispatch' || inputs.dry_run != true",
        )
        for step in job["steps"]:
            secrets = [
                value
                for value in step.get("env", {}).values()
                if "secrets." in str(value)
            ]
            self.assertEqual(
                secrets, list(publish[0]["env"].values()) if step is publish[0] else []
            )
        self.assertNotRegex(
            str(self.sync),
            r"\[skip ci\]|\[ci skip\]|--force\b|--author\b|user\.name|user\.email",
        )
        release = load_workflow("release-please.yaml")
        release_action = action_step(
            release["jobs"]["release-please"], "googleapis/release-please-action"
        )
        self.assertEqual(release_action["with"]["target-branch"], "master")
        self.assertEqual(
            release_action["with"]["token"], publish[0]["env"]["DEV_VERSION_TOKEN"]
        )

    def test_image_events_stay_manual_and_merged_release_pr_only(self):
        events = workflow_events(self.image)
        self.assertEqual(set(events), {"workflow_dispatch", "pull_request"})
        self.assertEqual(events["pull_request"]["types"], ["closed"])
        for name in ("setup", "fetch-latest-tags", "cleanup-cache"):
            condition = self.image["jobs"][name]["if"]
            self.assertIn("github.event_name == 'workflow_dispatch'", condition)
            self.assertIn("github.event.pull_request.merged == true", condition)
            self.assertIn("chore(master): release ", condition)

    def test_manual_dev_calculates_once_and_exports_one_core_and_display_pair(self):
        calculations = [
            (name, step)
            for name, job in self.image["jobs"].items()
            for step in job["steps"]
            if "scripts/dev-version/calculate.mjs" in step.get("run", "")
        ]
        self.assertEqual(len(calculations), 1)
        name, calculation = calculations[0]
        self.assertEqual(name, "setup")
        self.assertEqual(calculation["if"], "github.ref_name == 'dev'")
        self.assertEqual(calculation["env"]["SOURCE_SHA"], "${{ github.sha }}")
        self.assertIn('--head "$SOURCE_SHA"', calculation["run"])
        self.assertIn('--github-output "$GITHUB_OUTPUT"', calculation["run"])
        self.assertNotIn("--publish", calculation["run"])
        self.assertNotIn("--stamp", calculation["run"])
        setup = self.image["jobs"]["setup"]
        extract = step_by_id(setup, "setup_env_vars")
        self.assertEqual(
            extract["env"]["CALCULATED_VERSION"],
            "${{ steps.dev_version.outputs.version }}",
        )
        self.assertEqual(
            extract["env"]["CALCULATED_DEV_VERSION"],
            "${{ steps.dev_version.outputs.dev_version }}",
        )
        self.assertEqual(
            setup["outputs"]["VERSION"], "${{ steps.setup_env_vars.outputs.VERSION }}"
        )
        self.assertEqual(
            setup["outputs"]["DEV_VERSION"],
            "${{ steps.setup_env_vars.outputs.DEV_VERSION }}",
        )
        self.assertEqual(
            action_step(setup, "actions/checkout")["with"]["ref"], "${{ github.sha }}"
        )

    def _run_version_exports(self, branch, core, display):
        setup = self.image["jobs"]["setup"]
        script = step_by_id(setup, "setup_env_vars")["run"]
        for expression, value in (
            ("github.repository_owner", "Example"),
            ("github.repository", "Example/DUMB"),
            ("github.ref_name", branch),
        ):
            script = script.replace("${{ " + expression + " }}", value)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath("pyproject.toml").write_text(
                '[project]\nversion = "2.23.0"\n'
            )
            output = root / "output"
            result = subprocess.run(
                ["bash", "-e", "-c", script],
                cwd=root,
                env={
                    **os.environ,
                    "CALCULATED_VERSION": core,
                    "CALCULATED_DEV_VERSION": display,
                    "GITHUB_OUTPUT": str(output),
                },
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            values = (
                dict(line.split("=", 1) for line in output.read_text().splitlines())
                if output.exists()
                else {}
            )
            return result.returncode, values

    def test_actual_shell_exports_calculated_dev_and_preserves_master_release(self):
        code, values = self._run_version_exports("dev", "2.24.0", "2.24.0-dev.37")
        self.assertEqual(code, 0)
        self.assertEqual(values["VERSION"], "2.24.0")
        self.assertEqual(values["DEV_VERSION"], "2.24.0-dev.37")
        self.assertEqual(values["BRANCH_NAME"], "dev")
        code, values = self._run_version_exports("master", "99.0.0", "99.0.0-dev.999")
        self.assertEqual(code, 0)
        self.assertEqual(values["VERSION"], "2.23.0")
        self.assertEqual(values["DEV_VERSION"], "")

    def test_actual_shell_refuses_missing_calculated_core_or_display(self):
        for core, display in (("", "2.24.0-dev.37"), ("2.24.0", ""), ("", "")):
            with self.subTest(core=core, display=display):
                code, _ = self._run_version_exports("dev", core, display)
                self.assertNotEqual(
                    code,
                    0,
                    "A dev image must not proceed with incomplete calculated version inputs",
                )

    def test_both_architectures_keep_source_sha_and_share_exact_version_inputs(self):
        inputs = []
        for name in ("build-amd64-digest", "build-arm64-digest"):
            job = self.image["jobs"][name]
            self.assertIn("setup", job["needs"])
            self.assertEqual(
                action_step(job, "actions/checkout")["with"]["ref"], "${{ github.sha }}"
            )
            build = action_step(job, "docker/build-push-action")["with"]
            args = dict(line.split("=", 1) for line in build["build-args"].splitlines())
            self.assertEqual(
                args["DEV_VERSION"], "${{ needs.setup.outputs.DEV_VERSION }}"
            )
            self.assertEqual(
                args["DUMB_API_VERSION"],
                "${{ github.ref_name == 'dev' && needs.setup.outputs.VERSION || '' }}",
            )
            inputs.append((args["DUMB_API_VERSION"], args["DEV_VERSION"]))
            host_runs = "\n".join(step.get("run", "") for step in job["steps"])
            self.assertNotIn("sync_dev_version.py", host_runs)
            self.assertNotIn("calculate.mjs", host_runs)
        self.assertEqual(inputs[0], inputs[1])

    def test_image_stamping_runs_after_copy_inside_runtime_with_no_sdk_install(self):
        dockerfile = ROOT.joinpath("Dockerfile").read_text()
        final_stage = dockerfile.split("FROM base AS final-stage", 1)[1]
        self.assertIn("ARG DUMB_API_VERSION", final_stage)
        self.assertLess(
            final_stage.index("COPY . /./"), final_stage.index("--stamp-image")
        )
        self.assertRegex(final_stage, r'RUN if \[ -n "\$DUMB_API_VERSION" \]; then')
        self.assertIn(
            'python3 /scripts/sync_dev_version.py --root / --stamp-image --version "$DUMB_API_VERSION"',
            final_stage,
        )
        self.assertIn("DUMB_VERSION=${DEV_VERSION}", final_stage)
        stamp = final_stage.split("RUN if", 1)[1].split("fi", 1)[0]
        self.assertNotRegex(stamp, r"\b(?:git|node|npm|pnpm)\b")
        ignore = {
            line.strip()
            for line in ROOT.joinpath(".dockerignore").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn("**/node_modules/", ignore)

    def test_actual_image_inspection_asserts_both_api_core_and_display_version(self):
        job = self.image["jobs"]["build-and-push"]
        step = next(
            step
            for step in job["steps"]
            if step.get("name") == "Extract Versions from Image"
        )
        self.assertEqual(
            step["env"]["EXPECTED_API_VERSION"], "${{ needs.setup.outputs.VERSION }}"
        )
        self.assertEqual(
            step["env"]["EXPECTED_DISPLAY_VERSION"],
            "${{ needs.setup.outputs.DEV_VERSION }}",
        )
        tokens = shlex.split(step["run"].replace("\\\n", " "))
        code = next(
            tokens[index + 1]
            for index, token in enumerate(tokens[:-1])
            if token == "-c" and "get_project_version" in tokens[index + 1]
        )
        for expected, actual, expected_display, actual_display, passes in (
            ("2.24.0", "2.24.0", "2.24.0-dev.37", "2.24.0-dev.37", True),
            ("2.23.0", "2.23.0", "", "", True),
            ("2.24.0", "2.23.0", "2.24.0-dev.37", "2.24.0-dev.37", False),
            ("2.24.0", "2.24.0", "2.24.0-dev.37", "2.23.0-dev.1", False),
        ):
            with self.subTest(actual=actual, display=actual_display):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    root.joinpath("utils").mkdir()
                    root.joinpath("utils", "__init__.py").write_text("")
                    root.joinpath("utils", "project_metadata.py").write_text(
                        f"def get_project_version(path):\n    return {actual!r}\n"
                    )
                    result = subprocess.run(
                        [sys.executable, "-c", code],
                        cwd=root,
                        env={
                            **os.environ,
                            "PYTHONPATH": "",
                            "EXPECTED_API_VERSION": expected,
                            "EXPECTED_DISPLAY_VERSION": expected_display,
                            "DUMB_VERSION": actual_display,
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=5,
                    )
                    if passes:
                        self.assertEqual(result.returncode, 0, result.stderr)
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(
                            (
                                "Built API version differs"
                                if actual != expected
                                else "Built display version differs"
                            ),
                            result.stderr,
                        )

    def test_ci_checks_pinned_node_calculator_and_metadata_only_changes(self):
        for event in ("pull_request", "push"):
            paths = workflow_events(self.ci)[event]["paths"]
            self.assertIn(".github/.release-please-manifest.json", paths)
            self.assertIn(".github/release-please-config.json", paths)
            self.assertIn("scripts/**", paths)
        job = self.ci["jobs"]["dev-version-checks"]
        self.assertEqual(
            str(action_step(job, "actions/setup-node")["with"]["node-version"]), "24"
        )
        runs = [step.get("run", "") for step in job["steps"]]
        self.assertIn("npm ci --prefix scripts/dev-version --ignore-scripts", runs)
        self.assertIn("npm test --prefix scripts/dev-version", runs)
        self.assertIn(
            "npm audit --prefix scripts/dev-version --audit-level=moderate", runs
        )


if __name__ == "__main__":
    unittest.main()
