import ast
from functools import wraps
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from utils import infinidysk_v2_runtime as runtime
from utils import infinidysk_v2_topology as topology


def isolated_runtime_test(method):
    """Run import-sensitive integration checks with their actual dependencies.

    Full unittest discovery imports older suites that replace sys.modules entries
    with permanent config/setup/dependency doubles. A fresh interpreter preserves
    the real runtime contracts without changing those unrelated test fixtures.
    """

    @wraps(method)
    def isolated(self):
        test_id = f"tests.{Path(__file__).stem}.{type(self).__name__}.{method.__name__}"
        if os.environ.get("DUMB_NATIVE_RUNTIME_TEST_ID") == test_id:
            return method(self)
        result = subprocess.run(
            [sys.executable, "-m", "unittest", test_id],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "DUMB_NATIVE_RUNTIME_TEST_ID": test_id},
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    return isolated


class NativeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o755)
        self.service = {
            "enabled": True,
            "runtime_generation": "2",
            "mount_backend": "rclone",
            "mount_path": str(self.root / "mount"),
            "config_dir": str(self.root / "legacy"),
            "v2_data_dir": str(self.root / "data"),
            "v2_runtime_dir": str(self.root / "program"),
            "frontend_port": 3333,
            "env": {},
        }

    def installed(self):
        target = runtime.runtime_dir(self.service)
        target.mkdir()
        binary = target / "infinidysk"
        binary.write_bytes(b"fixture-native-binary")
        binary.chmod(0o755)
        (target / "commit.txt").write_text(runtime.COMMIT)
        (target / "version.txt").write_text(runtime.RELEASE)
        (target / runtime.MARKER).write_text(
            json.dumps(
                {
                    "commit": runtime.COMMIT,
                    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                }
            )
        )
        return target

    def archive(self, *, commit=None, member=None):
        output = self.root / "fixture.tar.gz"
        prefix = f"infinidysk-{runtime.RELEASE}-linux-x64"
        files = {
            "infinidysk": b"fixture-binary",
            "version.txt": runtime.RELEASE.encode(),
            "commit.txt": (commit or runtime.COMMIT).encode(),
            "LICENSE": b"fixture license",
        }
        with tarfile.open(output, "w:gz") as package:
            for name, data in files.items():
                info = tarfile.TarInfo(f"{prefix}/{name}")
                info.size = len(data)
                package.addfile(info, io.BytesIO(data))
            if member:
                package.addfile(member)
        return output

    def install(self, archive, *, digest=None):
        from utils import config_loader

        def download(url, target):
            self.assertIn(f"/releases/download/{runtime.RELEASE}/", url)
            Path(target).write_bytes(archive.read_bytes())

        with (
            patch.object(
                config_loader.CONFIG_MANAGER,
                "get",
                side_effect=lambda key, section=None: (
                    str(self.root / "default-data")
                    if key == "data_root" and not section
                    else None
                ),
            ),
            patch.object(runtime.platform, "machine", return_value="x86_64"),
            patch.object(runtime.platform, "system", return_value="Linux"),
            patch.object(runtime.platform, "libc_ver", return_value=("glibc", "2.43")),
            patch.object(runtime, "_download_archive", side_effect=download),
            patch.dict(
                runtime.ASSETS,
                {"x64": digest or hashlib.sha256(archive.read_bytes()).hexdigest()},
            ),
            patch.object(
                runtime.subprocess,
                "run",
                return_value=SimpleNamespace(
                    stdout=json.dumps(
                        {"paths": {"/api/v1/upgrade/status": {}}}
                    ).encode()
                ),
            ) as probe,
        ):
            result = runtime.install_v2_runtime(Mock(), "InfiniDysk", self.service)
        return result, probe

    def test_archive_provenance_and_openapi_probe_never_touch_legacy_or_data(self):
        legacy = Path(self.service["config_dir"])
        legacy.mkdir()
        (legacy / "db.sqlite").write_bytes(b"retain-original")
        result, probe = self.install(self.archive())
        self.assertTrue(result[0], result[1])
        self.assertEqual(probe.call_args.args[0][1:], ["openapi"])
        self.assertEqual(runtime.installed_v2_version(self.service), runtime.RELEASE)
        self.assertEqual((legacy / "db.sqlite").read_bytes(), b"retain-original")
        self.assertFalse(Path(self.service["v2_data_dir"]).exists())

    def test_bad_digest_commit_or_archive_link_refused_before_activation(self):
        bad_link = tarfile.TarInfo("escape")
        bad_link.type = tarfile.SYMTYPE
        bad_link.linkname = "/tmp/foreign"
        for kwargs, digest in (
            ({}, "wrong"),
            ({"commit": "0" * 40}, None),
            ({"member": bad_link}, None),
        ):
            with self.subTest(kwargs=kwargs):
                result, probe = self.install(self.archive(**kwargs), digest=digest)
                self.assertFalse(result[0])
                self.assertFalse(runtime.runtime_dir(self.service).exists())
                probe.assert_not_called()

    def test_foreign_runtime_directory_and_source_modes_are_preserved(self):
        target = runtime.runtime_dir(self.service)
        target.mkdir()
        (target / "foreign").write_text("keep")
        result, probe = self.install(self.archive())
        self.assertFalse(result[0])
        probe.assert_not_called()
        self.assertEqual((target / "foreign").read_text(), "keep")
        for selected in (
            {"commit_sha": "a" * 40},
            {"branch_enabled": True},
            {"v2_release_version": "latest"},
            {"repo_owner": "fork"},
        ):
            valid, error = runtime.validate_v2_selection({**self.service, **selected})
            self.assertFalse(valid, error)

    def test_shared_or_redirected_data_paths_refused(self):
        for path in (
            self.service["config_dir"],
            self.service["v2_runtime_dir"],
            str(self.root / "program/data"),
        ):
            self.assertFalse(
                runtime.validate_v2_paths({**self.service, "v2_data_dir": path})[0]
            )
        Path(self.service["v2_data_dir"]).symlink_to(self.root / "elsewhere")
        self.assertFalse(runtime.validate_v2_paths(self.service)[0])

    def test_private_credentials_persist_and_v1_provider_is_not_injected(self):
        self.installed()
        self.service["env"] = {
            "DATABASE_PROVIDER": "postgres",
            "DATABASE_CONNECTION_STRING": "legacy-private-value",
            "CONFIG_PATH": "/legacy",
            "TZ": "UTC",
            "INFINIDYSK_DATABASE_URL": "postgres://example.invalid/native",
        }
        valid, error = runtime.prepare_v2_launch(self.service, os.getuid(), os.getgid())
        self.assertTrue(valid, error)
        original = runtime.read_native_credentials(self.service)
        env = runtime.build_v2_environment(self.service)
        self.assertNotIn("DATABASE_PROVIDER", env)
        self.assertNotIn("DATABASE_CONNECTION_STRING", env)
        self.assertNotIn("CONFIG_PATH", env)
        self.assertEqual(
            env["INFINIDYSK_DATABASE_URL"], "postgres://example.invalid/native"
        )
        self.assertEqual(env["INFINIDYSK_LISTEN"], "0.0.0.0:3333")
        self.assertEqual(env["INFINIDYSK__MOUNT__FUSE"], "false")
        self.assertEqual(runtime.native_auth(self.service), original)
        private = Path(self.service["v2_data_dir"]) / runtime.AUTH_FILE
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)
        private.chmod(0o644)
        with self.assertRaises(ValueError):
            runtime.native_auth(self.service)

    def test_compatibility_auth_lock_preserves_saved_native_policy_and_other_auth(self):
        runtime.native_auth(self.service, create=True)
        data = Path(self.service["v2_data_dir"])
        saved = data / "infinidysk.sqlite"
        saved.write_bytes(b"saved-native-auth-local-policy")
        before = saved.read_bytes()
        self.service["env"] = {
            "INFINIDYSK__Auth__Mode": "required",
            "INFINIDYSK__AUTH__LOCAL_LOGIN": "false",
        }
        original_env = dict(self.service["env"])
        environment = runtime.build_v2_environment(self.service)
        self.assertEqual(environment["INFINIDYSK__AUTH__MODE"], "required")
        self.assertNotIn("INFINIDYSK__Auth__Mode", environment)
        self.assertEqual(environment["INFINIDYSK__AUTH__LOCAL_LOGIN"], "false")
        self.assertEqual(self.service["env"], original_env)
        self.assertEqual(saved.read_bytes(), before)

    def test_disabled_compatibility_preserves_native_auth_environment(self):
        runtime.native_auth(self.service, create=True)
        self.service["backend_port"] = self.service["frontend_port"]
        self.service["env"] = {"INFINIDYSK__Auth__Mode": "local"}
        environment = runtime.build_v2_environment(self.service)
        self.assertEqual(environment["INFINIDYSK__Auth__Mode"], "local")
        self.assertNotIn("INFINIDYSK__AUTH__MODE", environment)

    def test_conflicting_compatibility_auth_is_refused_before_auth_or_data_creation(
        self,
    ):
        self.installed()
        self.service["env"] = {"INFINIDYSK__Auth": '{"mode":"local"}'}
        with patch.object(runtime, "native_auth") as auth:
            with self.assertRaisesRegex(
                ValueError, "requires native admin authentication"
            ):
                runtime.build_v2_environment(self.service)
            valid, error = runtime.prepare_v2_launch(
                self.service, os.getuid(), os.getgid()
            )
        self.assertFalse(valid)
        self.assertIn("requires native admin authentication", error)
        auth.assert_not_called()
        self.assertFalse(Path(self.service["v2_data_dir"]).exists())

    def test_import_preserves_webdav_password_adoption_and_existing_rclone_secret(self):
        data = Path(self.service["v2_data_dir"])
        data.mkdir()
        (data / "db.sqlite").write_bytes(b"retained-copy")
        auth = runtime.native_auth(self.service, create=True, allow_v1_import=True)
        self.assertIsNone(auth["webdav_password"])
        obscure = Mock()
        lines = runtime.rclone_remote(
            self.service, ["user = old-user", "pass = obscured-value"], obscure
        )
        self.assertIn("user = old-user", lines)
        self.assertIn("pass = obscured-value", lines)
        self.assertIn("url = http://127.0.0.1:3333/", lines)
        obscure.assert_not_called()
        self.assertNotIn(
            "INFINIDYSK__WEBDAV__PASSWORD", runtime.build_v2_environment(self.service)
        )

    @isolated_runtime_test
    def test_configure_uses_real_config_accessor_semantics_and_custom_data_root(self):
        from utils import config_loader

        self.installed()
        self.service["v2_data_dir"] = ""
        values = {
            "puid": 1000,
            "pgid": 1000,
            "data_root": str(self.root / "custom-data"),
            "rclone": {
                "instances": {
                    "one": {
                        "enabled": True,
                        "key_type": "infinidysk",
                        "mount_dir": str(self.root),
                        "mount_name": "mount",
                    }
                }
            },
        }

        # Use the real getter, including its section argument. A dict.get fake
        # would silently accept defaults which ConfigManager interprets as sections.
        manager = object.__new__(config_loader.ConfigManager)
        manager.config = values
        with patch.object(config_loader, "CONFIG_MANAGER", manager):
            valid, error = runtime.configure_v2_runtime(
                Mock(), "InfiniDysk", self.service
            )
        self.assertTrue(valid, error)
        self.assertTrue(
            Path(values["data_root"], "infinidysk-v2", runtime.AUTH_FILE).is_file()
        )
        self.assertEqual(self.service["config_dir"], str(self.root / "legacy"))
        self.assertEqual(self.service["env"], {})
        self.assertEqual(
            self.service["command"], [str(self.root / "program/infinidysk")]
        )

    def test_prelaunch_replaces_stale_v1_command_and_excludes_legacy_provider_environment(
        self,
    ):
        from utils import processes

        self.installed()
        self.service.update(
            process_name="InfiniDysk",
            command=["legacy-v1-start"],
            suppress_logging=True,
        )
        self.service["env"] = {
            "DATABASE_PROVIDER": "postgres",
            "DATABASE_CONNECTION_STRING": "retained-private-value",
        }
        values = {
            "puid": os.getuid(),
            "pgid": os.getgid(),
            "infinidysk": self.service,
            "data_root": str(self.root),
            "rclone": {
                "instances": {
                    "one": {
                        "enabled": True,
                        "key_type": "infinidysk",
                        "mount_dir": str(self.root),
                        "mount_name": "mount",
                    }
                }
            },
        }
        handler = object.__new__(processes.ProcessHandler)
        with patch.object(processes.ProcessHandler, "_write_startup_state"):
            handler.init_attributes(Mock())
        handler.setup_tracker.add("InfiniDysk")

        def get(key, section=None):
            return values.get(section, {}).get(key) if section else values.get(key)

        def launch(command, **kwargs):
            self.assertEqual(command, [str(self.root / "program/infinidysk")])
            self.assertEqual(kwargs["cwd"], self.service["v2_data_dir"])
            self.assertEqual(kwargs["env"]["INFINIDYSK_LISTEN"], "0.0.0.0:3333")
            self.assertEqual(kwargs["env"]["INFINIDYSK__AUTH__MODE"], "required")
            self.assertNotIn("INFINIDYSK__Auth__Mode", kwargs["env"])
            self.assertNotIn("INFINIDYSK__Auth", kwargs["env"])
            self.assertNotIn("DATABASE_PROVIDER", kwargs["env"])
            self.assertNotIn("DATABASE_CONNECTION_STRING", kwargs["env"])
            self.assertGreater(len(kwargs["env"]["INFINIDYSK__AUTH__API_KEY"]), 32)
            raise RuntimeError("stopped before real process launch")

        with (
            patch.dict(
                os.environ,
                {
                    "INFINIDYSK__Auth__Mode": "local",
                    "INFINIDYSK__Auth": '{"mode":"local"}',
                },
            ),
            patch.object(processes.CONFIG_MANAGER, "get", side_effect=get),
            patch.object(
                processes.CONFIG_MANAGER, "get_instance", return_value=self.service
            ),
            patch.object(
                processes.CONFIG_MANAGER,
                "find_key_for_process",
                return_value=("infinidysk", None),
            ),
            patch.object(processes.subprocess, "Popen", side_effect=launch) as spawn,
            patch.object(processes, "notify_event"),
        ):
            valid, error = handler._start_process_admitted("InfiniDysk")
        self.assertFalse(valid)
        self.assertIn("stopped before real process launch", error)
        spawn.assert_called_once()

    @isolated_runtime_test
    def test_rclone_setup_preserves_custom_cache_and_uses_native_endpoint_without_rc_sync(
        self,
    ):
        from utils import dependencies, nzbdav_settings, riven_settings, setup

        runtime.native_auth(self.service, create=True)
        remote = self.root / "rclone/remote.conf"
        instance = {
            "enabled": True,
            "process_name": "Rclone InfiniDysk",
            "key_type": "infinidysk",
            "config_file": str(remote),
            "config_dir": str(remote.parent),
            "mount_name": "mount",
            "mount_dir": str(self.root),
            "cache_dir": str(self.root / "custom-cache"),
            "command": [
                "rclone",
                "mount",
                "mount:",
                str(self.root / "mount"),
                "--dir-cache-time=1w",
                "--vfs-cache-max-size=17G",
                "--no-modtime",
            ],
        }
        values = {
            "puid": os.geteuid() or 1000,
            "pgid": os.getegid() or 1000,
            "infinidysk": self.service,
            "rclone": {"instances": {"one": instance}},
            "data_root": str(self.root),
        }
        manager = SimpleNamespace(
            config=values,
            get=lambda key, section=None: (
                values.get(section, {}).get(key) if section else values.get(key)
            ),
            save_config=Mock(),
        )
        api = Mock()
        api.get_status.return_value = "stopped"
        with (
            patch.object(setup, "CONFIG_MANAGER", manager),
            patch.object(setup, "user_id", values["puid"]),
            patch.object(setup, "group_id", values["pgid"]),
            patch.object(setup, "fuse_config", return_value=(True, None)),
            patch.object(setup, "obscure_password", return_value="obscured-fixture"),
            patch.object(setup, "chown_single"),
            patch.object(setup, "chown_recursive"),
            patch.object(setup, "ensure_directory") as force_directory,
            patch.object(setup, "_select_rclone_rc_port", return_value=5572),
            patch.object(dependencies, "get_api_state", return_value=api),
            patch.object(riven_settings, "parse_config_keys"),
            patch.object(nzbdav_settings, "sync_nzbdav_rclone_rc") as sync,
        ):
            valid, error = setup.rclone_setup()
        self.assertTrue(valid, error)
        self.assertIn("url = http://127.0.0.1:3333/", remote.read_text())
        self.assertIn("--dir-cache-time=20s", instance["command"])
        self.assertIn("--vfs-cache-max-size=17G", instance["command"])
        self.assertIn("--links", instance["command"])
        self.assertIn("--no-modtime", instance["command"])
        self.assertIn(f"--uid={values['puid']}", instance["command"])
        self.assertIn(f"--gid={values['pgid']}", instance["command"])
        sync.assert_not_called()
        force_directory.assert_not_called()

    def test_native_foreign_mount_rejection_precedes_data_creation(self):
        self.installed()
        self.service["mount_backend"] = "fuse"
        with patch.object(
            topology,
            "mount_records",
            return_value=[
                {
                    "path": self.service["mount_path"],
                    "source": "foreign",
                    "filesystem": "fuse.rclone",
                }
            ],
        ):
            valid, error = runtime.prepare_v2_launch(self.service, 1000, 1000)
        self.assertFalse(valid)
        self.assertIn("occupied", error)
        self.assertFalse(Path(self.service["v2_data_dir"]).exists())

    def test_foreign_data_is_unchanged_and_v1_import_needs_explicit_server_authority(
        self,
    ):
        self.installed()
        data = Path(self.service["v2_data_dir"])
        data.mkdir()
        foreign = data / "foreign"
        foreign.write_bytes(b"retain foreign state")
        before = data.stat()
        with patch.object(runtime.os, "chown") as own:
            valid, error = runtime.prepare_v2_launch(self.service, 1000, 1000)
        self.assertFalse(valid)
        self.assertIn("foreign nonempty", error)
        own.assert_not_called()
        self.assertFalse((data / runtime.AUTH_FILE).exists())
        self.assertEqual(foreign.read_bytes(), b"retain foreign state")
        self.assertEqual(data.stat().st_uid, before.st_uid)
        foreign.unlink()
        (data / "db.sqlite").write_bytes(b"owned staged v1 fixture")
        with self.assertRaises(ValueError):
            runtime.native_auth(self.service, create=True)
        valid, error = runtime.prepare_v2_launch(
            self.service, os.getuid(), os.getgid(), allow_v1_import=True
        )
        self.assertTrue(valid, error)
        # The credential file does not authorize an ordinary uncontained import.
        valid, error = runtime.prepare_v2_launch(self.service, os.getuid(), os.getgid())
        self.assertFalse(valid)
        self.assertIn("contained candidate", error)

    def test_postgres_import_contract_needs_authority_and_native_restart_needs_owned_evidence(
        self,
    ):
        self.installed()
        data = Path(self.service["v2_data_dir"])
        data.mkdir()
        (data / "db-contract.json").write_text("{}")
        valid, error = runtime.prepare_v2_launch(
            self.service, os.getuid(), os.getgid(), allow_v1_import=True
        )
        self.assertTrue(valid, error)
        with patch.object(runtime.os, "chown") as own:
            valid, error = runtime.prepare_v2_launch(self.service, 1000, 1000)
        self.assertFalse(valid)
        own.assert_not_called()
        self.assertIn("contained candidate", error)
        # Native SQLite-to-PG cutover retains its former SQLite database and the
        # original v1 import contract. This is a legitimate cold restart.
        retired = data / "infinidysk.sqlite.pre-postgres"
        retired.write_bytes(b"retained native SQLite")
        (data / "database.toml").write_text(
            'url = "postgresql://localhost:5432/native"\n'
        )
        valid, error = runtime.prepare_v2_launch(self.service, os.getuid(), os.getgid())
        self.assertTrue(valid, error)
        (data / "database.toml").write_text('url = "sqlite://infinidysk.sqlite"\n')
        self.assertFalse(
            runtime.prepare_v2_launch(self.service, os.getuid(), os.getgid())[0]
        )
        retired.unlink()
        retired.symlink_to(self.root / "foreign-db")
        (data / "database.toml").write_text(
            'url = "postgresql://localhost:5432/native"\n'
        )
        self.assertFalse(
            runtime.prepare_v2_launch(self.service, os.getuid(), os.getgid())[0]
        )

    def test_native_shutdown_reports_leftover_mount_without_detaching_it(self):
        from utils import processes
        from utils import infinidysk_migration_admission as admission

        self.service["mount_backend"] = "fuse"
        handler = object.__new__(processes.ProcessHandler)
        handler.init_attributes(Mock())
        handler._prefixed_name = lambda name: name
        handler._signal_process_group = Mock(return_value=None)
        handler._process_group_alive = Mock(return_value=False)
        handler._update_running_processes_file = Mock()
        child = Mock(pid=4242)
        child.poll.return_value = 0
        handler.process_names = {"InfiniDysk": child}
        handler.processes = {
            child.pid: {"name": "InfiniDysk", "internal_name": "InfiniDysk"}
        }
        manager = Mock()
        manager.find_key_for_process.return_value = ("infinidysk", None)
        manager.get.side_effect = lambda key, section=None: (
            self.service if key == "infinidysk" and section is None else None
        )
        with (
            patch.object(processes, "CONFIG_MANAGER", manager),
            patch.object(admission, "infinidysk_v2_jobs_active", return_value=False),
            patch.object(
                admission, "infinidysk_v2_recovery_pending", return_value=False
            ),
            patch.object(
                topology,
                "mount_records",
                return_value=[
                    {
                        "path": self.service["mount_path"] + "/child",
                        "filesystem": "fuse",
                        "source": "infinidysk",
                    }
                ],
            ),
            patch.object(processes.subprocess, "run") as unmount,
        ):
            result = handler.stop_process("InfiniDysk")
        self.assertEqual(result[0], False)
        self.assertIn("mount remains occupied", result[1])
        self.assertNotIn("InfiniDysk", handler.process_names)
        unmount.assert_not_called()
        self.assertTrue(
            topology.mount_path_released(
                self.service["mount_path"], [{"path": "/elsewhere"}]
            )
        )
        self.assertFalse(
            topology.mount_path_released(
                self.service["mount_path"], [{"path": self.service["mount_path"]}]
            )
        )
        with patch.object(topology, "mount_records", side_effect=OSError("unreadable")):
            self.assertFalse(topology.mount_path_released(self.service["mount_path"]))

    @isolated_runtime_test
    def test_native_dependency_uses_actual_boot_provider_not_legacy_checkbox(self):
        from utils.dependency_map import build_conditional_dependency_map

        data_root = self.root / "custom-data-root"
        self.service.pop("v2_data_dir")
        data = Path(runtime.resolve_v2_data_dir(self.service, str(data_root)))
        data.mkdir(parents=True)
        config = {
            "infinidysk": self.service,
            "data_root": str(data_root),
            "postgres": {"enabled": True, "port": 5544},
        }
        for url, legacy_enabled, expected in (
            ("postgresql://127.0.0.1:5544/native", False, True),
            ("postgresql://[::1]:5544/native", False, True),
            ("postgresql://db.example.invalid:5544/native", True, False),
            ("postgresql://localhost:5432/native", True, False),
            ("sqlite://infinidysk.sqlite", True, False),
        ):
            with self.subTest(url=url):
                self.service["postgres_enabled"] = legacy_enabled
                (data / "database.toml").write_text("url = " + json.dumps(url) + "\n")
                deps = build_conditional_dependency_map(lambda key: config.get(key))
                self.assertEqual("postgres" in deps.get("infinidysk", set()), expected)

    @isolated_runtime_test
    def test_copying_journal_starts_managed_target_before_resume_but_health_stays_sqlite(
        self,
    ):
        from utils.dependency_map import build_conditional_dependency_map

        data = Path(self.service["v2_data_dir"])
        data.mkdir()
        config = {
            "infinidysk": self.service,
            "postgres": {"enabled": True, "port": 5544},
        }
        for state, target, expected in (
            ("copying", "postgresql://localhost:5544/native", True),
            ("syncing", "postgresql://localhost:5544/native", True),
            ("cutover", "postgresql://localhost:5544/native", True),
            ("copying", "postgresql://db.example.invalid:5544/native", False),
            ("cancelled", "postgresql://localhost:5544/native", False),
        ):
            with self.subTest(state=state, target=target):
                (data / "database.toml").write_text(
                    "[migration]\nstate = "
                    + json.dumps(state)
                    + "\ntarget = "
                    + json.dumps(target)
                    + "\n"
                )
                self.assertTrue(
                    runtime.native_database_url(self.service).startswith("sqlite:")
                )
                deps = build_conditional_dependency_map(lambda key: config.get(key))
                self.assertEqual("postgres" in deps.get("infinidysk", set()), expected)
        redirected = data / "database.toml"
        redirected.unlink()
        redirected.symlink_to(self.root / "foreign-journal")
        with self.assertRaises(OSError):
            runtime.native_database_dependency_urls(self.service)

    def test_native_provider_migration_blocks_updates_before_global_admission(self):
        from utils import auto_update
        from utils.infinidysk_v2_postgres import INFINIDYSK_V2_POSTGRES_MANAGER

        updater = auto_update.Update.__new__(auto_update.Update)
        updater.process_handler = Mock()
        updater.process_handler.processes = {
            22: {"name": "InfiniDysk", "process_obj": SimpleNamespace(returncode=None)}
        }
        manager = Mock()
        manager.find_key_for_process.return_value = ("infinidysk", None)
        manager.get_instance.return_value = self.service
        manager.get.side_effect = lambda key, section=None: (
            "/private-data" if key == "data_root" and section is None else None
        )
        with (
            patch.object(auto_update, "CONFIG_MANAGER", manager),
            patch.object(
                runtime,
                "native_api_request",
                return_value={
                    "status": 200,
                    "body": {"migration": {"state": "copying"}},
                },
            ) as api,
            patch.object(INFINIDYSK_V2_POSTGRES_MANAGER, "status") as status,
            patch.object(updater, "_auto_update_admitted") as automatic,
            patch.object(updater, "_manual_update_install_once_admitted") as manual,
        ):
            result, error = updater.auto_update("InfiniDysk", True)
            self.assertIsNone(result)
            self.assertIn("deferred", error)
            response = updater._manual_update_install_once("InfiniDysk")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(api.call_args.kwargs["data_root"], "/private-data")
        self.assertEqual(status.call_count, 2)
        automatic.assert_not_called()
        manual.assert_not_called()
        updater.process_handler.stop_process.assert_not_called()
        with patch.object(
            runtime, "native_api_request", side_effect=ValueError("private response")
        ):
            self.assertIn(
                "cannot be verified",
                updater._native_provider_update_blocker(
                    "InfiniDysk", "infinidysk", self.service
                ),
            )

    @isolated_runtime_test
    def test_native_provider_cold_resume_configures_only_verified_services(self):
        from contextlib import contextmanager
        from utils import setup
        from utils import infinidysk_migration_admission as admission
        from utils.infinidysk_v2_postgres import INFINIDYSK_V2_POSTGRES_MANAGER

        source = Path(setup.__file__).parents[1] / "main.py"
        tree = ast.parse(source.read_text())
        selected = ast.Module(
            body=[
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef)
                and node.name == "start_configured_process"
            ],
            type_ignores=[],
        )
        namespace = {
            "logger": Mock(),
            "config": {},
            "cli_battery_is_embedded": lambda *args: False,
        }
        exec(compile(selected, str(source), "exec"), namespace)  # noqa: S102
        updater = Mock()
        updater.process_handler.start_process.return_value = (True, None)
        active_owner = []

        @contextmanager
        def owner(kind):
            active_owner.append(kind)
            try:
                yield
            finally:
                active_owner.pop()

        def configured(handler, name):
            self.assertEqual(active_owner, ["postgres"])
            return True, None

        with (
            patch.object(
                INFINIDYSK_V2_POSTGRES_MANAGER, "has_blocking_job", return_value=True
            ),
            patch.object(
                INFINIDYSK_V2_POSTGRES_MANAGER,
                "resume_runtime_allowed",
                side_effect=lambda key, service: service.get("verified", False),
            ),
            patch.object(admission, "v2_migration_owner", side_effect=owner),
            patch.object(admission, "infinidysk_v2_jobs_active", return_value=False),
            patch.object(
                setup, "configure_project", side_effect=configured
            ) as configure,
        ):
            for key in ("infinidysk", "postgres"):
                self.assertTrue(
                    namespace["start_configured_process"](
                        {"enabled": True, "verified": True, "process_name": key},
                        updater,
                        key,
                    )
                )
            self.assertFalse(
                namespace["start_configured_process"](
                    {
                        "instances": {
                            "unrelated": {"enabled": True, "process_name": "foreign"},
                            "associated": {
                                "enabled": True,
                                "verified": True,
                                "process_name": "associated",
                            },
                        }
                    },
                    updater,
                    "rclone",
                )
            )
            self.assertFalse(
                namespace["start_configured_process"](
                    {"enabled": True, "process_name": "unverified"},
                    updater,
                    "infinidysk",
                )
            )
        self.assertEqual(
            [call.args[1] for call in configure.call_args_list],
            ["infinidysk", "postgres", "associated"],
        )
        self.assertEqual(
            [
                call.args[0]
                for call in updater.process_handler.start_process.call_args_list
            ],
            ["infinidysk", "postgres", "associated"],
        )
        updater.auto_update.assert_not_called()

    @unittest.skipUnless(os.geteuid() == 0, "requires root to test real UID transition")
    def test_uid_1000_can_rewrite_private_state_and_create_native_database(self):
        self.installed()
        valid, error = runtime.prepare_v2_launch(self.service, 1000, 1000)
        self.assertTrue(valid, error)
        data = self.service["v2_data_dir"]
        code = "from pathlib import Path; import sys; p=Path(sys.argv[1]); a=p/'.dumb-native-auth.json'; a.write_bytes(a.read_bytes()); (p/'infinidysk.sqlite').write_bytes(b'fixture')"
        child = subprocess.run(
            [sys.executable, "-c", code, data],
            user=1000,
            group=1000,
            extra_groups=[],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(Path(data, "infinidysk.sqlite").stat().st_uid, 1000)

    def test_native_api_is_loopback_only_handles_empty_and_redacts_errors(self):
        runtime.native_auth(self.service, create=True)
        response = Mock()
        response.code = 202
        response.read.return_value = b""
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(runtime.urllib.request, "build_opener", return_value=opener):
            result = runtime.native_api_request(
                self.service,
                "POST",
                "/api/v1/database/check",
                {"url": "postgres://private-value"},
            )
            self.assertEqual(result, {"status": 202, "body": None})
            self.assertEqual(
                runtime.native_api_request(self.service, "GET", "/healthz"),
                {"status": 202, "body": None},
            )
            self.assertEqual(
                opener.open.call_args.args[0].full_url,
                "http://127.0.0.1:3333/healthz",
            )
            opener.open.side_effect = OSError("private-value")
            with self.assertRaisesRegex(
                ValueError, "Native API request failed"
            ) as error:
                runtime.native_api_request(self.service, "GET", "/api/v1/database")
            self.assertNotIn("private-value", str(error.exception))
        for path in (
            "https://example.invalid/",
            "//example.invalid/api/v1/",
            "/api/v1/../other",
            "/api/v1/database?secret=x",
        ):
            with self.assertRaises(ValueError):
                runtime.native_api_request(self.service, "GET", path)


class NativeTopologyTests(unittest.TestCase):
    def test_delivery_readiness_rejects_foreign_fuse_and_missing_rclone_owner(self):
        service = {
            "runtime_generation": "2",
            "mount_backend": "fuse",
            "mount_path": "/mnt/native",
        }
        record = {"path": "/mnt/native", "filesystem": "fuse", "source": "infinidysk"}
        self.assertTrue(topology.delivery_mount_ready(service, {}, [record]))
        self.assertFalse(
            topology.delivery_mount_ready(
                service, {}, [{**record, "source": "foreign"}]
            )
        )
        service["mount_backend"] = "rclone"
        self.assertFalse(topology.delivery_mount_ready(service, {}, [record]))
        instance = {
            "enabled": True,
            "key_type": "infinidysk",
            "mount_dir": "/mnt",
            "mount_name": "native",
        }
        self.assertTrue(
            topology.delivery_mount_ready(
                service,
                {"one": instance},
                [{**record, "filesystem": "fuse.rclone", "source": "native:"}],
            )
        )
        self.assertFalse(
            topology.validate_selected_owners(
                service,
                {"one": instance, "foreign": {**instance, "key_type": "realdebrid"}},
            )[0]
        )

    def test_native_database_health_uses_boot_provider_and_custom_data_root(self):
        from utils import database_health, service_postgres

        with tempfile.TemporaryDirectory() as tmp:
            service = {
                "enabled": True,
                "runtime_generation": "2",
                "config_dir": "/retained-v1",
                "postgres_enabled": True,
                "env": {"DATABASE_PROVIDER": "postgres"},
            }
            data = Path(tmp, "infinidysk-v2")
            data.mkdir()
            collector = database_health.DatabaseHealthCollector.__new__(
                database_health.DatabaseHealthCollector
            )
            discovered = collector._discover_services(
                {"data_root": tmp, "infinidysk": service}
            )
            self.assertEqual(discovered[0]["provider"], "sqlite")
            self.assertEqual(
                collector._sqlite_paths(discovered[0]),
                [
                    ("main", str(data / "infinidysk.sqlite")),
                    ("metrics", str(data / "metrics.sqlite")),
                ],
            )
            (data / "database.toml").write_text(
                'url = "postgres://user:private-fixture@localhost/native"\n'
            )
            discovered = collector._discover_services(
                {"data_root": tmp, "infinidysk": service}
            )
            self.assertEqual(discovered[0]["provider"], "postgresql")
            connection = database_health._service_postgres_connection(
                "infinidysk", discovered[0]["service_config"], {"data_root": tmp}
            )
            self.assertEqual(connection["database"], "native")
            self.assertFalse(service_postgres.service_postgres_enabled(service))
            original = json.loads(json.dumps(service))
            self.assertFalse(
                service_postgres.apply_service_postgres_config(
                    "infinidysk", service, {}, "legacy", enabled=True
                )
            )
            self.assertEqual(service, original)

    def test_legacy_settings_hook_cannot_read_or_write_v1_database_for_native(self):
        from utils import nzbdav_settings

        with (
            patch.object(
                nzbdav_settings.CONFIG_MANAGER,
                "get",
                return_value={"runtime_generation": "2"},
            ),
            patch.object(nzbdav_settings, "_patch_nzbdav_config_admitted") as legacy,
        ):
            changed, error = nzbdav_settings.patch_nzbdav_config()
        self.assertFalse(changed)
        self.assertIn("native API/UI", error)
        legacy.assert_not_called()

    def test_native_fixed_source_status_never_fetches_v1_or_stops_on_bad_selection(
        self,
    ):
        from utils import auto_update

        updater = auto_update.Update.__new__(auto_update.Update)
        updater.process_handler = Mock()
        service = {
            "runtime_generation": "2",
            "v2_release_version": runtime.RELEASE,
            "release_version": "latest",
            "release_version_enabled": False,
        }
        self.assertEqual(
            updater._get_update_block_reason(service, "infinidysk"), "release"
        )
        self.assertEqual(
            updater._configured_target_label(service, "release"),
            f"release {runtime.RELEASE}",
        )
        with patch.object(
            runtime, "installed_v2_version", return_value=runtime.RELEASE
        ):
            status = updater._manual_check_generic_repo(
                "InfiniDysk",
                service,
                "infinidysk",
                None,
                "release",
                1,
                True,
                24,
                "00:00",
                2,
            )
        self.assertEqual(status["current_version"], runtime.RELEASE)
        self.assertEqual(status["available_version"], runtime.RELEASE)
        self.assertFalse(status["auto_update_enabled"])
        with patch.object(
            runtime, "install_v2_runtime", return_value=(False, "unverified selection")
        ):
            result = updater._install_configured_target(
                "InfiniDysk", service, "infinidysk", None, "release"
            )
        self.assertFalse(result[0])
        updater.process_handler.stop_process.assert_not_called()

    def test_native_suppresses_only_its_associated_rclone_without_mutating_config(self):
        service = {
            "runtime_generation": "2",
            "mount_backend": "fuse",
            "mount_path": "/mnt/native",
        }
        related = {
            "enabled": True,
            "key_type": "infinidysk",
            "mount_dir": "/mnt",
            "mount_name": "old",
        }
        foreign = {**related, "key_type": "realdebrid"}
        self.assertTrue(topology.suppress_rclone(service, related))
        self.assertFalse(topology.suppress_rclone(service, foreign))
        self.assertTrue(related["enabled"])
        self.assertEqual(
            topology.selected_topology(service, {"old": related})[0]["path"],
            "/mnt/native",
        )
        self.assertFalse(
            topology.suppress_rclone(
                {"runtime_generation": "1", "mount_backend": "fuse"}, related
            )
        )

    def test_external_mount_must_have_one_exact_path_owner(self):
        service = {
            "runtime_generation": "2",
            "mount_backend": "rclone",
            "mount_path": "/mnt/native",
        }
        related = {
            "enabled": True,
            "key_type": "infinidysk",
            "mount_dir": "/mnt",
            "mount_name": "native",
        }
        self.assertTrue(topology.validate_rclone_topology(service, {"one": related})[0])
        self.assertFalse(topology.validate_rclone_topology(service, {})[0])
        self.assertFalse(
            topology.validate_rclone_topology(
                service, {"one": related, "two": related}
            )[0]
        )
        self.assertFalse(
            topology.validate_rclone_topology(
                service, {"one": {**related, "mount_name": "other"}}
            )[0]
        )

    def test_redirected_nonempty_and_child_mount_targets_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "mount")
            target.mkdir()
            self.assertTrue(topology.validate_native_mount_target(target, [])[0])
            (target / "foreign").write_text("keep")
            self.assertFalse(topology.validate_native_mount_target(target, [])[0])
            self.assertEqual((target / "foreign").read_text(), "keep")
            (target / "foreign").unlink()
            self.assertFalse(
                topology.validate_native_mount_target(
                    target, [{"path": str(target / "nested")}]
                )[0]
            )
            redirected = Path(tmp, "redirect")
            redirected.symlink_to(target, target_is_directory=True)
            self.assertFalse(topology.validate_native_mount_target(redirected, [])[0])

    def test_setup_dispatch_never_enters_v1_source_or_postgres_paths(self):
        from utils import setup

        service = {"runtime_generation": "2"}
        manager = Mock()
        manager.find_key_for_process.return_value = ("infinidysk", None)
        manager.get_instance.return_value = service
        handler = Mock(setup_tracker=set(), setup_tracker_lock=threading.Lock())
        with (
            patch.object(setup, "CONFIG_MANAGER", manager),
            patch.object(
                runtime, "install_v2_runtime", return_value=(True, None)
            ) as install,
            patch.object(
                runtime, "configure_v2_runtime", return_value=(True, None)
            ) as configure,
            patch.object(setup, "_validate_infinidysk_postgres_source") as legacy,
        ):
            result = setup._setup_project_inner(handler, "InfiniDysk", True, True)
        self.assertEqual(result, (True, None))
        install.assert_called_once()
        configure.assert_called_once()
        legacy.assert_not_called()
        self.assertIn("InfiniDysk", handler.setup_tracker)

    def test_shared_startup_inventory_and_mounts_use_selected_owner(self):
        from utils import setup

        source = Path(setup.__file__).parents[1] / "main.py"
        tree = ast.parse(source.read_text())
        selected = ast.Module(
            body=[
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef)
                and node.name
                in {"_collect_expected_process_names", "_collect_mount_paths"}
            ],
            type_ignores=[],
        )
        values = {
            "infinidysk": {
                "enabled": True,
                "process_name": "InfiniDysk",
                "runtime_generation": "2",
                "mount_backend": "fuse",
                "mount_path": "/mnt/native",
            },
            "rclone": {
                "instances": {
                    "old": {
                        "enabled": True,
                        "process_name": "Rclone InfiniDysk",
                        "key_type": "infinidysk",
                        "mount_dir": "/mnt",
                        "mount_name": "native",
                    },
                    "other": {
                        "enabled": True,
                        "process_name": "Rclone Other",
                        "mount_dir": "/mnt",
                        "mount_name": "other",
                    },
                }
            },
        }
        manager = SimpleNamespace(
            get=lambda key, default=None: values.get(key, default)
        )
        namespace = {
            "os": os,
            "cli_battery_is_embedded": lambda *args: False,
            "_collect_decypharr_mount_paths": lambda *args: [],
        }
        exec(compile(selected, str(source), "exec"), namespace)  # noqa: S102
        self.assertEqual(
            namespace["_collect_mount_paths"](manager), ["/mnt/native", "/mnt/other"]
        )
        self.assertEqual(
            namespace["_collect_expected_process_names"](
                manager, ["infinidysk", "rclone"]
            ),
            ["InfiniDysk", "Rclone Other"],
        )
