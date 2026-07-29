import importlib
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


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


if __name__ == "__main__":
    unittest.main()
