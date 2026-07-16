import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from utils import setup


class SetupDotnetTests(unittest.TestCase):
    @staticmethod
    def _write_zilean_source(temp_dir):
        Path(temp_dir, "Directory.Build.props").write_text(
            "<Project><PropertyGroup>"
            "<TargetFramework>net9.0</TargetFramework>"
            "</PropertyGroup></Project>",
            encoding="utf-8",
        )
        Path(temp_dir, "Directory.Packages.props").write_text(
            "<Project><ItemGroup>"
            '<PackageVersion Include="KubernetesClient" Version="15.0.1" />'
            "</ItemGroup></Project>",
            encoding="utf-8",
        )
        for project_name in ("Zilean.ApiService", "Zilean.Scraper"):
            project_dir = Path(temp_dir, "src", project_name)
            project_dir.mkdir(parents=True)
            Path(project_dir, f"{project_name}.csproj").write_text(
                "<Project />", encoding="utf-8"
            )

    def test_zilean_requires_local_dotnet_10_sdk_before_restore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write_zilean_source(temp_dir)
            with (
                patch.object(setup, "_dotnet_sdk_major", return_value=None),
                patch.object(
                    setup,
                    "_ensure_local_dotnet_sdk",
                    return_value=(False, "expected test stop", None),
                ) as ensure_sdk,
            ):
                success, error = setup.setup_dotnet_environment(
                    object(), "zilean", temp_dir
                )

            self.assertFalse(success)
            self.assertEqual(error, "expected test stop")
            self.assertEqual(ensure_sdk.call_args.args[-1], 10)

    def test_clear_directory_preserves_local_dotnet_sdk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sdk_marker = Path(temp_dir, ".dotnet-sdk", "sdk", "10.0.302")
            sdk_marker.parent.mkdir(parents=True)
            sdk_marker.write_text("cached", encoding="utf-8")
            removable = Path(temp_dir, "old-source.cs")
            removable.write_text("remove", encoding="utf-8")

            success, error = setup.clear_directory(temp_dir)

            self.assertTrue(success, error)
            self.assertTrue(sdk_marker.is_file())
            self.assertFalse(removable.exists())

    def test_zilean_repairs_local_sdk_and_retries_runtime_only_restore(self):
        class RestoreHandler:
            def __init__(self):
                self.calls = []
                self.returncode = None
                self.stderr = ""
                self.stdout = ""

            def start_process(self, name, config_dir, command, env=None):
                self.calls.append((name, config_dir, command, env.copy()))
                if len(self.calls) == 1:
                    self.returncode = 145
                    self.stderr = (
                        "The application 'restore' does not exist. "
                        "No .NET SDKs were found."
                    )
                    return False, "dotnet_env_restore failed to stay running."
                self.returncode = None
                self.stderr = ""
                return True, None

            def wait(self, _name):
                self.returncode = 0

        with tempfile.TemporaryDirectory() as temp_dir:
            self._write_zilean_source(temp_dir)
            app_dir = Path(temp_dir, "app")
            app_dir.mkdir()
            runtime_config = {"runtimeOptions": {"tfm": "net10.0"}}
            for file_name in (
                "zilean-api.runtimeconfig.json",
                "scraper.runtimeconfig.json",
            ):
                Path(app_dir, file_name).write_text(
                    json.dumps(runtime_config), encoding="utf-8"
                )

            handler = RestoreHandler()

            def sdk_major(dotnet_cmd, _env):
                return None if dotnet_cmd == "dotnet" else 10

            with (
                patch.object(setup, "_dotnet_sdk_major", side_effect=sdk_major),
                patch.object(
                    setup,
                    "_ensure_local_dotnet_sdk",
                    side_effect=[
                        (True, None, "/managed-sdk/dotnet"),
                        (True, None, "/repaired-sdk/dotnet"),
                    ],
                ) as ensure_sdk,
            ):
                success, error = setup.setup_dotnet_environment(
                    handler,
                    "zilean",
                    temp_dir,
                    project_paths=[],
                )

            self.assertTrue(success, error)
            self.assertEqual(len(handler.calls), 2)
            self.assertEqual(handler.calls[0][2][0], "/managed-sdk/dotnet")
            self.assertEqual(handler.calls[1][2][0], "/repaired-sdk/dotnet")
            self.assertEqual(ensure_sdk.call_count, 2)
            self.assertNotIn("force_reinstall", ensure_sdk.call_args_list[0].kwargs)
            self.assertTrue(ensure_sdk.call_args_list[1].kwargs.get("force_reinstall"))

    def test_force_reinstall_clears_stale_managed_sdk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir, ".dotnet-sdk")
            stale_sdk = install_root / "sdk" / "10.0.100" / "stale.txt"
            stale_sdk.parent.mkdir(parents=True)
            stale_sdk.write_text("stale", encoding="utf-8")
            stale_dotnet = install_root / "dotnet"
            stale_dotnet.write_text("stale", encoding="utf-8")
            env = {"PATH": "/usr/bin"}
            response = SimpleNamespace(
                text="#!/usr/bin/env bash\n",
                raise_for_status=lambda: None,
            )
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch.object(setup.requests, "get", return_value=response),
                patch.object(setup.subprocess, "run", return_value=completed),
                patch.object(setup, "_dotnet_sdk_major", return_value=10),
                patch.object(setup, "chown_single"),
            ):
                success, error, dotnet_cmd = setup._ensure_local_dotnet_sdk(
                    temp_dir,
                    env,
                    10,
                    force_reinstall=True,
                )

            self.assertTrue(success, error)
            self.assertFalse(stale_sdk.exists())
            self.assertEqual(dotnet_cmd, str(install_root / "dotnet"))
            self.assertEqual(env["DOTNET_ROOT"], str(install_root))
            self.assertEqual(env["DUMB_DOTNET_BIN"], dotnet_cmd)


if __name__ == "__main__":
    unittest.main()
