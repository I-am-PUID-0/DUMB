import copy
import json
import os
from pathlib import Path
import subprocess
import types
import unittest

from utils import arrangement_pgadmin as adapter


class CLI:
    def __init__(self):
        self.servers = {
            "1": {
                "Name": "User-owned",
                "Group": "Servers",
                "Host": "db.example.com",
                "Port": 5433,
                "Username": "user",
                "MaintenanceDB": "custom",
                "ConnectionParameters": {"sslmode": "verify-full"},
                "PasswordExecCommand": "user-choice",
            }
        }
        self.calls = []
        self.drop_import = False
        self.invalid_export = False

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        command, path = argv[2], Path(argv[3])
        if command == "dump-servers":
            path.write_text(
                "CLI failed silently"
                if self.invalid_export
                else json.dumps({"Servers": self.servers})
            )
        elif command == "load-servers" and not self.drop_import:
            rows = json.loads(path.read_text())["Servers"]
            for row in rows.values():
                self.servers[str(len(self.servers) + 1)] = row
        return types.SimpleNamespace(returncode=0)


class PgAdminTests(unittest.TestCase):
    def setUp(self):
        self.cli = CLI()
        self.cfg = {"setup_email": "operator@example.com"}
        self.details = {
            "name": "DUMB",
            "group": "DUMB",
            "host": "127.0.0.1",
            "port": 5432,
            "username": "DUMB",
            "maintenance_db": "postgres",
        }

    def ensure(self, apply=False):
        return adapter.ensure_server(
            self.cfg,
            self.details,
            apply=apply,
            runner=self.cli,
            cli=("/runtime/python", "/runtime/setup.py"),
        )

    @unittest.skipUnless(
        os.geteuid() == 0, "Assigning a different service owner requires root"
    )
    def test_cli_and_private_files_use_configured_unprivileged_service_owner(self):
        self.cfg.update({"puid": 12345, "pgid": 12346})
        original = self.cli
        seen = []

        def check_owner(argv, **kwargs):
            path = Path(argv[3])
            seen.append(
                (
                    path.stat().st_uid,
                    path.stat().st_gid,
                    path.stat().st_mode & 0o777,
                    kwargs.get("user"),
                    kwargs.get("group"),
                )
            )
            return original(argv, **kwargs)

        self.assertTrue(
            adapter.ensure_server(
                self.cfg,
                self.details,
                apply=True,
                runner=check_owner,
                cli=("python", "setup.py"),
            )
        )
        self.assertTrue(seen)
        self.assertEqual({(12345, 12346, 0o600, 12345, 12346)}, set(seen))

    def test_import_readback_idempotence_and_preservation(self):
        before = copy.deepcopy(self.cli.servers)
        self.assertFalse(self.ensure())
        self.assertTrue(self.ensure(True))
        self.assertEqual(before["1"], self.cli.servers["1"])
        self.cli.calls.clear()
        self.assertTrue(self.ensure(True))
        self.assertEqual(["dump-servers"], [call[0][2] for call in self.cli.calls])
        self.assertEqual(2, len(self.cli.servers))
        for argv, kwargs in self.cli.calls:
            self.assertNotIn("--replace", argv)
            self.assertEqual(subprocess.DEVNULL, kwargs["stdout"])
            self.assertEqual(30, kwargs["timeout"])
            self.assertIn("--user", argv)
            self.assertFalse(Path(argv[3]).exists())

    def test_same_name_conflict_does_not_replace_user_connection(self):
        self.details.update({"name": "User-owned", "group": "Servers"})
        before = copy.deepcopy(self.cli.servers)
        with self.assertRaisesRegex(adapter.PgAdminSetupError, "different connection"):
            self.ensure(True)
        self.assertEqual(before, self.cli.servers)

    def test_existing_legacy_group_target_is_reused_without_duplicate(self):
        self.cli.servers["1"].update(
            {
                "Host": "127.0.0.1",
                "Port": 5432,
                "Username": "DUMB",
                "MaintenanceDB": "postgres",
            }
        )
        before = copy.deepcopy(self.cli.servers)
        self.assertTrue(self.ensure(True))
        self.assertEqual(before, self.cli.servers)
        self.assertEqual(["dump-servers"], [call[0][2] for call in self.cli.calls])

    def test_cli_zero_exit_with_no_import_is_not_success(self):
        self.cli.drop_import = True
        with self.assertRaisesRegex(adapter.PgAdminSetupError, "did not retain"):
            self.ensure(True)

    def test_cli_zero_exit_invalid_dump_is_not_success(self):
        self.cli.invalid_export = True
        with self.assertRaisesRegex(adapter.PgAdminSetupError, "did not export"):
            self.ensure(True)
        self.assertFalse(any(call[0][2] == "load-servers" for call in self.cli.calls))

    def test_existing_saved_password_options_and_duplicate_prevention(self):
        self.assertTrue(self.ensure(True))
        self.cli.servers["2"]["ConnectionParameters"][
            "passfile"
        ] = "user-selected.pgpass"
        self.cli.servers["2"]["Comment"] = "My comment"
        self.assertTrue(self.ensure(True))
        self.assertEqual(2, len(self.cli.servers))
        self.assertEqual(
            "user-selected.pgpass",
            self.cli.servers["2"]["ConnectionParameters"]["passfile"],
        )

    def test_adapter_preview_export_only_and_apply_verified(self):
        action = {
            "source": "pgadmin",
            "service_key": "pgadmin",
            "action": "postgres_connection",
        }
        config = {
            "pgadmin": self.cfg,
            "postgres": {"enabled": True, "user": "DUMB", "port": 5432},
        }
        preview = adapter.inspect_pgadmin(
            action, config, {}, runner=self.cli, cli=("python", "setup.py")
        )
        self.assertEqual("planned", preview["execution"])
        self.assertEqual(1, len(self.cli.servers))
        self.assertEqual([], self.cli.calls)
        applied = adapter.apply_pgadmin(
            action, config, {}, runner=self.cli, cli=("python", "setup.py")
        )
        self.assertEqual(
            ("applied", "passed"), (applied["execution"], applied["verification"])
        )
        self.assertNotIn("password", self.cli.servers["2"])

    def test_invalid_port_and_missing_user_fail_before_import(self):
        self.details["port"] = 0
        # Zero is not a valid explicitly chosen port.
        self.details["port"] = 65536
        with self.assertRaises(adapter.PgAdminSetupError):
            self.ensure(True)
        self.cfg["setup_email"] = ""
        with self.assertRaises(adapter.PgAdminSetupError):
            self.ensure(True)
        self.assertFalse(self.cli.calls)
