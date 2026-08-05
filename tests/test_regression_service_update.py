import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import regression_service_update
from scripts.regression_service_update import prepare_config


class RegressionServiceUpdateTests(unittest.TestCase):
    def test_configured_release_target_is_persisted_before_update(self):
        service_config = {
            "release_version_enabled": True,
            "release_version": "v0.10.0-rc.1",
            "branch_enabled": False,
            "commit_sha": "",
        }
        response = type(
            "Result",
            (),
            {"stdout": json.dumps({"status": "service config updated"})},
        )()
        with (
            patch.object(
                regression_service_update,
                "_docker_exec_json",
                return_value=service_config,
            ),
            patch.object(
                regression_service_update, "_run", return_value=response
            ) as run,
        ):
            regression_service_update._set_configured_release_target(
                "case", "NzbDAV", "rc"
            )

        command = run.call_args.args[0]
        payload = json.loads(command[command.index("-d") + 1])
        self.assertEqual("rc", payload["updates"]["release_version"])
        self.assertTrue(payload["updates"]["release_version_enabled"])
        self.assertTrue(payload["persist"])

    def test_configured_update_does_not_request_latest_override(self):
        response = type("Result", (), {"stdout": json.dumps({"status": "updated"})})()
        with patch.object(
            regression_service_update, "_run", return_value=response
        ) as run:
            result = regression_service_update._post_update(
                "case", "NzbDAV", 60, target="configured"
            )

        payload = json.loads(
            run.call_args.args[0][run.call_args.args[0].index("-d") + 1]
        )
        self.assertEqual("updated", result["status"])
        self.assertEqual("configured", payload["target"])
        self.assertFalse(payload["allow_override"])

    def test_moving_channel_update_preserves_saved_selector(self):
        response = type("Result", (), {"stdout": json.dumps({"status": "updated"})})()
        with patch.object(
            regression_service_update, "_run", return_value=response
        ) as run:
            regression_service_update._post_update("case", "NzbDAV", 60, target=None)

        command = run.call_args.args[0]
        payload = json.loads(command[command.index("-d") + 1])
        self.assertIsNone(payload["target"])
        self.assertFalse(payload["allow_override"])

    def test_service_url_probes_run_inside_the_disposable_container(self):
        with patch.object(regression_service_update, "_run") as run:
            regression_service_update._probe_service_urls(
                "case", ["http://127.0.0.1:3000/healthz"]
            )

        command = run.call_args.args[0]
        self.assertEqual(["docker", "exec", "case", "curl"], command[:4])
        self.assertEqual("http://127.0.0.1:3000/healthz", command[-1])

    def test_container_running_probe_requires_successful_true_result(self):
        result = type("Result", (), {"returncode": 0, "stdout": "true\n"})()
        with patch.object(regression_service_update, "_run", return_value=result):
            self.assertTrue(regression_service_update._container_is_running("case"))

        result = type("Result", (), {"returncode": 0, "stdout": "false\n"})()
        with patch.object(regression_service_update, "_run", return_value=result):
            self.assertFalse(regression_service_update._container_is_running("case"))

    def test_runner_uses_the_dumb_virtualenv_python_as_entrypoint(self):
        source = Path("scripts/regression_service_update.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('"--entrypoint",\n            "/venv/bin/python",', source)
        self.assertNotIn("exec /venv/bin/python /main.py", source)

    def test_cleanup_uses_disposable_root_helper_for_root_owned_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            regression_root = Path(
                tempfile.mkdtemp(prefix="dumb-update-regression-example-", dir=temp_dir)
            )
            Path(regression_root, "state").write_text("test", encoding="utf-8")
            real_rmtree = shutil.rmtree

            def cleanup_run(command, **_kwargs):
                self.assertEqual("docker", command[0])
                self.assertEqual(regression_root.name, command[-1])
                real_rmtree(regression_root)
                return type("Result", (), {"stdout": "", "stderr": ""})()

            with (
                patch.object(
                    regression_service_update.shutil,
                    "rmtree",
                    side_effect=PermissionError("root-owned"),
                ),
                patch.object(
                    regression_service_update, "_run", side_effect=cleanup_run
                ) as run,
            ):
                regression_service_update._remove_regression_state(
                    regression_root, "dumb-regression-base:local"
                )

            run.assert_called_once()
            self.assertFalse(regression_root.exists())

    def test_prepares_release_service_without_enabling_unrelated_services(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "dumb_config.json"
            process_name, config_dir = prepare_config(
                Path("utils/dumb_config.json"),
                destination,
                key="decypharr",
                instance_name=None,
                previous_version="v2.0",
                selector="release",
                dependencies=[],
            )
            config = json.loads(destination.read_text(encoding="utf-8"))

        self.assertEqual("Decypharr", process_name)
        self.assertEqual(Path("/decypharr"), config_dir)
        self.assertTrue(config["decypharr"]["enabled"])
        self.assertEqual("v2.0", config["decypharr"]["release_version"])
        self.assertEqual("dfs", config["decypharr"]["mount_type"])
        self.assertFalse(config["maintainerr"]["enabled"])

    def test_prepares_pinned_instance_and_dependency(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "dumb_config.json"
            process_name, _ = prepare_config(
                Path("utils/dumb_config.json"),
                destination,
                key="sonarr",
                instance_name="Default",
                previous_version="4.0.14.2939",
                selector="pinned",
                dependencies=["postgres"],
            )
            config = json.loads(destination.read_text(encoding="utf-8"))

        self.assertEqual("Sonarr", process_name)
        self.assertTrue(config["sonarr"]["instances"]["Default"]["enabled"])
        self.assertEqual(
            "4.0.14.2939",
            config["sonarr"]["instances"]["Default"]["pinned_version"],
        )
        self.assertTrue(config["postgres"]["enabled"])
        self.assertFalse(config["radarr"]["instances"]["Default"]["enabled"])

    def test_prepares_release_instance_with_disposable_postgres_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "dumb_config.json"
            prepare_config(
                Path("utils/dumb_config.json"),
                destination,
                key="lidarr",
                instance_name="Default",
                previous_version="v3.1.0.4875",
                selector="release",
                dependencies=["postgres"],
                config_overrides={
                    "lidarr": {"instances": {"Default": {"postgres_enabled": True}}}
                },
            )
            config = json.loads(destination.read_text(encoding="utf-8"))

        instance = config["lidarr"]["instances"]["Default"]
        self.assertTrue(instance["enabled"])
        self.assertTrue(instance["postgres_enabled"])
        self.assertEqual("v3.1.0.4875", instance["release_version"])
        self.assertTrue(config["postgres"]["enabled"])
        self.assertEqual("Lidarr", instance["process_name"])

    def test_prepares_safe_disposable_authelia_origin(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "dumb_config.json"
            prepare_config(
                Path("utils/dumb_config.json"),
                destination,
                key="authelia",
                instance_name=None,
                previous_version="v4.39.19",
                selector="release",
                dependencies=["postgres"],
            )
            config = json.loads(destination.read_text(encoding="utf-8"))
            users = (destination.parent / "authelia" / "users_database.yml").read_text(
                encoding="utf-8"
            )

        self.assertEqual("https://auth.example.com", config["authelia"]["public_url"])
        self.assertEqual("example.com", config["authelia"]["cookie_domain"])
        self.assertTrue(config["postgres"]["enabled"])
        self.assertIn("regression:", users)
        self.assertIn("user@example.com", users)


if __name__ == "__main__":
    unittest.main()
