import copy
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from utils import setup


class SetupDotnetTests(unittest.TestCase):
    def test_zilean_runtime_data_is_created_and_owned_for_managed_user(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir, "app", "data")
            with (
                patch.object(setup, "user_id", 1000),
                patch.object(setup, "group_id", 1001),
                patch.object(
                    setup, "chown_recursive", return_value=(True, None)
                ) as chown,
            ):
                success, error = setup._prepare_zilean_runtime_data(
                    {
                        "config_dir": temp_dir,
                        "config_file": str(data_dir / "settings.json"),
                    }
                )

            self.assertTrue(success, error)
            self.assertTrue(data_dir.is_dir())
            chown.assert_called_once_with(str(data_dir), 1000, 1001)

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

    def test_partial_nzbdav_environment_preserves_system_path(self):
        class RestoreHandler:
            def __init__(self):
                self.calls = []
                self.returncode = None
                self.stderr = ""
                self.stdout = ""

            def start_process(self, name, config_dir, command, env=None):
                self.calls.append((name, config_dir, command, env.copy()))
                return True, None

            def wait(self, _name):
                self.returncode = 0

        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir, "NzbWebDAV.csproj")
            project_path.write_text(
                "<Project><PropertyGroup>"
                "<TargetFramework>net10.0</TargetFramework>"
                "</PropertyGroup></Project>",
                encoding="utf-8",
            )
            handler = RestoreHandler()
            partial_env = {
                "HOME": temp_dir,
                "DOTNET_CLI_HOME": str(Path(temp_dir, ".dotnet")),
                "NUGET_PACKAGES": str(Path(temp_dir, ".nuget", "packages")),
            }

            with (
                patch.dict(setup.os.environ, {"PATH": "/bin:/usr/bin"}, clear=False),
                patch.object(setup, "_dotnet_sdk_major", return_value=10),
            ):
                success, error = setup.setup_dotnet_environment(
                    handler,
                    "infinidysk",
                    temp_dir,
                    project_paths=[],
                    restore_project_path=str(project_path),
                    env=partial_env,
                )

            self.assertTrue(success, error)
            restore_env = handler.calls[0][3]
            self.assertEqual(restore_env["PATH"], "/bin:/usr/bin")
            self.assertEqual(restore_env["HOME"], temp_dir)
            self.assertEqual(
                restore_env["DOTNET_CLI_HOME"], str(Path(temp_dir, ".dotnet"))
            )

    def test_infinidysk_publish_disables_upstream_analyzers_only(self):
        class PublishHandler:
            def __init__(self):
                self.calls = []
                self.returncode = None
                self.stderr = ""
                self.stdout = ""

            def start_process(self, name, config_dir, command, env=None):
                self.calls.append((name, config_dir, command, env.copy()))
                return True, None

            def wait(self, _name):
                self.returncode = 0

        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir, "NzbWebDAV.csproj")
            project_path.write_text(
                "<Project><PropertyGroup>"
                "<TargetFramework>net10.0</TargetFramework>"
                "</PropertyGroup></Project>",
                encoding="utf-8",
            )
            handler = PublishHandler()

            with patch.object(setup, "_dotnet_sdk_major", return_value=10):
                success, error = setup.setup_dotnet_environment(
                    handler,
                    "infinidysk",
                    temp_dir,
                    project_paths=[str(project_path)],
                    restore_project_path=str(project_path),
                )

        self.assertTrue(success, error)
        publish_command = next(
            call[2] for call in handler.calls if call[0] == "dotnet_publish"
        )
        self.assertIn("/p:RunAnalyzers=false", publish_command)
        self.assertIn("/p:EnableNETAnalyzers=false", publish_command)
        self.assertNotIn("/p:NoBuild=true", publish_command)


class VerifiedDotnetSdkTests(unittest.TestCase):
    SDK_VERSION = "10.0.401"
    SDK_URL = (
        "https://builds.dotnet.microsoft.com/dotnet/Sdk/10.0.401/"
        "dotnet-sdk-10.0.401-linux-arm64.tar.gz"
    )

    class Response:
        def __init__(self, content=b"", metadata=None, error=None):
            self.content = content
            self.metadata = metadata
            self.error = error
            self.closed = False

        def raise_for_status(self):
            if self.error:
                raise self.error

        def json(self):
            return self.metadata

        def iter_content(self, chunk_size):
            yield self.content[:17]
            yield self.content[17:]

        def close(self):
            self.closed = True

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.install_root = self.root / ".dotnet-sdk"
        self.install_root.mkdir()
        self.original_dotnet = self.install_root / "dotnet"
        self.original_dotnet.write_text("previous SDK", encoding="utf-8")
        self.original_marker = self.install_root / "sdk" / "9.0.100" / "marker"
        self.original_marker.parent.mkdir(parents=True)
        self.original_marker.write_text("preserve", encoding="utf-8")
        self.env = {"PATH": "/bin:/usr/bin"}

        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as output:
            payload = b"verified SDK"
            entry = tarfile.TarInfo("dotnet")
            entry.size = len(payload)
            entry.mode = 0o755
            output.addfile(entry, io.BytesIO(payload))
        self.payload = archive.getvalue()
        self.metadata = {
            "releases": [
                {
                    "sdk": {
                        "version": self.SDK_VERSION,
                        "files": [
                            {
                                "url": self.SDK_URL,
                                "hash": hashlib.sha512(self.payload).hexdigest(),
                            }
                        ],
                    }
                }
            ]
        }
        self.script_response = self.Response(content=b"#!/bin/bash\r\n")
        self.signature_response = self.Response(content=b"\x00\xff detached signature")
        self.archive_response = self.Response(content=self.payload)
        self.dry_run_returncode = 0
        self.dry_run_output = (
            "dotnet-install: Payload URLs:\n"
            f"dotnet-install: URL #0 - aka.ms: {self.SDK_URL}\n"
        )
        self.candidate_version = self.SDK_VERSION
        self.dry_run_calls = []

        self.fetch = self._patch(
            setup.requests, "get", side_effect=self._response_for_url
        )
        self.verify = self._patch(setup, "verify_publisher_signature")
        self.run = self._patch(
            setup, "_run_dotnet_bootstrap", side_effect=self._run_installer_or_candidate
        )
        self._patch(setup, "_dotnet_sdk_major", return_value=9)
        self.ownership = self._patch(
            setup, "chown_recursive", return_value=(True, None)
        )

    def _patch(self, target, name, **kwargs):
        patched = patch.object(target, name, **kwargs)
        self.addCleanup(patched.stop)
        return patched.start()

    def _response_for_url(self, url, **kwargs):
        if url.endswith("dotnet-install.sh"):
            return self.script_response
        if url.endswith("dotnet-install.sig"):
            return self.signature_response
        if url.endswith("releases.json"):
            return self.Response(metadata=self.metadata)
        if url == self.SDK_URL:
            self.assertTrue(kwargs["stream"])
            return self.archive_response
        self.fail(f"Unexpected download: {url}")

    def _run_installer_or_candidate(self, command, **kwargs):
        self.assertEqual(self.verify.call_count, 1)
        if command[0] == "/bin/bash":
            self.assertIn("--dry-run", command)
            self.assertNotIn("--install-dir", command)
            self.dry_run_calls.append(command)
            self.assertEqual(self.original_dotnet.read_text(), "previous SDK")
            return SimpleNamespace(
                returncode=self.dry_run_returncode,
                stdout=self.dry_run_output,
                stderr="resolution failed",
            )
        self.assertTrue(self.archive_response.closed)
        self.assertTrue(command[0].endswith("/candidate/dotnet"))
        self.assertEqual(command[1:], ["--version"])
        self.assertEqual(self.original_dotnet.read_text(), "previous SDK")
        candidate_parent = Path(command[0]).parent.parent
        self.assertEqual(candidate_parent.stat().st_mode & 0o777, 0o700)
        return SimpleNamespace(
            returncode=0, stdout=self.candidate_version + "\n", stderr=""
        )

    def _ensure(self, force_reinstall=True):
        return setup._ensure_local_dotnet_sdk(
            str(self.root), self.env, 10, force_reinstall=force_reinstall
        )

    def _assert_preserved(self):
        self.assertEqual(self.original_dotnet.read_text(), "previous SDK")
        self.assertEqual(self.original_marker.read_text(), "preserve")
        self.assertEqual(self.env, {"PATH": "/bin:/usr/bin"})
        self.assertEqual(list(self.root.glob(".dumb-dotnet-sdk-*")), [])

    def test_verified_candidate_replaces_stale_sdk_after_exact_version_probe(self):
        def check_signature(script, signature, publisher):
            self.assertEqual(script.read_bytes(), self.script_response.content)
            self.assertEqual(signature.read_bytes(), self.signature_response.content)
            self.assertEqual(publisher, "dotnet")
            self.assertEqual(script.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.original_dotnet.read_text(), "previous SDK")

        self.verify.side_effect = check_signature
        success, error, dotnet_cmd = self._ensure()

        self.assertTrue(success, error)
        self.assertEqual(self.original_dotnet.read_bytes(), b"verified SDK")
        self.assertFalse(self.original_marker.exists())
        self.assertEqual(dotnet_cmd, str(self.install_root / "dotnet"))
        self.assertEqual(self.env["DOTNET_ROOT"], str(self.install_root))
        self.assertEqual(self.env["DUMB_DOTNET_BIN"], dotnet_cmd)
        self.assertEqual(len(self.dry_run_calls), 1)
        self.ownership.assert_called_once()
        self.assertEqual(list(self.root.glob(".dumb-dotnet-sdk-*")), [])

    def test_symlinked_service_directory_keeps_existing_topology(self):
        configured_service = self.root / "configured-service"
        configured_service.symlink_to(self.root, target_is_directory=True)
        success, error, dotnet_cmd = setup._ensure_local_dotnet_sdk(
            str(configured_service), self.env, 10, force_reinstall=True
        )
        self.assertTrue(success, error)
        self.assertTrue(configured_service.is_symlink())
        self.assertEqual(dotnet_cmd, str(configured_service / ".dotnet-sdk" / "dotnet"))
        self.assertEqual(self.original_dotnet.read_bytes(), b"verified SDK")
        self.assertEqual(list(self.root.glob(".dumb-dotnet-sdk-*")), [])

    def test_satisfied_newer_sdk_skips_network_and_signature_provisioning(self):
        with patch.object(setup, "_dotnet_sdk_major", return_value=11):
            success, error, _ = self._ensure(force_reinstall=False)
        self.assertTrue(success, error)
        self.assertEqual(self.original_dotnet.read_text(), "previous SDK")
        self.fetch.assert_not_called()
        self.verify.assert_not_called()
        self.run.assert_not_called()

    def test_invalid_signature_preserves_sdk_and_prevents_any_script_execution(self):
        self.verify.side_effect = setup.ArtifactVerificationError("invalid signature")
        success, error, _ = self._ensure()
        self.assertFalse(success)
        self.assertIn("invalid signature", error)
        self.run.assert_not_called()
        self.assertEqual(self.fetch.call_count, 2)
        self._assert_preserved()

    def test_missing_signature_preserves_sdk_and_prevents_any_script_execution(self):
        self.signature_response.error = setup.requests.HTTPError("missing signature")
        success, error, _ = self._ensure()
        self.assertFalse(success)
        self.assertIn("missing signature", error)
        self.verify.assert_not_called()
        self.run.assert_not_called()
        self._assert_preserved()

    def test_digest_mismatch_stops_before_extraction_or_version_execution(self):
        self.archive_response.content = b"tampered payload"
        with patch.object(setup.downloader, "_extract_tarfile") as extract:
            success, error, _ = self._ensure()
        self.assertFalse(success)
        self.assertIn("SHA-512 verification", error)
        extract.assert_not_called()
        self.assertEqual(self.run.call_count, 1)
        self.ownership.assert_not_called()
        self._assert_preserved()

    def test_missing_checksum_stops_before_archive_download(self):
        self.metadata["releases"][0]["sdk"]["files"][0]["hash"] = ""
        success, error, _ = self._ensure()
        self.assertFalse(success)
        self.assertIn("published SHA-512", error)
        self.assertEqual(self.fetch.call_count, 3)
        self.assertEqual(self.run.call_count, 1)
        self._assert_preserved()

    def test_conflicting_checksum_metadata_stops_before_archive_download(self):
        conflict = copy.deepcopy(self.metadata["releases"][0]["sdk"])
        conflict["files"][0]["hash"] = "f" * 128
        self.metadata["releases"][0]["sdks"] = [conflict]
        success, error, _ = self._ensure()
        self.assertFalse(success)
        self.assertIn("unambiguous", error)
        self.assertEqual(self.fetch.call_count, 3)
        self._assert_preserved()

    def test_foreign_payload_url_stops_before_metadata_or_archive_download(self):
        self.dry_run_output = (
            "dotnet-install: URL #0 - primary: "
            "https://example.com/dotnet-sdk-10.0.401-linux-arm64.tar.gz\n"
        )
        success, error, _ = self._ensure()
        self.assertFalse(success)
        self.assertIn("unsupported SDK archive", error)
        self.assertEqual(self.fetch.call_count, 2)
        self._assert_preserved()

    def test_unexpected_dry_run_output_does_not_run_normal_installer(self):
        self.dry_run_output = "no SDK payload was selected\n"
        success, error, _ = self._ensure()
        self.assertFalse(success)
        self.assertIn("did not resolve", error)
        self.assertEqual(self.run.call_count, 1)
        self._assert_preserved()

    def test_candidate_version_mismatch_preserves_existing_sdk(self):
        self.candidate_version = "10.0.400"
        success, error, _ = self._ensure()
        self.assertFalse(success)
        self.assertIn("selected SDK version", error)
        self.ownership.assert_not_called()
        self._assert_preserved()

    def test_activation_failure_restores_previous_sdk(self):
        original_replace = setup.os.replace

        def fail_candidate(source, target, **kwargs):
            if Path(source).name == "candidate":
                raise OSError("activation denied")
            return original_replace(source, target, **kwargs)

        with patch.object(setup.os, "replace", side_effect=fail_candidate):
            success, error, _ = self._ensure()
        self.assertFalse(success)
        self.assertIn("activation denied", error)
        self._assert_preserved()

    def test_failed_rollback_retains_previous_sdk_for_recovery(self):
        original_replace = setup.os.replace

        def fail_activation_and_rollback(source, target, **kwargs):
            if Path(source).name in {"candidate", "previous"}:
                raise OSError("rename denied")
            return original_replace(source, target, **kwargs)

        with patch.object(
            setup.os, "replace", side_effect=fail_activation_and_rollback
        ):
            success, error, _ = self._ensure()
        self.assertFalse(success)
        self.assertIn("Previous SDK retained at", error)
        stages = list(self.root.glob(".dumb-dotnet-sdk-*"))
        self.assertEqual(len(stages), 1)
        self.assertEqual(
            (stages[0] / "previous" / "dotnet").read_text(), "previous SDK"
        )
        self.assertEqual(
            (stages[0] / "previous" / "sdk" / "9.0.100" / "marker").read_text(),
            "preserve",
        )

    def test_preview_fallback_only_resolves_before_verified_archive_installation(self):
        original_run = self._run_installer_or_candidate

        def resolve_preview(command, **kwargs):
            if command[0] == "/bin/bash" and "--quality" not in command:
                self.dry_run_calls.append(command)
                return SimpleNamespace(returncode=1, stdout="", stderr="no stable SDK")
            return original_run(command, **kwargs)

        self.run.side_effect = resolve_preview
        success, error, _ = self._ensure()
        self.assertTrue(success, error)
        self.assertEqual(len(self.dry_run_calls), 2)
        self.assertEqual(self.dry_run_calls[-1][-2:], ["--quality", "preview"])
        self.assertEqual(self.verify.call_count, 1)

    def test_oversized_payload_preserves_sdk_without_extracting(self):
        with patch.object(
            setup.downloader, "_archive_limits", return_value={"download_bytes": 5}
        ):
            success, error, _ = self._ensure()
        self.assertFalse(success)
        self.assertIn("download size limit", error)
        self.assertEqual(self.run.call_count, 1)
        self._assert_preserved()

    def test_signed_checksum_does_not_bypass_archive_path_safety(self):
        malicious_archive = io.BytesIO()
        with tarfile.open(fileobj=malicious_archive, mode="w:gz") as output:
            entry = tarfile.TarInfo("../escape")
            entry.size = 1
            output.addfile(entry, io.BytesIO(b"x"))
        self.archive_response.content = malicious_archive.getvalue()
        self.metadata["releases"][0]["sdk"]["files"][0]["hash"] = hashlib.sha512(
            self.archive_response.content
        ).hexdigest()
        success, error, _ = self._ensure()
        self.assertFalse(success)
        self.assertIn("Unsafe archive member", error)
        self.assertFalse((self.root / "escape").exists())
        self.assertEqual(self.run.call_count, 1)
        self._assert_preserved()

    def test_bootstrap_environment_excludes_unverified_service_code_hooks(self):
        self.env.update(
            {
                "BASH_ENV": "/service/bootstrap.sh",
                "ENV": "/service/environment.sh",
                "BASH_FUNC_curl%%": "() { malicious-command; }",
                "LD_PRELOAD": "/service/preload.so",
                "DOTNET_STARTUP_HOOKS": "/service/startup.dll",
                "HTTPS_PROXY": "https://proxy.example:443",
            }
        )
        success, error, _ = self._ensure()
        self.assertTrue(success, error)
        for call in self.run.call_args_list:
            bootstrap_env = call.kwargs["env"]
            self.assertEqual(bootstrap_env["PATH"], "/usr/sbin:/usr/bin:/sbin:/bin")
            self.assertEqual(bootstrap_env["HTTPS_PROXY"], "https://proxy.example:443")
            for unsafe in (
                "BASH_ENV",
                "ENV",
                "BASH_FUNC_curl%%",
                "LD_PRELOAD",
                "DOTNET_STARTUP_HOOKS",
            ):
                self.assertNotIn(unsafe, bootstrap_env)
        self.assertEqual(self.env["BASH_ENV"], "/service/bootstrap.sh")

    def test_resolution_timeout_preserves_sdk_and_does_not_try_preview(self):
        self.run.side_effect = setup.subprocess.TimeoutExpired(
            "signed installer resolution", 180
        )
        success, error, _ = self._ensure()
        self.assertFalse(success)
        self.assertIn("timed out", error)
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.run.call_args.kwargs["timeout"], 180)
        self._assert_preserved()

    def test_activation_stage_rename_does_not_replace_verified_candidate(self):
        original_copy = setup.shutil.copytree

        def move_named_stage(source, destination, **kwargs):
            stages = list(self.root.glob(".dumb-dotnet-sdk-*"))
            self.assertEqual(len(stages), 1)
            named_stage = stages[0]
            named_stage.rename(self.root / "renamed-stage")
            named_stage.mkdir()
            (named_stage / "attacker-marker").write_text("leave alone")
            return original_copy(source, destination, **kwargs)

        with patch.object(setup.shutil, "copytree", side_effect=move_named_stage):
            success, error, _ = self._ensure()
        self.assertTrue(success, error)
        self.assertEqual(self.original_dotnet.read_bytes(), b"verified SDK")
        replacements = list(self.root.glob(".dumb-dotnet-sdk-*"))
        self.assertEqual(len(replacements), 1)
        self.assertEqual(
            (replacements[0] / "attacker-marker").read_text(), "leave alone"
        )
        self.assertFalse((self.root / "renamed-stage").exists())

    def test_ownership_failure_prevents_activation_and_preserves_working_sdk(self):
        self.ownership.return_value = (False, "ownership failed")
        success, error, _ = self._ensure()
        self.assertFalse(success)
        self.assertIn("ownership failed", error)
        self._assert_preserved()


class DotnetBootstrapProcessTests(unittest.TestCase):
    def test_timeout_kills_and_waits_for_entire_bootstrap_process_group(self):
        process = SimpleNamespace(
            pid=12345,
            communicate=Mock(
                side_effect=[
                    setup.subprocess.TimeoutExpired("bootstrap", 180),
                    ("", ""),
                ]
            ),
        )
        with (
            patch.object(setup.subprocess, "Popen", return_value=process) as popen,
            patch.object(setup.os, "killpg") as killpg,
        ):
            with self.assertRaises(setup.subprocess.TimeoutExpired):
                setup._run_dotnet_bootstrap(
                    ["/bin/bash", "/tmp/verified-script", "--dry-run"],
                    env={"PATH": "/usr/bin"},
                    timeout=180,
                )
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_once_with(12345, setup.signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)


if __name__ == "__main__":
    unittest.main()
