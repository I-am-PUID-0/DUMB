import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils import nzbdav_settings, setup


class InfiniDyskSetupTests(unittest.TestCase):
    def test_postgres_install_only_refuses_a_ready_v11_runtime(self):
        config = {
            "process_name": "InfiniDysk",
            "config_dir": "/infinidysk",
            "repo_owner": "infinidysk",
            "repo_name": "infinidysk",
            "release_version_enabled": False,
            "release_version": "latest",
            "postgres_enabled": True,
            "env": {"DATABASE_PROVIDER": "postgres"},
        }

        with (
            patch.object(
                setup.CONFIG_MANAGER,
                "get",
                side_effect=lambda key, default=None: (
                    config if key == "infinidysk" else (default or {})
                ),
            ),
            patch.object(
                setup, "_nzbdav_installed_runtime_ready", return_value=(True, None)
            ),
            patch.object(
                setup,
                "validate_infinidysk_postgres_installed_version",
                return_value=(False, "installed runtime is below v1.2.0"),
            ) as validate_runtime,
            patch.object(setup, "chown_single") as chown,
        ):
            success, error = setup.setup_nzbdav(Mock(), install_only=True)

        self.assertFalse(success)
        self.assertIn("below v1.2.0", error)
        validate_runtime.assert_called_once_with("/infinidysk")
        chown.assert_not_called()

    def test_namespace_paths_default_to_infinidysk_for_new_configs(self):
        config = {"config_dir": "/infinidysk"}
        self.assertEqual(
            "/mnt/debrid/infinidysk-symlinks",
            nzbdav_settings._infinidysk_symlink_root(config),
        )
        self.assertEqual(
            "/mnt/debrid/infinidysk",
            nzbdav_settings._infinidysk_mount_root(config),
        )

    def test_namespace_paths_retain_nzbdav_for_existing_configs(self):
        config = {
            "config_dir": "/nzbdav",
            "symlink_backup_roots": ["/mnt/debrid/nzbdav-symlinks"],
        }
        self.assertEqual(
            "/mnt/debrid/nzbdav-symlinks",
            nzbdav_settings._infinidysk_symlink_root(config),
        )
        self.assertEqual(
            "/mnt/debrid/nzbdav",
            nzbdav_settings._infinidysk_mount_root(config),
        )

    def test_postgres_refuses_an_existing_sqlite_main_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "db.sqlite").write_bytes(b"existing application data")

            safe, error = setup._validate_infinidysk_postgres_fresh_install(
                temp_dir, True
            )

        self.assertFalse(safe)
        self.assertIn("supported only for fresh installations", error)
        self.assertIn("db.sqlite", error)

    def test_postgres_allows_existing_auxiliary_sqlite_stores(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for filename in ("metrics.sqlite", "warden.db", "usenet-migration.db"):
                Path(temp_dir, filename).write_bytes(b"auxiliary state")

            safe, error = setup._validate_infinidysk_postgres_fresh_install(
                temp_dir, True
            )

        self.assertTrue(safe, error)
        self.assertIsNone(error)

    def test_rejected_postgres_switch_does_not_mutate_runtime_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "db.sqlite").write_bytes(b"existing application data")
            config = {
                "process_name": "InfiniDysk",
                "config_dir": temp_dir,
                "repo_owner": "infinidysk",
                "repo_name": "infinidysk",
                "postgres_enabled": True,
                "env": {"CONFIG_PATH": temp_dir},
            }
            config_manager = Mock()
            config_manager.get.side_effect = lambda key, default=None: (
                config if key == "infinidysk" else default
            )

            with (
                patch.object(setup, "CONFIG_MANAGER", config_manager),
                patch.object(setup, "chown_single"),
                patch.object(setup, "_chown_recursive_if_needed"),
                patch.object(setup, "apply_service_postgres_config") as apply_config,
            ):
                success, error = setup.setup_nzbdav(Mock())

        self.assertFalse(success)
        self.assertIn("supported only for fresh installations", error)
        apply_config.assert_not_called()
        self.assertNotIn("DATABASE_PROVIDER", config["env"])
        self.assertNotIn("DATABASE_CONNECTION_STRING", config["env"])

    def test_postgres_start_rejects_installed_v11_before_environment_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "version.txt").write_text(
                "v1.1.9-deadbeef", encoding="utf-8"
            )
            config = {
                "process_name": "InfiniDysk",
                "config_dir": temp_dir,
                "postgres_enabled": True,
                "release_version_enabled": False,
                "release_version": "latest",
                "env": {"CONFIG_PATH": temp_dir},
            }
            config_manager = Mock()
            config_manager.get.side_effect = lambda key, default=None: (
                config if key == "infinidysk" else default
            )

            with (
                patch.object(setup, "CONFIG_MANAGER", config_manager),
                patch.object(
                    setup,
                    "_nzbdav_installed_runtime_ready",
                    return_value=(True, None),
                ),
                patch.object(setup, "chown_single") as chown,
                patch.object(setup, "apply_service_postgres_config") as apply_config,
            ):
                success, error = setup.setup_nzbdav(Mock())

        self.assertFalse(success)
        self.assertIn("v1.2.0-or-newer", error)
        chown.assert_not_called()
        apply_config.assert_not_called()
        self.assertNotIn("DATABASE_PROVIDER", config["env"])
        self.assertNotIn("DATABASE_CONNECTION_STRING", config["env"])

    def test_guarded_migration_defers_live_arr_calls_but_persists_provider_config(
        self,
    ):
        config_manager = Mock()
        config_manager.get.return_value = {
            "process_name": "InfiniDysk",
            "config_dir": "/infinidysk",
            "backend_port": 8080,
            "env": {},
        }
        values = {
            "api.key": "provider-key",
            "arr.instances": "{}",
            "api.categories": "",
            "rclone.mount-dir": "",
        }

        with (
            patch.object(nzbdav_settings, "CONFIG_MANAGER", config_manager),
            patch.object(
                nzbdav_settings,
                "_collect_arr_entries",
                return_value=(
                    [
                        {
                            "Host": "http://127.0.0.1:7878",
                            "ApiKey": "arr-key",
                            "Category": "movies",
                            "Instance": "InfiniDysk",
                        }
                    ],
                    [],
                    [],
                    [],
                ),
            ),
            patch.object(nzbdav_settings, "_instance_core_services", return_value=[]),
            patch.object(nzbdav_settings, "_ensure_symlink_roots"),
            patch.object(
                nzbdav_settings.nzbdav_db,
                "get_config_value",
                side_effect=lambda name: values.get(name),
            ),
            patch.object(
                nzbdav_settings.nzbdav_db,
                "set_config_value",
                return_value=(True, None),
            ) as setter,
            patch.object(nzbdav_settings, "_wait_for_arr") as wait_for_arr,
            patch.object(nzbdav_settings, "_wait_for_nzbdav") as wait_for_provider,
        ):
            with nzbdav_settings.defer_nzbdav_runtime_integrations():
                updated, error = nzbdav_settings.patch_nzbdav_config()

        self.assertTrue(updated)
        self.assertIsNone(error)
        wait_for_arr.assert_not_called()
        wait_for_provider.assert_not_called()
        self.assertIn(
            ("rclone.mount-dir", "/mnt/debrid/infinidysk"),
            [item.args for item in setter.call_args_list],
        )

    def test_config_patch_refuses_external_sync_during_active_migration(self):
        with (
            patch(
                "utils.infinidysk_migration_admission.infinidysk_postgres_migration_active",
                return_value=True,
            ),
            patch(
                "utils.infinidysk_migration_admission.infinidysk_namespace_migration_active",
                return_value=False,
            ),
            patch.object(nzbdav_settings, "_patch_nzbdav_config_admitted") as admitted,
        ):
            updated, error = nzbdav_settings.patch_nzbdav_config()

        self.assertFalse(updated)
        self.assertIn("guarded migration job is active", error)
        admitted.assert_not_called()

    def test_config_patch_releases_admission_lock_before_live_dependency_waits(self):
        contender_result = []

        def admitted():
            from utils.infinidysk_migration_admission import (
                INFINIDYSK_MIGRATION_ADMISSION_LOCK,
            )

            def contend_for_lock():
                acquired = INFINIDYSK_MIGRATION_ADMISSION_LOCK.acquire(timeout=0.5)
                contender_result.append(acquired)
                if acquired:
                    INFINIDYSK_MIGRATION_ADMISSION_LOCK.release()

            contender = threading.Thread(target=contend_for_lock)
            contender.start()
            contender.join(timeout=1)
            return True, None

        with (
            patch(
                "utils.infinidysk_migration_admission.infinidysk_postgres_migration_active",
                return_value=False,
            ),
            patch(
                "utils.infinidysk_migration_admission.infinidysk_namespace_migration_active",
                return_value=False,
            ),
            patch(
                "utils.infinidysk_migration_admission.reserve_infinidysk_external_mutation",
                return_value="mutation-token",
            ) as reserve,
            patch(
                "utils.infinidysk_migration_admission.release_infinidysk_external_mutation"
            ) as release,
            patch(
                "utils.service_postgres.infinidysk_postgres_migration_authorized",
                return_value=False,
            ),
            patch.object(
                nzbdav_settings,
                "_patch_nzbdav_config_admitted",
                side_effect=admitted,
            ),
        ):
            updated, error = nzbdav_settings.patch_nzbdav_config()

        self.assertTrue(updated)
        self.assertIsNone(error)
        self.assertEqual([True], contender_result)
        reserve.assert_called_once_with("InfiniDysk configuration synchronization")
        release.assert_called_once_with("mutation-token")

    def test_config_patch_skips_duplicate_concurrent_synchronization(self):
        started = threading.Event()
        finish = threading.Event()
        first_result = []

        def admitted():
            started.set()
            self.assertTrue(finish.wait(timeout=2))
            return True, None

        with (
            patch(
                "utils.infinidysk_migration_admission.infinidysk_postgres_migration_active",
                return_value=False,
            ),
            patch(
                "utils.infinidysk_migration_admission.infinidysk_namespace_migration_active",
                return_value=False,
            ),
            patch(
                "utils.infinidysk_migration_admission.reserve_infinidysk_external_mutation",
                return_value="mutation-token",
            ),
            patch(
                "utils.infinidysk_migration_admission.release_infinidysk_external_mutation"
            ) as release,
            patch(
                "utils.service_postgres.infinidysk_postgres_migration_authorized",
                return_value=False,
            ),
            patch.object(
                nzbdav_settings,
                "_patch_nzbdav_config_admitted",
                side_effect=admitted,
            ) as synchronize,
        ):
            first = threading.Thread(
                target=lambda: first_result.append(
                    nzbdav_settings.patch_nzbdav_config()
                )
            )
            first.start()
            self.assertTrue(started.wait(timeout=1))
            duplicate_result = nzbdav_settings.patch_nzbdav_config()
            finish.set()
            first.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertEqual([(True, None)], first_result)
        self.assertEqual((False, None), duplicate_result)
        synchronize.assert_called_once_with()
        release.assert_called_once_with("mutation-token")

    def test_namespace_owned_config_patch_is_admitted_by_narrow_context(self):
        with (
            patch(
                "utils.infinidysk_migration_admission.infinidysk_postgres_migration_active",
                return_value=False,
            ),
            patch(
                "utils.infinidysk_migration_admission.infinidysk_namespace_migration_active",
                return_value=True,
            ),
            patch.object(
                nzbdav_settings,
                "_patch_nzbdav_config_admitted",
                return_value=(True, None),
            ) as admitted,
        ):
            with nzbdav_settings.defer_nzbdav_runtime_integrations():
                updated, error = nzbdav_settings.patch_nzbdav_config()

        self.assertTrue(updated)
        self.assertIsNone(error)
        admitted.assert_called_once_with()

    def test_postgres_config_patch_uses_api_without_opening_main_sqlite(self):
        config = {
            "process_name": "InfiniDysk",
            "config_dir": "/infinidysk",
            "backend_port": 8080,
            "postgres_enabled": True,
            "env": {
                "DATABASE_PROVIDER": "postgres",
                "FRONTEND_BACKEND_API_KEY": "provider-key",
            },
        }
        config_manager = Mock()
        config_manager.get.side_effect = lambda key, default=None: (
            config if key == "infinidysk" else default
        )
        api_calls = [
            (
                {
                    "configItems": [
                        {"configName": "arr.instances", "configValue": "{}"},
                        {"configName": "api.categories", "configValue": ""},
                        {"configName": "rclone.mount-dir", "configValue": ""},
                    ]
                },
                None,
            ),
            ({"status": True}, None),
        ]

        with (
            patch.object(nzbdav_settings, "CONFIG_MANAGER", config_manager),
            patch.object(
                nzbdav_settings,
                "_collect_arr_entries",
                return_value=(
                    [
                        {
                            "Host": "http://127.0.0.1:7878",
                            "ApiKey": "arr-key",
                            "Category": "movies",
                            "Instance": "Movies",
                        }
                    ],
                    [],
                    [],
                    [],
                ),
            ),
            patch.object(nzbdav_settings, "_instance_core_services", return_value=[]),
            patch.object(nzbdav_settings, "_ensure_symlink_roots"),
            patch.object(
                nzbdav_settings,
                "_infinidysk_config_api_request",
                side_effect=api_calls,
            ) as api_request,
            patch.object(
                nzbdav_settings.nzbdav_db,
                "get_config_value",
                side_effect=AssertionError("PostgreSQL mode must not read db.sqlite"),
            ),
            patch.object(
                nzbdav_settings.nzbdav_db,
                "set_config_value",
                side_effect=AssertionError("PostgreSQL mode must not write db.sqlite"),
            ),
        ):
            with nzbdav_settings.defer_nzbdav_runtime_integrations():
                updated, error = nzbdav_settings.patch_nzbdav_config()

        self.assertTrue(updated)
        self.assertIsNone(error)
        self.assertEqual(api_request.call_count, 2)
        self.assertEqual(api_request.call_args_list[0].args[1], "/api/get-config")
        self.assertEqual(api_request.call_args_list[1].args[1], "/api/update-config")
        updates = dict(api_request.call_args_list[1].args[2])
        self.assertEqual(updates["rclone.mount-dir"], "/mnt/debrid/infinidysk")
        self.assertEqual(updates["api.categories"], "movies,music,tv,whisparr")

    @staticmethod
    def _write_nzbdav_source(root: Path) -> None:
        backend = root / "backend"
        backend.mkdir(parents=True)
        (backend / "NzbWebDAV.csproj").write_text(
            "<Project><PropertyGroup><TargetFramework>net10.0</TargetFramework>"
            "</PropertyGroup></Project>",
            encoding="utf-8",
        )
        (root / "version.txt").write_text("main-12345678\n", encoding="utf-8")

    @staticmethod
    def _write_nzbdav_publish(output_dir: str, hashing_version: str) -> None:
        output = Path(output_dir)
        output.mkdir(parents=True)
        (output / "NzbWebDAV.dll").write_text("backend", encoding="utf-8")
        (output / "NzbWebDAV.deps.json").write_text("{}", encoding="utf-8")
        (output / "NzbWebDAV.runtimeconfig.json").write_text(
            '{"runtimeOptions":{"frameworks":['
            '{"name":"Microsoft.AspNetCore.App","version":"10.0.0"}]}}',
            encoding="utf-8",
        )
        (output / "System.IO.Hashing.dll").write_text(hashing_version, encoding="utf-8")

    def test_source_patch_adds_missing_system_io_hashing_reference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = root / "backend"
            par2 = backend / "Par2Recovery"
            reed_solomon = par2 / "ReedSolomon"
            reed_solomon.mkdir(parents=True)
            project_path = backend / "NzbWebDAV.csproj"
            project_path.write_bytes(
                b"\xef\xbb\xbf"
                b"<Project>\n  <ItemGroup>\n"
                b'      <PackageReference Include="MemoryPack" />\n'
                b"  </ItemGroup>\n</Project>\n"
            )
            (root / "Directory.Packages.props").write_text(
                '<Project><ItemGroup><PackageVersion Include="System.IO.Hashing" '
                'Version="10.0.11" /></ItemGroup></Project>',
                encoding="utf-8",
            )
            (par2 / "Par2TestEncoder.cs").write_text(
                "using System.IO.Hashing;\n", encoding="utf-8"
            )
            (reed_solomon / "Par2Reconstructor.cs").write_text(
                "using System.IO.Hashing;\n", encoding="utf-8"
            )

            self.assertTrue(
                setup._nzbdav_system_io_hashing_reference_patch_needed(project_path)
            )
            patched, error = setup._patch_nzbdav_system_io_hashing_reference(
                project_path
            )
            patched_again, second_error = (
                setup._patch_nzbdav_system_io_hashing_reference(project_path)
            )

            self.assertTrue(patched, error)
            self.assertTrue(patched_again, second_error)
            project_bytes = project_path.read_bytes()
            self.assertTrue(project_bytes.startswith(b"\xef\xbb\xbf"))
            project_contents = project_bytes.decode("utf-8-sig")
            self.assertEqual(
                1,
                project_contents.count('PackageReference Include="System.IO.Hashing"'),
            )
            self.assertFalse(
                setup._nzbdav_system_io_hashing_reference_patch_needed(project_path)
            )

    def test_source_patch_refuses_unversioned_system_io_hashing_dependency(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = root / "backend"
            par2 = backend / "Par2Recovery"
            par2.mkdir(parents=True)
            project_path = backend / "NzbWebDAV.csproj"
            original = (
                "<Project><ItemGroup>\n"
                '      <PackageReference Include="MemoryPack" />\n'
                "</ItemGroup></Project>"
            )
            project_path.write_text(original, encoding="utf-8")
            (root / "Directory.Packages.props").write_text(
                "<Project><ItemGroup /></Project>", encoding="utf-8"
            )
            (par2 / "Par2TestEncoder.cs").write_text(
                "using System.IO.Hashing;\n", encoding="utf-8"
            )

            patched, error = setup._patch_nzbdav_system_io_hashing_reference(
                project_path
            )

            self.assertFalse(patched)
            self.assertIn("refusing to guess", error)
            self.assertEqual(original, project_path.read_text(encoding="utf-8"))

    def test_configure_exposes_release_version_without_internal_commit_suffix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            version_path = Path(tmpdir) / "version.txt"
            version_path.write_text("v0.10.0-0dec23ac\n", encoding="utf-8")
            config = {
                "enabled": True,
                "process_name": "InfiniDysk",
                "config_dir": tmpdir,
                "webdav_password": "configured-password",
                "backend_port": 8080,
                "frontend_port": 3000,
                "log_level": "INFO",
                "env": {},
            }
            config_manager = Mock()
            config_manager.find_key_for_process.return_value = ("infinidysk", None)
            config_manager.get_instance.return_value = config
            process_handler = Mock()
            process_handler.setup_tracker = set()
            process_handler.setup_tracker_lock = threading.Lock()

            with (
                patch.object(setup, "CONFIG_MANAGER", config_manager),
                patch.object(setup, "setup_nzbdav", return_value=(True, None)),
            ):
                success, error = setup._setup_project_inner(
                    process_handler,
                    "InfiniDysk",
                    install_phase=False,
                    configure_phase=True,
                )

        self.assertTrue(success, error)
        self.assertEqual("v0.10.0", config["env"]["NZBDAV_VERSION"])
        self.assertEqual(
            {
                "name": "DUMB",
                "url": "https://dumbarr.com",
                "disabledFeatures": [],
            },
            json.loads(config["env"]["SERVICE_PROVIDER"]),
        )

    def test_fresh_prerelease_install_handles_comparison_error_without_type_crash(self):
        config = {
            "process_name": "InfiniDysk",
            "config_dir": "/nzbdav",
            "release_version_enabled": True,
            "release_version": "prerelease",
            "auto_update": False,
            "branch_enabled": False,
            "commit_sha": "",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("infinidysk", None)
        config_manager.get_instance.return_value = config
        process_handler = Mock()
        process_handler.setup_tracker = set()
        process_handler.setup_tracker_lock = threading.Lock()

        with (
            patch.object(setup, "CONFIG_MANAGER", config_manager),
            patch.object(
                setup.versions,
                "compare_versions",
                return_value=(False, "No prerelease versions found."),
            ),
            patch.object(
                setup.versions,
                "version_check",
                return_value=(None, "version marker missing"),
            ),
            patch.object(
                setup, "setup_release_version", return_value=(True, None)
            ) as install_release,
            patch.object(setup, "setup_nzbdav", return_value=(True, None)),
        ):
            success, error = setup._setup_project_inner(
                process_handler,
                "InfiniDysk",
                install_phase=True,
                configure_phase=False,
            )

        self.assertTrue(success, error)
        install_release.assert_called_once_with(
            process_handler, config, "InfiniDysk", "infinidysk"
        )

    def test_disabled_named_channel_keeps_verified_installed_runtime_on_startup(self):
        config = {
            "process_name": "InfiniDysk",
            "config_dir": "/infinidysk",
            "release_version_enabled": True,
            "release_version": "rc",
            "auto_update": False,
            "branch_enabled": False,
            "commit_sha": "",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("infinidysk", None)
        config_manager.get_instance.return_value = config
        process_handler = Mock()
        process_handler.setup_tracker = set()
        process_handler.setup_tracker_lock = threading.Lock()

        with (
            patch.object(setup, "CONFIG_MANAGER", config_manager),
            patch.object(
                setup,
                "_nzbdav_installed_runtime_ready",
                return_value=(True, None),
            ),
            patch.object(setup, "setup_release_version") as install_release,
            patch.object(setup, "setup_nzbdav") as setup_runtime,
        ):
            success, error = setup._setup_project_inner(
                process_handler,
                "InfiniDysk",
                install_phase=True,
                configure_phase=False,
            )

        self.assertTrue(success, error)
        install_release.assert_not_called()
        setup_runtime.assert_not_called()

    def test_disabled_named_channel_reinstalls_when_runtime_is_incomplete(self):
        config = {
            "process_name": "InfiniDysk",
            "config_dir": "/infinidysk",
            "release_version_enabled": True,
            "release_version": "rc",
            "auto_update": False,
            "branch_enabled": False,
            "commit_sha": "",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("infinidysk", None)
        config_manager.get_instance.return_value = config
        process_handler = Mock()
        process_handler.setup_tracker = set()
        process_handler.setup_tracker_lock = threading.Lock()

        with (
            patch.object(setup, "CONFIG_MANAGER", config_manager),
            patch.object(
                setup,
                "_nzbdav_installed_runtime_ready",
                return_value=(False, "frontend server missing"),
            ),
            patch.object(
                setup, "setup_release_version", return_value=(True, None)
            ) as install_release,
            patch.object(setup, "setup_nzbdav", return_value=(True, None)),
        ):
            success, error = setup._setup_project_inner(
                process_handler,
                "InfiniDysk",
                install_phase=True,
                configure_phase=False,
            )

        self.assertTrue(success, error)
        install_release.assert_called_once_with(
            process_handler, config, "InfiniDysk", "infinidysk"
        )

    def test_official_prerelease_selector_resolves_to_rc_channel(self):
        config = {
            "repo_owner": "infinidysk",
            "repo_name": "infinidysk",
        }
        with (
            patch.object(
                setup.downloader,
                "get_ref_commit_sha",
                return_value=("a" * 40, None),
            ) as resolve_ref,
            patch.object(setup.downloader, "get_latest_release") as latest,
        ):
            resolved, error = setup._resolve_nzbdav_release_selector(
                config, "prerelease"
            )

        self.assertIsNone(error)
        self.assertEqual("rc", resolved)
        resolve_ref.assert_called_once_with("infinidysk", "infinidysk", "rc")
        latest.assert_not_called()

    def test_postgres_fixed_v11_release_is_rejected_before_filesystem_mutation(self):
        config = {
            "process_name": "InfiniDysk",
            "config_dir": "/infinidysk",
            "release_version_enabled": True,
            "release_version": "v1.1.0",
            "postgres_enabled": True,
            "repo_owner": "infinidysk",
            "repo_name": "infinidysk",
        }
        with patch.object(setup.os, "makedirs") as make_dirs:
            success, error = setup.setup_release_version(
                Mock(), config, "InfiniDysk", "infinidysk"
            )

        self.assertFalse(success)
        self.assertIn("v1.2.0-or-newer", error)
        make_dirs.assert_not_called()

    def test_postgres_latest_resolving_below_v12_is_rejected_before_install(self):
        config = {
            "process_name": "InfiniDysk",
            "config_dir": "/infinidysk",
            "release_version_enabled": True,
            "release_version": "latest",
            "postgres_enabled": True,
            "repo_owner": "infinidysk",
            "repo_name": "infinidysk",
        }
        with (
            patch.object(
                setup.downloader,
                "get_latest_release",
                return_value=("v1.1.9", None),
            ),
            patch.object(setup.os, "makedirs") as make_dirs,
            patch.object(setup.downloader, "get_ref_commit_sha") as resolve_commit,
        ):
            success, error = setup.setup_release_version(
                Mock(), config, "InfiniDysk", "infinidysk"
            )

        self.assertFalse(success)
        self.assertIn("v1.2.0-or-newer", error)
        make_dirs.assert_not_called()
        resolve_commit.assert_not_called()

    def test_postgres_branch_install_is_rejected_before_source_download(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = {
                "process_name": "InfiniDysk",
                "config_dir": temp_dir,
                "branch_enabled": True,
                "branch": "main",
                "commit_sha": "",
                "postgres_enabled": True,
                "repo_owner": "infinidysk",
                "repo_name": "infinidysk",
            }
            with (
                patch.object(
                    setup, "infinidysk_postgres_runtime_floor", return_value=None
                ),
                patch.object(setup.os, "makedirs") as make_dirs,
                patch.object(setup.downloader, "get_branch") as download_branch,
            ):
                success, error = setup.setup_branch_version(
                    Mock(), config, "InfiniDysk", "infinidysk"
                )

        self.assertFalse(success)
        self.assertIn("v1.2.0-or-newer", error)
        make_dirs.assert_not_called()
        download_branch.assert_not_called()

    def test_configure_only_rejects_incomplete_runtime_before_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = {
                "process_name": "InfiniDysk",
                "config_dir": temp_dir,
                "release_version_enabled": False,
                "release_version": "latest",
                "postgres_enabled": True,
                "repo_owner": "infinidysk",
                "repo_name": "infinidysk",
                "env": {"CONFIG_PATH": temp_dir, "DATABASE_PROVIDER": "postgres"},
            }
            with (
                patch.object(
                    setup.CONFIG_MANAGER,
                    "get",
                    side_effect=lambda key, default=None: (
                        config if key == "infinidysk" else (default or {})
                    ),
                ),
                patch.object(
                    setup,
                    "_validate_infinidysk_postgres_source",
                    return_value=(True, None),
                ),
                patch.object(
                    setup,
                    "_nzbdav_installed_runtime_ready",
                    return_value=(
                        False,
                        "InfiniDysk frontend directory was not found.",
                    ),
                ),
                patch.object(setup, "chown_single") as chown,
                patch.object(setup, "apply_service_postgres_config") as apply_config,
                patch.object(setup, "_install_nzbdav_prebuilt_release") as install,
            ):
                success, error = setup.setup_nzbdav(Mock(), configure_only=True)

        self.assertFalse(success)
        self.assertIn("requires the install phase", error)
        self.assertIn("frontend directory was not found", error)
        chown.assert_not_called()
        apply_config.assert_not_called()
        install.assert_not_called()

    def test_install_only_repairs_incomplete_stable_runtime_from_prebuilt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            benchmark = root / "backend.Benchmarks" / "NzbWebDAV.Benchmarks.csproj"
            benchmark.parent.mkdir()
            benchmark.write_text("<Project />", encoding="utf-8")
            config = {
                "process_name": "InfiniDysk",
                "config_dir": temp_dir,
                "release_version_enabled": False,
                "release_version": "latest",
                "postgres_enabled": False,
                "repo_owner": "infinidysk",
                "repo_name": "infinidysk",
                "env": {"CONFIG_PATH": temp_dir},
            }
            with (
                patch.object(
                    setup.CONFIG_MANAGER,
                    "get",
                    side_effect=lambda key, default=None: (
                        config if key == "infinidysk" else (default or {})
                    ),
                ),
                patch.object(
                    setup,
                    "_validate_infinidysk_postgres_source",
                    return_value=(True, None),
                ),
                patch.object(
                    setup,
                    "_nzbdav_installed_runtime_ready",
                    return_value=(
                        False,
                        "InfiniDysk frontend directory was not found.",
                    ),
                ),
                patch.object(
                    setup,
                    "_nzbdav_prebuilt_runtime_ready",
                    return_value=(
                        False,
                        "critical file is missing: frontend/dist-node/server.js",
                    ),
                ),
                patch.object(
                    setup,
                    "_resolve_nzbdav_release_selector",
                    return_value=("v1.2.1", None),
                ),
                patch.object(
                    setup,
                    "_install_nzbdav_prebuilt_release",
                    return_value=({"version_marker": "v1.2.1-1fbed04a"}, None),
                ) as install,
                patch.object(
                    setup, "apply_service_postgres_config", return_value=False
                ),
                patch.object(
                    setup,
                    "_validate_infinidysk_postgres_fresh_install",
                    return_value=(True, None),
                ),
                patch.object(
                    setup,
                    "_normalize_nzbdav_writable_ownership",
                    return_value=(True, None),
                ),
                patch.object(setup, "_chown_recursive_if_needed"),
                patch.object(setup, "chown_single"),
                patch.object(setup.versions, "version_write") as version_write,
            ):
                success, error = setup.setup_nzbdav(Mock(), install_only=True)

        self.assertTrue(success, error)
        install.assert_called_once()
        version_write.assert_called_once()

    def test_source_discovery_ignores_benchmark_and_library_projects(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for project in (
                root / "backend.Benchmarks" / "NzbWebDAV.Benchmarks.csproj",
                root / "libs" / "NzbWebDAV.Models.csproj",
                root / "tests" / "NzbWebDAV.Tests.csproj",
            ):
                project.parent.mkdir(parents=True, exist_ok=True)
                project.write_text("<Project />", encoding="utf-8")

            found_backend, error = setup._find_nzbdav_backend_project(str(root), {})

        self.assertIsNone(found_backend)
        self.assertIn("No application .csproj", error)

    def test_artifact_restore_staging_resolves_data_root_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_root = root / "data"
            target = data_root / "nzbdav"
            target.mkdir(parents=True)
            link = root / "nzbdav"
            link.symlink_to(target, target_is_directory=True)

            staging_parent = setup._same_filesystem_staging_parent(str(link))

        self.assertEqual(str(data_root), staging_parent)

    def test_source_discovery_prefers_known_layout_without_walking_runtime_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend_project = root / "backend" / "NzbWebDAV.csproj"
            backend_project.parent.mkdir()
            backend_project.write_text("<Project />", encoding="utf-8")
            frontend_dir = root / "frontend"
            frontend_dir.mkdir()
            (frontend_dir / "package.json").write_text("{}", encoding="utf-8")
            (root / "blobs" / "aa" / "bb").mkdir(parents=True)

            with patch.object(
                setup.os,
                "walk",
                side_effect=AssertionError("known layouts must not be walked"),
            ):
                found_backend, error = setup._find_nzbdav_backend_project(str(root), {})
                found_frontend = setup._find_nzbdav_frontend_dir(str(root), {})

        self.assertIsNone(error)
        self.assertEqual(str(backend_project), found_backend)
        self.assertEqual(str(frontend_dir), found_frontend)

    def test_source_discovery_fallback_prunes_runtime_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend_project = root / "source" / "server" / "Fork.csproj"
            backend_project.parent.mkdir(parents=True)
            backend_project.write_text("<Project />", encoding="utf-8")
            frontend_dir = root / "source" / "ui"
            frontend_dir.mkdir()
            (frontend_dir / "package.json").write_text("{}", encoding="utf-8")

            runtime_dir = root / "blobs" / "aa" / "bb"
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "Runtime.csproj").write_text("<Project />", encoding="utf-8")
            (runtime_dir / "package.json").write_text("{}", encoding="utf-8")

            real_scandir = os.scandir

            def guarded_scandir(path):
                if Path(path) == root / "blobs":
                    raise AssertionError("runtime data must not be traversed")
                return real_scandir(path)

            with patch.object(setup.os, "scandir", side_effect=guarded_scandir):
                found_backend, error = setup._find_nzbdav_backend_project(str(root), {})
                found_frontend = setup._find_nzbdav_frontend_dir(str(root), {})

        self.assertIsNone(error)
        self.assertEqual(str(backend_project), found_backend)
        self.assertEqual(str(frontend_dir), found_frontend)

    def test_frontend_runtime_requires_server_and_client_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            frontend = Path(tmpdir) / "frontend"
            (frontend / "dist-node").mkdir(parents=True)
            (frontend / "dist-node" / "server.js").write_text("", encoding="utf-8")

            ready, error = setup._validate_nzbdav_frontend_runtime(str(frontend))
            self.assertFalse(ready)
            self.assertIn("build/server/index.js", error)

            (frontend / "build" / "server").mkdir(parents=True)
            (frontend / "build" / "server" / "index.js").write_text(
                "", encoding="utf-8"
            )
            (frontend / "build" / "client").mkdir()
            ready, error = setup._validate_nzbdav_frontend_runtime(str(frontend))

        self.assertTrue(ready, error)

    def test_artifact_activation_replaces_backend_and_frontend_together(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            restored = root / "restored"
            live = root / "live"
            for component in ("app", "frontend-build", "frontend-dist-node"):
                (restored / component).mkdir(parents=True)
                (restored / component / "marker").write_text("new", encoding="utf-8")
                (live / component).mkdir(parents=True)
                (live / component / "marker").write_text("old", encoding="utf-8")

            activated, error = setup._activate_nzbdav_build_artifact(
                [
                    (restored / "app", live / "app"),
                    (restored / "frontend-build", live / "frontend-build"),
                    (
                        restored / "frontend-dist-node",
                        live / "frontend-dist-node",
                    ),
                ]
            )

            self.assertTrue(activated, error)
            for component in ("app", "frontend-build", "frontend-dist-node"):
                self.assertEqual(
                    "new",
                    (live / component / "marker").read_text(encoding="utf-8"),
                )

    def test_source_preparation_preserves_live_backend_until_activation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for source_dir in ("backend", "frontend"):
                (root / source_dir).mkdir()
            app = root / "app"
            app.mkdir()
            (app / "System.IO.Hashing.dll").write_text("10.0.9", encoding="utf-8")
            (root / setup._NZBDAV_PREBUILT_MARKER).write_text("{}", encoding="utf-8")
            (root / setup._NZBDAV_SOURCE_BUILD_MARKER).write_text("3", encoding="utf-8")

            success, error = setup._prepare_nzbdav_source_tree(str(root))

            self.assertTrue(success, error)
            self.assertFalse((root / "backend").exists())
            self.assertFalse((root / "frontend").exists())
            self.assertEqual(
                "10.0.9",
                (app / "System.IO.Hashing.dll").read_text(encoding="utf-8"),
            )
            self.assertTrue((root / setup._NZBDAV_PREBUILT_MARKER).is_file())
            self.assertTrue((root / setup._NZBDAV_SOURCE_BUILD_MARKER).is_file())

    def test_ownership_preflight_repairs_state_without_walking_blobs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "blobs" / "aa" / "bb").mkdir(parents=True)
            (root / "db.sqlite.maintenance.lock").write_text("lock", encoding="utf-8")
            session_key = root / "session.key"
            session_key.write_text("signing key", encoding="utf-8")
            (root / "logs").mkdir()

            with (
                patch.object(setup, "chown_single") as chown_single,
                patch.object(
                    setup, "chown_recursive", return_value=(True, None)
                ) as recursive,
            ):
                success, error = setup._normalize_nzbdav_writable_ownership(
                    str(root), os.getuid(), os.getgid()
                )

        self.assertTrue(success, error)
        chown_single.assert_any_call(str(session_key), os.getuid(), os.getgid())
        recursive.assert_called_once_with(str(root / "logs"), os.getuid(), os.getgid())

    def test_update_clear_preserves_config_data_and_sqlite_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data = root / "data"
            data.mkdir()
            (data / "state.json").write_text("state", encoding="utf-8")
            config_file = root / "settings.json"
            config_file.write_text("settings", encoding="utf-8")
            for database_name in (
                "db.sqlite",
                "db.sqlite-wal",
                "metrics.sqlite",
                "warden.db",
                "usenet-migration.db",
            ):
                (root / database_name).write_text(database_name, encoding="utf-8")
            (root / "session.key").write_text("signing key", encoding="utf-8")
            runtime_file = root / "old-runtime.dll"
            runtime_file.write_text("replaceable", encoding="utf-8")
            config = {
                "process_name": "InfiniDysk",
                "repo_name": "infinidysk",
                "config_file": str(config_file),
                "exclude_dirs": [],
            }

            exclusions = setup._update_persistent_excludes(config, str(root))
            success, error = setup.clear_directory(str(root), exclusions)

            self.assertTrue(success, error)
            self.assertTrue(config_file.is_file())
            self.assertTrue((data / "state.json").is_file())
            self.assertTrue((root / "db.sqlite").is_file())
            self.assertTrue((root / "db.sqlite-wal").is_file())
            self.assertTrue((root / "metrics.sqlite").is_file())
            self.assertTrue((root / "warden.db").is_file())
            self.assertTrue((root / "usenet-migration.db").is_file())
            self.assertTrue((root / "session.key").is_file())
            self.assertFalse(runtime_file.exists())

    def test_archive_exclusions_preserve_symlinked_data_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "service"
            external_data = Path(tmpdir) / "persistent-data"
            root.mkdir()
            external_data.mkdir()
            (root / "data").symlink_to(external_data, target_is_directory=True)

            exclusions = setup._update_persistent_excludes({}, str(root))
            normalized = setup.downloader._normalized_archive_excludes(
                str(root), exclusions
            )

            self.assertIn(str(root / "data"), exclusions)
            self.assertEqual({"data"}, normalized)

    def test_source_build_atomically_replaces_stale_backend_dependencies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_nzbdav_source(root)
            live_app = root / "app"
            live_app.mkdir()
            (live_app / "System.IO.Hashing.dll").write_text("10.0.9", encoding="utf-8")
            (live_app / "removed-dependency.dll").write_text("stale", encoding="utf-8")
            config = {"config_dir": str(root)}
            build_toolchain = {}

            def build_key(_service, _source, **kwargs):
                build_toolchain.update(kwargs["toolchain"])
                return "a" * 64

            def publish(_handler, _key, _platforms, _config_dir, **kwargs):
                output_dir = kwargs["dotnet_options"]["output_dir"]
                self.assertNotEqual(str(live_app), output_dir)
                self._write_nzbdav_publish(output_dir, "10.0.10")
                return True, None

            with (
                patch.object(setup, "chown_recursive", return_value=(True, None)),
                patch.object(
                    setup,
                    "_patch_nzbdav_embedded_resource_util",
                    return_value=(True, None),
                ),
                patch.object(
                    setup, "_nzbdav_uses_internal_nzb_models", return_value=False
                ),
                patch.object(setup.INSTALL_CACHE, "build_key", side_effect=build_key),
                patch.object(
                    setup.INSTALL_CACHE,
                    "restore_artifact",
                    return_value=(False, "artifact not found"),
                ),
                patch.object(
                    setup.INSTALL_CACHE, "store_artifact", return_value=(True, None)
                ),
                patch.object(setup, "setup_environment", side_effect=publish),
            ):
                success, error = setup.setup_nzbdav_build(Mock(), config)

            self.assertTrue(success, error)
            self.assertEqual(3, build_toolchain["artifact_format"])
            self.assertEqual(
                "10.0.10",
                (live_app / "System.IO.Hashing.dll").read_text(encoding="utf-8"),
            )
            self.assertFalse((live_app / "removed-dependency.dll").exists())
            self.assertTrue(setup._nzbdav_source_build_is_current(str(root)))
            self.assertFalse(list(root.glob(".nzbdav-publish-candidate-*")))

    def test_source_build_failure_preserves_existing_backend(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_nzbdav_source(root)
            live_app = root / "app"
            live_app.mkdir()
            (live_app / "System.IO.Hashing.dll").write_text("10.0.9", encoding="utf-8")
            config = {"config_dir": str(root)}

            def failed_publish(_handler, _key, _platforms, _config_dir, **kwargs):
                output_dir = kwargs["dotnet_options"]["output_dir"]
                self._write_nzbdav_publish(output_dir, "partial-10.0.10")
                return False, "publish failed"

            with (
                patch.object(setup, "chown_recursive", return_value=(True, None)),
                patch.object(
                    setup,
                    "_patch_nzbdav_embedded_resource_util",
                    return_value=(True, None),
                ),
                patch.object(
                    setup, "_nzbdav_uses_internal_nzb_models", return_value=False
                ),
                patch.object(setup.INSTALL_CACHE, "build_key", return_value="b" * 64),
                patch.object(
                    setup.INSTALL_CACHE,
                    "restore_artifact",
                    return_value=(False, "artifact not found"),
                ),
                patch.object(setup, "setup_environment", side_effect=failed_publish),
            ):
                success, error = setup.setup_nzbdav_build(Mock(), config)

            self.assertFalse(success)
            self.assertEqual("publish failed", error)
            self.assertEqual(
                "10.0.9",
                (live_app / "System.IO.Hashing.dll").read_text(encoding="utf-8"),
            )
            self.assertFalse(setup._nzbdav_source_build_is_current(str(root)))
            self.assertFalse(list(root.glob(".nzbdav-publish-candidate-*")))

    def test_commit_pin_requires_full_sha_and_normalizes_case(self):
        commit_sha = "A" * 40

        normalized, error = setup._normalize_commit_sha(commit_sha)
        short_value, short_error = setup._normalize_commit_sha("abc1234")

        self.assertEqual("a" * 40, normalized)
        self.assertIsNone(error)
        self.assertIsNone(short_value)
        self.assertIn("40-character hexadecimal", short_error)

    def test_commit_state_skip_requires_matching_installed_version_marker(self):
        commit_sha = "a" * 40
        with tempfile.TemporaryDirectory() as tmpdir:
            version_path = Path(tmpdir) / "version.txt"
            version_path.write_text("v0.8.1", encoding="utf-8")

            self.assertFalse(setup._commit_version_marker_matches(tmpdir, commit_sha))

            version_path.write_text(
                f"commit-{commit_sha[:12]}",
                encoding="utf-8",
            )
            self.assertTrue(setup._commit_version_marker_matches(tmpdir, commit_sha))

    def test_commit_pin_takes_precedence_over_release_and_branch(self):
        commit_sha = "a" * 40
        config = {
            "enabled": True,
            "process_name": "InfiniDysk",
            "config_dir": "/nzbdav",
            "release_version_enabled": True,
            "release_version": "latest",
            "commit_sha": commit_sha,
            "branch_enabled": True,
            "branch": "main",
            "env": {},
        }
        process_handler = Mock()
        process_handler.setup_tracker = set()
        process_handler.setup_tracker_lock = threading.Lock()

        with (
            patch.object(
                setup.CONFIG_MANAGER,
                "find_key_for_process",
                return_value=("infinidysk", None),
            ),
            patch.object(setup.CONFIG_MANAGER, "get_instance", return_value=config),
            patch.object(
                setup, "setup_branch_version", return_value=(True, None)
            ) as install_source,
            patch.object(setup, "setup_release_version") as install_release,
            patch.object(setup, "setup_nzbdav", return_value=(True, None)),
        ):
            success, error = setup.install_project(process_handler, "InfiniDysk")

        self.assertTrue(success, error)
        install_source.assert_called_once_with(
            process_handler, config, "InfiniDysk", "infinidysk"
        )
        install_release.assert_not_called()

    def test_commit_marker_is_not_written_before_source_setup_succeeds(self):
        commit_sha = "b" * 40
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "process_name": "DUMB Frontend",
                "config_dir": tmpdir,
                "repo_owner": "example",
                "repo_name": "frontend",
                "commit_sha": commit_sha,
                "clear_on_update": False,
            }
            with (
                patch.object(
                    setup.downloader,
                    "get_commit",
                    return_value=("https://example.invalid/archive.zip", "source"),
                ),
                patch.object(
                    setup.downloader,
                    "download_and_extract",
                    return_value=(True, None),
                ),
                patch.object(
                    setup,
                    "additional_setup",
                    return_value=(False, "build failed"),
                ),
                patch.object(setup.versions, "version_write") as version_write,
            ):
                success, error = setup.setup_branch_version(
                    Mock(),
                    config,
                    "DUMB Frontend",
                    "dumb_frontend",
                )

        self.assertFalse(success)
        self.assertEqual("build failed", error)
        version_write.assert_not_called()

    def test_frontend_branch_marker_is_written_after_source_setup_succeeds(self):
        branch_sha = "a" * 40
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "process_name": "DUMB Frontend",
                "config_dir": tmpdir,
                "repo_owner": "example",
                "repo_name": "frontend",
                "commit_sha": "",
                "branch_enabled": True,
                "branch": "dev",
                "clear_on_update": False,
            }
            with (
                patch.object(
                    setup.downloader,
                    "get_branch",
                    return_value=("https://example.invalid/archive.zip", "source"),
                ),
                patch.object(
                    setup.downloader,
                    "get_ref_commit_sha",
                    side_effect=((branch_sha, None), (None, None)),
                ),
                patch.object(
                    setup.downloader,
                    "download_and_extract",
                    return_value=(True, None),
                ),
                patch.object(setup, "additional_setup", return_value=(True, None)),
            ):
                success, error = setup.setup_branch_version(
                    Mock(),
                    config,
                    "DUMB Frontend",
                    "dumb_frontend",
                )

            self.assertTrue(success, error)
            self.assertEqual(
                "dev-aaaaaaaa",
                (Path(tmpdir) / "version.txt").read_text(encoding="utf-8"),
            )

    def test_release_tag_marker_records_resolved_commit_after_setup(self):
        release_sha = "d" * 40
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "process_name": "InfiniDysk",
                "config_dir": tmpdir,
                "repo_owner": "nzbdav",
                "repo_name": "nzbdav",
                "release_version": "dev",
                "clear_on_update": False,
            }
            with (
                patch.object(
                    setup.downloader,
                    "get_ref_commit_sha",
                    return_value=(release_sha, None),
                ),
                patch.object(
                    setup,
                    "_install_nzbdav_prebuilt_release",
                    return_value=(None, "archive unavailable"),
                ),
                patch.object(
                    setup, "_prepare_nzbdav_source_tree", return_value=(True, None)
                ),
                patch.object(
                    setup.downloader,
                    "download_release_version",
                    return_value=(True, None),
                ) as download_release,
                patch.object(setup, "additional_setup", return_value=(True, None)),
                patch.object(setup.versions, "version_write") as version_write,
            ):
                success, error = setup.setup_release_version(
                    Mock(), config, "InfiniDysk", "infinidysk"
                )

        self.assertTrue(success, error)
        self.assertEqual(
            release_sha,
            download_release.call_args.kwargs["release_version"],
        )
        version_write.assert_called_once_with(
            "InfiniDysk",
            "infinidysk",
            version_path=os.path.join(tmpdir, "version.txt"),
            version=f"dev-{release_sha[:8]}",
        )

    def test_release_tag_marker_is_not_written_when_setup_fails(self):
        release_sha = "e" * 40
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "process_name": "InfiniDysk",
                "config_dir": tmpdir,
                "repo_owner": "nzbdav",
                "repo_name": "nzbdav",
                "release_version": "dev",
                "clear_on_update": False,
            }
            with (
                patch.object(
                    setup.downloader,
                    "get_ref_commit_sha",
                    return_value=(release_sha, None),
                ),
                patch.object(
                    setup,
                    "_install_nzbdav_prebuilt_release",
                    return_value=(None, "archive unavailable"),
                ),
                patch.object(
                    setup, "_prepare_nzbdav_source_tree", return_value=(True, None)
                ),
                patch.object(
                    setup.downloader,
                    "download_release_version",
                    return_value=(True, None),
                ),
                patch.object(
                    setup, "additional_setup", return_value=(False, "build failed")
                ),
                patch.object(setup.versions, "version_write") as version_write,
            ):
                success, error = setup.setup_release_version(
                    Mock(), config, "InfiniDysk", "infinidysk"
                )

        self.assertFalse(success)
        self.assertEqual("build failed", error)
        version_write.assert_not_called()

    def test_prebuilt_release_is_verified_activated_and_manifested(self):
        release_sha = "a" * 40
        published_digest = "b" * 64
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = {
                "process_name": "InfiniDysk",
                "config_dir": str(root),
                "repo_owner": "infinidysk",
                "repo_name": "infinidysk",
            }

            def populate_candidate(_url, target_dir, **kwargs):
                candidate = Path(target_dir)
                backend = candidate / "backend"
                frontend = candidate / "frontend"
                backend.mkdir(parents=True)
                for name in (
                    "NzbWebDAV",
                    "NzbWebDAV.dll",
                    "NzbWebDAV.deps.json",
                    "librapidyenc.so",
                    "libe_sqlite3.so",
                ):
                    (backend / name).write_text(name, encoding="utf-8")
                (backend / "NzbWebDAV").chmod(0o755)
                (backend / "NzbWebDAV.runtimeconfig.json").write_text(
                    '{"runtimeOptions":{"frameworks":['
                    '{"name":"Microsoft.AspNetCore.App","version":"10.0.0"}]}}',
                    encoding="utf-8",
                )
                (frontend / "dist-node").mkdir(parents=True)
                (frontend / "dist-node" / "server.js").write_text(
                    "server", encoding="utf-8"
                )
                (frontend / "build" / "server").mkdir(parents=True)
                (frontend / "build" / "server" / "index.js").write_text(
                    "build", encoding="utf-8"
                )
                (frontend / "build" / "client").mkdir()
                (frontend / "package.json").write_text("{}", encoding="utf-8")
                (frontend / "node_modules" / "express").mkdir(parents=True)
                (frontend / "node_modules" / "express" / "package.json").write_text(
                    "{}", encoding="utf-8"
                )
                (candidate / "version.txt").write_text("0.10.0-rc.2", encoding="utf-8")
                self.assertEqual(
                    f"sha256:{published_digest}", kwargs["expected_sha256"]
                )
                self.assertEqual(
                    [
                        "infinidysk-v1.0.0-linux-arm64",
                        "infinidysk-*-linux-arm64",
                        "nzbdav-*-linux-arm64",
                    ],
                    kwargs["zip_folder_name"],
                )
                return True, None

            release_info = {
                "assets": [
                    {
                        "name": "infinidysk-v1.0.0-linux-arm64.tar.gz",
                        "digest": f"sha256:{published_digest}",
                        "browser_download_url": "https://example.test/infinidysk.tar.gz",
                    }
                ]
            }
            with (
                patch.object(setup.platform, "machine", return_value="aarch64"),
                patch.object(
                    setup.downloader,
                    "fetch_github_release_info",
                    return_value=(release_info, None),
                ),
                patch.object(
                    setup.downloader,
                    "get_ref_commit_sha",
                    return_value=(release_sha, None),
                ),
                patch.object(
                    setup.downloader,
                    "download_and_extract",
                    side_effect=populate_candidate,
                ),
                patch.object(setup, "chown_recursive", return_value=(True, None)),
                patch.object(setup, "chown_single", return_value=(True, None)),
            ):
                result, error = setup._install_nzbdav_prebuilt_release(
                    config, "InfiniDysk", "v1.0.0", str(root)
                )
                runtime_ready, runtime_error = setup._nzbdav_prebuilt_runtime_ready(
                    str(root)
                )

            self.assertIsNone(error)
            self.assertEqual(f"v1.0.0-{release_sha[:8]}", result["version_marker"])
            self.assertTrue((root / "app" / "NzbWebDAV.dll").is_file())
            self.assertTrue((root / "frontend" / "dist-node" / "server.js").is_file())
            self.assertTrue((root / setup._NZBDAV_PREBUILT_MARKER).is_file())
            self.assertEqual(
                "prebuilt", setup.read_nzbdav_install_info(str(root))["method"]
            )
            self.assertTrue(runtime_ready, runtime_error)

            canonical_marker = root / setup._NZBDAV_PREBUILT_MARKER
            legacy_marker = root / setup._NZBDAV_LEGACY_PREBUILT_MARKER
            canonical_marker.rename(legacy_marker)
            legacy_ready, legacy_error = setup._nzbdav_prebuilt_runtime_ready(str(root))
            self.assertTrue(legacy_ready, legacy_error)

    def test_prebuilt_asset_selection_retains_legacy_nzbdav_compatibility(self):
        legacy = {
            "name": "nzbdav-v0.10.0-linux-x64.tar.gz",
            "digest": f"sha256:{'a' * 64}",
        }

        selected, error = setup._select_nzbdav_prebuilt_asset(
            {"assets": [legacy]}, "x64", "infinidysk"
        )

        self.assertIsNone(error)
        self.assertIs(legacy, selected)

    def test_prebuilt_asset_selection_prefers_configured_canonical_name(self):
        legacy = {"name": "nzbdav-v1.0.0-linux-x64.tar.gz"}
        renamed = {"name": "infinidysk-v1.0.0-linux-x64.tar.gz"}

        selected, error = setup._select_nzbdav_prebuilt_asset(
            {"assets": [legacy, renamed]}, "x64", "infinidysk"
        )

        self.assertIsNone(error)
        self.assertIs(renamed, selected)

    def test_prebuilt_asset_selection_prefers_exact_rolling_channel_alias(self):
        rolling = {"name": "infinidysk-dev-linux-x64.tar.gz"}
        internally_versioned = {"name": "infinidysk-vdev-linux-x64.tar.gz"}

        selected, error = setup._select_nzbdav_prebuilt_asset(
            {"tag_name": "dev", "assets": [internally_versioned, rolling]},
            "x64",
            "infinidysk",
            "dev",
        )

        self.assertIsNone(error)
        self.assertIs(rolling, selected)
        self.assertEqual(
            [
                "infinidysk-dev-linux-x64",
                "infinidysk-*-linux-x64",
                "nzbdav-*-linux-x64",
            ],
            setup._nzbdav_prebuilt_archive_roots(rolling["name"], "dev"),
        )

    def test_prebuilt_rejects_stale_rolling_release_assets(self):
        current_sha = "f" * 40
        stale_sha = "a" * 40
        config = {
            "repo_owner": "infinidysk",
            "repo_name": "infinidysk",
        }
        release_info = {
            "tag_name": "dev",
            "target_commitish": stale_sha,
            "assets": [
                {
                    "name": "infinidysk-dev-linux-x64.tar.gz",
                    "digest": f"sha256:{'b' * 64}",
                    "browser_download_url": "https://example.test/dev.tar.gz",
                }
            ],
        }

        with (
            patch.object(setup.platform, "machine", return_value="x86_64"),
            patch.object(
                setup.downloader,
                "fetch_github_release_info",
                return_value=(release_info, None),
            ),
            patch.object(
                setup.downloader,
                "get_ref_commit_sha",
                return_value=(current_sha, None),
            ),
            patch.object(setup.downloader, "download_and_extract") as download,
        ):
            result, error = setup._install_nzbdav_prebuilt_release(
                config, "InfiniDysk", "dev", "/tmp/nzbdav-test"
            )

        self.assertIsNone(result)
        self.assertIn("archives target aaaaaaaa", error)
        self.assertIn("tag now points to ffffffff", error)
        download.assert_not_called()

    def test_prebuilt_runtime_prefers_native_host_over_leftover_sdk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = root / "app"
            app.mkdir()
            native = app / "NzbWebDAV"
            native.write_text("native", encoding="utf-8")
            native.chmod(0o755)
            (app / "NzbWebDAV.dll").write_text("dll", encoding="utf-8")
            sdk = root / ".dotnet-sdk" / "dotnet"
            sdk.parent.mkdir()
            sdk.write_text("sdk", encoding="utf-8")

            prebuilt_command, prebuilt_error = setup._nzbdav_build_command(
                str(app), prefer_native=True
            )
            source_command, source_error = setup._nzbdav_build_command(str(app))

        self.assertIsNone(prebuilt_error)
        self.assertEqual([str(native)], prebuilt_command)
        self.assertIsNone(source_error)
        self.assertEqual([str(sdk), str(app / "NzbWebDAV.dll")], source_command)

    def test_prebuilt_unavailable_falls_back_to_source_release(self):
        release_sha = "c" * 40
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "process_name": "InfiniDysk",
                "config_dir": tmpdir,
                "repo_owner": "nzbdav",
                "repo_name": "nzbdav",
                "release_version": "v0.9.5",
                "clear_on_update": False,
            }
            with (
                patch.object(
                    setup.downloader,
                    "get_ref_commit_sha",
                    return_value=(release_sha, None),
                ),
                patch.object(
                    setup,
                    "_install_nzbdav_prebuilt_release",
                    return_value=(None, "archive unavailable"),
                ) as prebuilt,
                patch.object(
                    setup, "_prepare_nzbdav_source_tree", return_value=(True, None)
                ),
                patch.object(
                    setup.downloader,
                    "download_release_version",
                    return_value=(True, None),
                ) as source_download,
                patch.object(setup, "additional_setup", return_value=(True, None)),
                patch.object(setup.versions, "version_write"),
            ):
                success, error = setup.setup_release_version(
                    Mock(), config, "InfiniDysk", "infinidysk"
                )
            install_info = setup.read_nzbdav_install_info(tmpdir)

        self.assertTrue(success, error)
        prebuilt.assert_called_once()
        source_download.assert_called_once()
        self.assertEqual("source", install_info["method"])
        self.assertEqual("archive unavailable", install_info["fallback_reason"])

    def test_install_provenance_redacts_fallback_details(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            setup._write_nzbdav_install_info(
                tmpdir,
                method="source",
                requested_selector="dev",
                fallback_reason="download failed?token=do-not-expose",
            )

            install_info = setup.read_nzbdav_install_info(tmpdir)

        self.assertEqual(
            "download failed?token=[REDACTED]", install_info["fallback_reason"]
        )

    def test_lts_tag_without_release_object_falls_back_to_same_source_commit(self):
        release_sha = "e" * 40
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "process_name": "InfiniDysk",
                "config_dir": tmpdir,
                "repo_owner": "infinidysk",
                "repo_name": "infinidysk",
                "release_version": "lts",
                "clear_on_update": False,
            }
            with (
                patch.object(
                    setup.downloader,
                    "get_ref_commit_sha",
                    return_value=(release_sha, None),
                ),
                patch.object(
                    setup.downloader,
                    "fetch_github_release_info",
                    return_value=(None, "release not found"),
                ),
                patch.object(
                    setup.downloader,
                    "download_release_version",
                    return_value=(True, None),
                ) as source_download,
                patch.object(setup, "additional_setup", return_value=(True, None)),
                patch.object(setup.versions, "version_write"),
            ):
                success, error = setup.setup_release_version(
                    Mock(), config, "InfiniDysk", "infinidysk"
                )
            install_info = setup.read_nzbdav_install_info(tmpdir)

        self.assertTrue(success, error)
        self.assertEqual(
            release_sha, source_download.call_args.kwargs["release_version"]
        )
        self.assertEqual("source", install_info["method"])
        self.assertEqual("lts", install_info["resolved_release"])
        self.assertIn("release not found", install_info["fallback_reason"])

    def test_prerelease_source_fallback_uses_resolved_immutable_commit(self):
        release_sha = "d" * 40
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "process_name": "InfiniDysk",
                "config_dir": tmpdir,
                "repo_owner": "nzbdav",
                "repo_name": "nzbdav",
                "release_version": "prerelease",
                "clear_on_update": False,
            }
            with (
                patch.object(
                    setup.downloader,
                    "get_latest_release",
                    return_value=("v0.10.0-rc.3", None),
                ),
                patch.object(
                    setup.downloader,
                    "get_ref_commit_sha",
                    return_value=(release_sha, None),
                ),
                patch.object(
                    setup,
                    "_install_nzbdav_prebuilt_release",
                    return_value=(None, "archive unavailable"),
                ) as prebuilt,
                patch.object(
                    setup, "_prepare_nzbdav_source_tree", return_value=(True, None)
                ),
                patch.object(
                    setup.downloader,
                    "download_release_version",
                    return_value=(True, None),
                ) as source_download,
                patch.object(setup, "additional_setup", return_value=(True, None)),
                patch.object(setup.versions, "version_write") as version_write,
            ):
                success, error = setup.setup_release_version(
                    Mock(), config, "InfiniDysk", "infinidysk"
                )

        self.assertTrue(success, error)
        self.assertEqual("v0.10.0-rc.3", prebuilt.call_args.args[2])
        self.assertEqual(
            release_sha,
            source_download.call_args.kwargs["release_version"],
        )
        version_write.assert_called_once_with(
            "InfiniDysk",
            "infinidysk",
            version_path=os.path.join(tmpdir, "version.txt"),
            version=f"v0.10.0-rc.3-{release_sha[:8]}",
        )

    def _write_start_script(self, root: Path) -> Path:
        frontend_dir = root / "frontend"
        frontend_dir.mkdir()
        with patch.object(setup, "chown_single"):
            script_path = setup._write_nzbdav_start_script(
                str(root),
                [str(root / "backend")],
                str(frontend_dir),
                8080,
            )
        return Path(script_path)

    def test_start_script_runs_frontend_before_blocking_migration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script = self._write_start_script(Path(tmpdir)).read_text(encoding="utf-8")

        frontend_start = script.index("node dist-node/server.js &")
        migration_start = script.index(" --db-migration")
        backend_start = script.index(" &\nBACKEND_PID=$!", migration_start)

        self.assertLess(script.index("trap terminate TERM INT"), migration_start)
        self.assertLess(frontend_start, migration_start)
        self.assertLess(migration_start, backend_start)
        self.assertIn("MIGRATION_EXIT_CODE=$?", script)
        self.assertIn('exit "$MIGRATION_EXIT_CODE"', script)

    def test_migration_failure_stops_frontend_and_preserves_exit_code(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state_dir = root / "state"
            state_dir.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()

            backend = root / "backend"
            backend.write_text(
                """#!/bin/sh
if [ "${1:-}" = "--db-migration" ]; then
  count=0
  while [ ! -f "$TEST_STATE/frontend.started" ] && [ "$count" -lt 100 ]; do
    sleep 0.01
    count=$((count + 1))
  done
  [ -f "$TEST_STATE/frontend.started" ] || exit 99
  touch "$TEST_STATE/migration.started"
  exit 23
fi
touch "$TEST_STATE/backend.started"
""",
                encoding="utf-8",
            )
            backend.chmod(0o755)

            node = bin_dir / "node"
            node.write_text(
                """#!/bin/sh
touch "$TEST_STATE/frontend.started"
trap 'touch "$TEST_STATE/frontend.stopped"; exit 0' TERM INT
while :; do sleep 0.05; done
""",
                encoding="utf-8",
            )
            node.chmod(0o755)
            script = self._write_start_script(root)

            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["TEST_STATE"] = str(state_dir)
            result = subprocess.run(
                ["/bin/sh", str(script)],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(23, result.returncode, result.stderr)
            self.assertTrue((state_dir / "migration.started").exists())
            self.assertTrue((state_dir / "frontend.stopped").exists())
            self.assertFalse((state_dir / "backend.started").exists())


if __name__ == "__main__":
    unittest.main()
