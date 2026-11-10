import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, mock_open, patch


def _install_stubs():
    config_loader = types.ModuleType("utils.config_loader")
    config_loader.CONFIG_MANAGER = types.SimpleNamespace(
        get=lambda key, default=None: {"puid": 1000, "pgid": 1000}.get(key, default)
    )
    sys.modules["utils.config_loader"] = config_loader

    global_logger = types.ModuleType("utils.global_logger")
    global_logger.logger = types.SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
    )
    sys.modules["utils.global_logger"] = global_logger


_install_stubs()
sys.modules.pop("utils.user_management", None)
user_management = importlib.import_module("utils.user_management")


class UserManagementSecurityTests(unittest.TestCase):
    def test_gpu_membership_adds_only_missing_existing_groups(self):
        render = Mock(gr_gid=109, gr_mem=[])
        video = Mock(gr_gid=44, gr_mem=["DUMB"])
        with (
            patch.object(user_management.os.path, "isdir", return_value=True),
            patch.object(
                user_management.pwd, "getpwnam", return_value=Mock(pw_gid=1000)
            ),
            patch.object(user_management.grp, "getgrnam", side_effect=[render, video]),
            patch.object(user_management.subprocess, "run") as run,
        ):
            user_management.ensure_gpu_group_membership("DUMB")
        run.assert_called_once_with(["usermod", "-aG", "render", "DUMB"], check=True)

    def test_gpu_membership_skips_missing_device_and_missing_groups(self):
        for device in (False, True):
            with (
                patch.object(user_management.os.path, "isdir", return_value=device),
                patch.object(
                    user_management.pwd, "getpwnam", return_value=Mock(pw_gid=1000)
                ),
                patch.object(user_management.grp, "getgrnam", side_effect=KeyError),
                patch.object(user_management.subprocess, "run") as run,
            ):
                user_management.ensure_gpu_group_membership("DUMB")
            run.assert_not_called()

    def test_existing_user_reconciles_gpu_groups_on_startup(self):
        with (
            patch.object(user_management.grp, "getgrgid"),
            patch.object(user_management.pwd, "getpwnam"),
            patch.object(user_management, "ensure_gpu_group_membership") as reconcile,
            patch.object(user_management, "migrate_symlinks"),
        ):
            user_management.create_system_user()
        reconcile.assert_called_once_with("DUMB")

    def _symlink_migration_calls(self, *, legacy_identity, config_dir):
        config = types.SimpleNamespace(
            get=lambda key, default=None: {
                "data_root": "/data",
                "infinidysk": {"config_dir": config_dir},
            }.get(key, default),
            uses_legacy_infinidysk_identity=lambda: legacy_identity,
        )
        migrate = Mock()
        with (
            patch.object(user_management, "config", config),
            patch.object(user_management, "is_mount", return_value=True),
            patch.object(user_management, "cleanup_broken_symlinks"),
            patch.object(user_management, "migrate_and_symlink", migrate),
            patch.object(user_management.os.path, "lexists", return_value=False),
            patch.object(user_management.os.path, "exists", return_value=False),
        ):
            user_management.migrate_symlinks()
        return [call.args for call in migrate.call_args_list]

    def test_fresh_install_creates_only_canonical_infinidysk_root(self):
        calls = self._symlink_migration_calls(
            legacy_identity=False, config_dir="/infinidysk"
        )

        self.assertIn(("/infinidysk", "/data/infinidysk"), calls)
        self.assertNotIn(("/nzbdav", "/data/nzbdav"), calls)
        self.assertNotIn(("/cli_debrid/data", "/data/cli_debrid"), calls)

    def test_cli_guard_precedes_broken_link_cleanup_and_other_migrations(self):
        with (
            patch.object(user_management, "is_mount", return_value=True),
            patch.object(
                user_management,
                "_prepare_managed_cli_runtime",
                side_effect=RuntimeError("refused CLI redirect"),
            ),
            patch.object(user_management, "cleanup_broken_symlinks") as cleanup,
            patch.object(user_management, "migrate_and_symlink") as migrate,
        ):
            with self.assertRaisesRegex(RuntimeError, "refused CLI redirect"):
                user_management.migrate_symlinks()
        cleanup.assert_not_called()
        migrate.assert_not_called()

    def test_cli_early_guard_skips_fresh_roots_and_disabled_cli_with_custom_battery(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            for cli, battery in (
                ({"enabled": True, "config_dir": str(Path(tmp, "not-installed"))}, {}),
                (
                    {"enabled": False, "config_dir": tmp},
                    {
                        "enabled": True,
                        "config_dir": tmp,
                        "command": ["python", "operator_battery.py"],
                    },
                ),
                ({"enabled": True, "config_dir": tmp}, "invalid"),
            ):
                values = {"cli_debrid": cli, "cli_battery": battery}
                with (
                    patch.object(
                        user_management,
                        "config",
                        types.SimpleNamespace(
                            get=lambda key, default=None: values.get(key, default)
                        ),
                    ),
                    patch.object(
                        user_management, "prepare_cli_debrid_runtime"
                    ) as prepare,
                ):
                    user_management._prepare_managed_cli_runtime()
                prepare.assert_not_called()

    def test_new_user_uses_guarded_cli_preparation_instead_of_broad_cli_chown(self):
        values = {"dumb": {"log_dir": "/log"}, "rclone": {"instances": {}}}
        with (
            patch.object(
                user_management,
                "config",
                types.SimpleNamespace(
                    get=lambda key, default=None: values.get(key, default)
                ),
            ),
            patch.object(user_management.grp, "getgrgid"),
            patch.object(user_management.pwd, "getpwnam", side_effect=KeyError),
            patch.object(user_management.os.path, "exists", return_value=True),
            patch.object(user_management.os, "makedirs"),
            patch.object(user_management.os, "chown"),
            patch.object(
                user_management.os,
                "stat",
                return_value=types.SimpleNamespace(st_uid=1000, st_gid=1000),
            ),
            patch("builtins.open", mock_open()),
            patch.object(user_management, "ensure_gpu_group_membership"),
            patch.object(
                user_management,
                "_generate_user_password",
                return_value="example-test-password",
            ),
            patch.object(
                user_management, "_hash_user_password", return_value="example-test-hash"
            ),
            patch.object(user_management, "_set_user_password"),
            patch.object(
                user_management, "chown_recursive", return_value=(True, None)
            ) as broad_chown,
            patch.object(
                user_management, "chown_startup_directory", return_value=(True, None)
            ),
            patch.object(
                user_management, "_prepare_managed_cli_runtime"
            ) as cli_prepare,
            patch.object(user_management, "migrate_symlinks"),
        ):
            user_management.create_system_user()
        cli_prepare.assert_called_once()
        self.assertFalse(
            any(
                str(call.args[0]).startswith("/cli_debrid")
                for call in broad_chown.call_args_list
            )
        )

    def test_legacy_install_does_not_create_an_unused_canonical_root(self):
        calls = self._symlink_migration_calls(
            legacy_identity=True, config_dir="/nzbdav"
        )

        self.assertIn(("/nzbdav", "/data/nzbdav"), calls)
        self.assertNotIn(("/infinidysk", "/data/infinidysk"), calls)

    def test_dynamic_workers_use_all_available_cpus_on_local_filesystem(self):
        with (
            patch.object(user_management, "_available_cpu_count", return_value=128),
            patch.object(user_management, "_filesystem_type", return_value="ext4"),
        ):
            self.assertEqual(user_management.get_dynamic_workers("/config"), 128)

    def test_dynamic_workers_cap_network_filesystems(self):
        for filesystem_type in ("nfs4", "cifs", "fuse.rclone"):
            with self.subTest(filesystem_type=filesystem_type):
                with (
                    patch.object(
                        user_management, "_available_cpu_count", return_value=128
                    ),
                    patch.object(
                        user_management,
                        "_filesystem_type",
                        return_value=filesystem_type,
                    ),
                ):
                    self.assertEqual(
                        user_management.get_dynamic_workers("/mnt/media"), 16
                    )

    def test_validate_managed_user_ids_accepts_positive_ids(self):
        user_management.validate_managed_user_ids(1000, 1000)

    def test_validate_managed_user_ids_rejects_non_positive_ids(self):
        invalid_values = (
            (0, 1000, "PUID=0"),
            (1000, 0, "PGID=0"),
            (-1, 1000, "PUID=-1"),
            (1000, -1, "PGID=-1"),
        )

        for puid, pgid, expected in invalid_values:
            with self.subTest(puid=puid, pgid=pgid):
                with self.assertRaisesRegex(ValueError, expected):
                    user_management.validate_managed_user_ids(puid, pgid)

    def test_create_system_user_rejects_root_ids_before_account_lookup(self):
        with (
            patch.object(user_management, "user_id", 0),
            patch.object(user_management.grp, "getgrgid") as group_lookup,
            self.assertRaisesRegex(ValueError, "PUID=0"),
        ):
            user_management.create_system_user()

        group_lookup.assert_not_called()

    def test_hash_user_password_uses_stdin_without_shell(self):
        calls = []

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return types.SimpleNamespace(stdout="$6$hashed\n")

        original_run = user_management.subprocess.run
        user_management.subprocess.run = fake_run
        try:
            hashed = user_management._hash_user_password("raw-password")
        finally:
            user_management.subprocess.run = original_run

        self.assertEqual(hashed, "$6$hashed")
        self.assertEqual(calls[0][0][0], ["openssl", "passwd", "-6", "-stdin"])
        self.assertEqual(calls[0][1]["input"], "raw-password")
        self.assertTrue(calls[0][1]["capture_output"])
        self.assertTrue(calls[0][1]["text"])
        self.assertTrue(calls[0][1]["check"])
        self.assertNotIn("shell", calls[0][1])

    def test_set_user_password_uses_argument_list_without_shell(self):
        calls = []

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return types.SimpleNamespace(returncode=0)

        original_run = user_management.subprocess.run
        user_management.subprocess.run = fake_run
        try:
            user_management._set_user_password("dumb", "$6$hashed")
        finally:
            user_management.subprocess.run = original_run

        self.assertEqual(calls[0][0][0], ["usermod", "-p", "$6$hashed", "dumb"])
        self.assertTrue(calls[0][1]["check"])
        self.assertNotIn("shell", calls[0][1])

    def test_generate_user_password_returns_nonempty_random_string(self):
        first = user_management._generate_user_password()
        second = user_management._generate_user_password()

        self.assertIsInstance(first, str)
        self.assertGreaterEqual(len(first), 16)
        self.assertNotEqual(first, second)

    def test_startup_ownership_skips_healthy_top_level_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            healthy = os.path.join(temp_dir, "healthy")
            os.makedirs(healthy)
            with open(os.path.join(healthy, "nested"), "w", encoding="utf-8") as handle:
                handle.write("data")
            stat_info = os.stat(temp_dir)

            with patch.object(
                user_management,
                "chown_recursive",
                wraps=user_management.chown_recursive,
            ) as recursive:
                success, error = user_management.chown_startup_directory(
                    temp_dir, stat_info.st_uid, stat_info.st_gid
                )

            self.assertTrue(success, error)
            recursive.assert_not_called()

    def test_startup_ownership_repairs_controller_owned_entry_separately(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            protected = os.path.join(temp_dir, "arr-postgres-migration")
            os.makedirs(protected)
            stat_info = os.stat(temp_dir)

            with (
                patch.object(
                    user_management,
                    "_repair_controller_owned_tree",
                    return_value=(True, None),
                ) as controller_repair,
                patch.object(user_management, "chown_recursive") as managed_repair,
            ):
                success, error = user_management.chown_startup_directory(
                    temp_dir,
                    stat_info.st_uid,
                    stat_info.st_gid,
                    controller_owned_entries={"arr-postgres-migration"},
                    controller_uid=stat_info.st_uid,
                    controller_gid=stat_info.st_gid,
                )

            self.assertTrue(success, error)
            controller_repair.assert_called_once_with(
                protected, stat_info.st_uid, stat_info.st_gid
            )
            managed_repair.assert_not_called()

    def test_controller_owned_tree_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            protected = os.path.join(temp_dir, "arr-postgres-migration")
            os.makedirs(protected)
            os.symlink(temp_dir, os.path.join(protected, "unsafe"))
            stat_info = os.stat(temp_dir)

            success, error = user_management._repair_controller_owned_tree(
                protected, stat_info.st_uid, stat_info.st_gid
            )

            self.assertFalse(success)
            self.assertIn("unsafe link or mount", error)

    def test_private_controller_state_names_are_preserved(self):
        self.assertTrue(
            {".dumb-setup", "installation-backups", "backup-storage"}
            <= user_management.CONTROLLER_OWNED_CONFIG_ENTRIES
        )

    def test_private_tree_preflight_refuses_unsafe_entries_before_any_chown(self):
        for unsafe in ("symlink", "hardlink", "shared_mode", "special"):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / ".dumb-setup"
                root.mkdir(mode=0o700)
                record = root / "run-record.json"
                record.write_text('{"retained":true}')
                record.chmod(0o600)
                if unsafe == "symlink":
                    (root / "unsafe").symlink_to(record)
                elif unsafe == "hardlink":
                    os.link(record, root / "unsafe")
                elif unsafe == "shared_mode":
                    record.chmod(0o640)
                else:
                    os.mkfifo(root / "unsafe", 0o600)
                with patch.object(user_management.os, "fchown") as change:
                    ok, message = user_management._repair_private_controller_tree(
                        str(root),
                        os.geteuid() + 1,
                        os.getegid() + 1,
                        os.geteuid(),
                        os.getegid(),
                    )
                self.assertFalse(ok)
                self.assertIn("controller UID", message)
                self.assertIn("Preserve existing records", message)
                change.assert_not_called()
                self.assertEqual(record.read_text(), '{"retained":true}')

    def test_private_tree_foreign_owner_and_mount_identity_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".dumb-setup"
            root.mkdir(mode=0o700)
            record = root / "run-record.json"
            record.write_text("{}")
            record.chmod(0o600)
            with patch.object(user_management.os, "fchown") as change:
                ok, _ = user_management._repair_private_controller_tree(
                    str(root),
                    os.geteuid() + 1,
                    os.getegid(),
                    os.geteuid() + 2,
                    os.getegid(),
                )
                self.assertFalse(ok)
                change.assert_not_called()
            with (
                patch.object(
                    user_management, "_controller_state_mount_id", side_effect=[1, 1, 2]
                ),
                patch.object(user_management.os, "fchown") as change,
            ):
                ok, _ = user_management._repair_private_controller_tree(
                    str(root),
                    os.geteuid(),
                    os.getegid(),
                    os.geteuid(),
                    os.getegid(),
                )
                self.assertFalse(ok)
                change.assert_not_called()

    def test_private_state_never_uses_service_recursive_chown(self):
        for name in (".dumb-setup", "installation-backups", "backup-storage"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                protected = Path(temp) / name
                protected.mkdir(mode=0o700)
                with (
                    patch.object(
                        user_management,
                        "_repair_private_controller_tree",
                        return_value=(True, None),
                    ) as repair,
                    patch.object(user_management, "chown_recursive") as service_chown,
                ):
                    ok, error = user_management.chown_startup_directory(
                        temp,
                        os.geteuid(),
                        os.getegid(),
                        controller_owned_entries=user_management.CONTROLLER_OWNED_CONFIG_ENTRIES,
                    )
                self.assertTrue(ok, error)
                repair.assert_called_once_with(
                    str(protected),
                    os.geteuid(),
                    os.getegid(),
                    os.geteuid(),
                    os.getegid(),
                )
                service_chown.assert_not_called()

    @unittest.skipUnless(
        os.geteuid() == 0,
        "Requires root only to create the historical managed-owner fixture",
    )
    def test_recorded_managed_owner_regression_repairs_without_changing_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".dumb-setup"
            root.mkdir(mode=0o700)
            record = root / ("run-" + "a" * 32 + ".json")
            payload = b'{"run_id":"retained","status":"partial"}'
            record.write_bytes(payload)
            record.chmod(0o600)
            os.chown(record, 1001, 1001)
            os.chown(root, 1001, 1001)
            ok, error = user_management._repair_private_controller_tree(
                str(root), os.geteuid(), os.getegid(), 1001, 1001
            )
            self.assertTrue(ok, error)
            self.assertEqual(root.stat().st_uid, os.geteuid())
            self.assertEqual(record.stat().st_uid, os.geteuid())
            self.assertEqual(record.read_bytes(), payload)
            self.assertEqual(record.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
