import importlib
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


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


if __name__ == "__main__":
    unittest.main()
