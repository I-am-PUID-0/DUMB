import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import infinidysk_v2_pg_restore as restore_helper
from utils import infinidysk_v2_restore as restore


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dump = self.root / "backup.dump"
        self.dump.write_bytes(b"fixture custom dump")
        self.binding = {
            "binding_fingerprint": "a" * 64,
            "physical_identity": {"system_identifier": "111", "database_oid": "11"},
        }
        self.proof = {
            "verified": True,
            "source_binding": "a" * 64,
            "tables_verified": 8,
            "restored_identity": {"system_identifier": "222", "database_oid": "22"},
        }
        self.completed = subprocess.CompletedProcess(
            ["private-helper"], 0, json.dumps(self.proof).encode(), b""
        )
        self.supervisor = Mock(return_value=self.completed)
        self.patches = [
            patch.object(restore.os, "geteuid", return_value=0),
            patch.object(
                restore, "postgres_tools", return_value=Path("/private/tools")
            ),
            patch.object(
                restore, "namespace_command", side_effect=lambda uid, gid, cmd: cmd
            ),
            patch.object(restore, "run_migration_process", self.supervisor),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    def test_restore_binds_independent_proof_and_copies_only_in_owned_child(self):
        payloads = []

        def supervise(command, **kwargs):
            payload = json.loads(Path(command[-1]).read_text())
            payloads.append(payload)
            self.assertNotIn("source-private-password", json.dumps(payload))
            self.assertEqual((payload["uid"], payload["gid"]), (1000, 1000))
            self.assertEqual(payload["dump_path"], str(self.dump))
            self.assertEqual(
                payload["dump_identity"], restore._dump_identity(self.dump.stat())
            )
            self.assertFalse(Path(payload["root"], "backup.dump").exists())
            self.assertNotIn("timeout", kwargs)
            self.assertNotIn("deadline", kwargs)
            self.assertEqual(kwargs["idle_warning_seconds"], 1800)
            self.assertEqual(
                [Path(path).name for path in kwargs["activity_paths"]],
                ["copy-progress.json", "restore-progress.log"],
            )
            return self.completed

        self.supervisor.side_effect = supervise
        result = restore.verify_postgres_restore(
            str(self.dump), self.binding, {"password": "source-private-password"}
        )
        self.assertEqual(result, self.proof)
        self.assertTrue(payloads)
        self.assertFalse(Path(payloads[0]["root"]).exists())
        self.assertEqual(self.dump.read_bytes(), b"fixture custom dump")

    def test_source_libpq_environment_is_not_inherited_by_private_restore(self):
        def supervise(command, **kwargs):
            root = json.loads(Path(command[-1]).read_text())["root"]
            environment = kwargs["env"]
            self.assertEqual(
                set(environment),
                {"PATH", "LANG", "LC_ALL", "HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE"},
            )
            self.assertEqual(environment["HOME"], root)
            self.assertEqual(environment["TMPDIR"], root)
            self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertNotIn("source-private-password", json.dumps(environment))
            return self.completed

        self.supervisor.side_effect = supervise
        with patch.dict(
            os.environ,
            {
                "PGPORT": "6543",
                "PGOPTIONS": "-c statement_timeout=1 -c transaction_timeout=1",
                "PGPASSWORD": "source-private-password",
                "PGSERVICE": "source-private-service",
                "PGPASSFILE": "/reserved-example/source.pgpass",
            },
        ):
            result = restore.verify_postgres_restore(str(self.dump), self.binding, {})
        self.assertEqual(result, self.proof)

    def test_wrong_binding_original_identity_or_missing_verification_is_rejected(self):
        for change in (
            {"source_binding": "other"},
            {"restored_identity": self.binding["physical_identity"]},
            {"restored_identity": None},
            {"verified": False},
        ):
            with self.subTest(change=change):
                self.supervisor.return_value = subprocess.CompletedProcess(
                    ["private-helper"],
                    0,
                    json.dumps({**self.proof, **change}).encode(),
                    b"",
                )
                with self.assertRaisesRegex(ValueError, "binding"):
                    restore.verify_postgres_restore(str(self.dump), self.binding, {})

    def test_long_quiet_restore_warns_and_keeps_running_without_exposing_private_output(
        self,
    ):
        reports = []

        def supervise(command, **kwargs):
            kwargs["progress"](
                {"elapsed_seconds": 7200, "idle_seconds": 1900, "stall_warning": True}
            )
            return self.completed

        self.supervisor.side_effect = supervise
        result = restore.verify_postgres_restore(
            str(self.dump),
            self.binding,
            {},
            progress=lambda *args: reports.append(args),
            idle_warning_seconds=60,
        )
        self.assertEqual(result, self.proof)
        self.assertIn("7,200 seconds elapsed", reports[0][1])
        self.assertIn("still running", reports[0][1])
        self.assertEqual(reports[0][0], "postgres_restore_verification")
        self.assertEqual(self.supervisor.call_args.kwargs["idle_warning_seconds"], 60)
        self.assertNotIn(str(self.dump), reports[0][1])

    def test_cancellation_propagates_and_confirmed_cleanup_removes_private_scratch(
        self,
    ):
        class Cancelled(RuntimeError):
            pass

        cancelled = Cancelled("fixture cancellation")
        paths = []

        def check_cancel():
            raise cancelled

        def supervise(command, **kwargs):
            paths.append(Path(json.loads(Path(command[-1]).read_text())["root"]))
            kwargs["check_cancel"]()

        self.supervisor.side_effect = supervise
        with self.assertRaises(Cancelled) as caught:
            restore.verify_postgres_restore(
                str(self.dump), self.binding, {}, check_cancel=check_cancel
            )
        self.assertIs(caught.exception, cancelled)
        self.assertFalse(paths[0].exists())
        self.assertTrue(self.dump.exists())

    def test_unconfirmed_child_cleanup_retains_private_scratch_and_typed_error(self):
        class CleanupIncomplete(RuntimeError):
            cleanup_complete = False

        failure = CleanupIncomplete("fixture child remains")
        failure.cleanup_evidence = {"process_group": 987654}
        paths = []

        def supervise(command, **kwargs):
            paths.append(Path(json.loads(Path(command[-1]).read_text())["root"]))
            raise failure

        self.supervisor.side_effect = supervise
        try:
            with patch.object(
                restore, "MigrationProcessCleanupError", CleanupIncomplete
            ):
                with self.assertRaises(CleanupIncomplete) as caught:
                    restore.verify_postgres_restore(str(self.dump), self.binding, {})
            self.assertIs(caught.exception, failure)
            self.assertTrue(paths[0].is_dir())
            self.assertTrue((paths[0] / "payload.json").is_file())
            self.assertTrue(self.dump.exists())
            self.assertEqual(
                failure.cleanup_evidence["restore_scratch"],
                {
                    "path": str(paths[0]),
                    "identity": [
                        paths[0].stat().st_dev,
                        paths[0].stat().st_ino,
                        paths[0].stat().st_mode & 0o170000,
                    ],
                },
            )
        finally:
            for path in paths:
                shutil.rmtree(path, ignore_errors=True)

    def test_missing_dump_is_rejected_before_spawning(self):
        with self.assertRaisesRegex(ValueError, "direct"):
            restore.verify_postgres_restore(
                str(self.root / "missing.dump"), self.binding, {}
            )
        self.supervisor.assert_not_called()

    def test_redirected_dump_is_rejected_before_spawning(self):
        link = self.root / "link.dump"
        link.symlink_to(self.dump)
        with self.assertRaisesRegex(ValueError, "direct"):
            restore.verify_postgres_restore(str(link), self.binding, {})
        self.supervisor.assert_not_called()


class RestoreHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "retained.dump"
        self.source.write_bytes(b"retained fixture backup")
        self.destination = self.root / "copy.dump"

    def tearDown(self):
        self.temp.cleanup()

    def test_owned_copy_preserves_all_bytes_and_reports_actual_copy_progress(self):
        events = []
        restore_helper._copy_dump(
            self.source,
            self.destination,
            restore_helper._dump_identity(self.source.stat()),
            events.append,
        )
        self.assertEqual(self.destination.read_bytes(), self.source.read_bytes())
        self.assertEqual(events, [0, self.source.stat().st_size])
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)

    def test_redirected_or_replaced_copy_source_is_rejected_before_writing(self):
        identity = restore_helper._dump_identity(self.source.stat())
        link = self.root / "redirect.dump"
        link.symlink_to(self.source)
        with self.assertRaises(OSError):
            restore_helper._copy_dump(
                link, self.destination, identity, lambda count: None
            )
        self.assertFalse(self.destination.exists())
        identity[1] += 1
        with self.assertRaisesRegex(ValueError, "before copying"):
            restore_helper._copy_dump(
                self.source, self.destination, identity, lambda count: None
            )
        self.assertFalse(self.destination.exists())

    def test_copy_source_mutation_is_rejected_before_restore(self):
        identity = restore_helper._dump_identity(self.source.stat())

        def change_source(count):
            if count == 0:
                self.source.write_bytes(b"changed source")

        with self.assertRaisesRegex(ValueError, "while copying"):
            restore_helper._copy_dump(
                self.source, self.destination, identity, change_source
            )

    def test_copy_refuses_existing_destination(self):
        self.destination.write_bytes(b"preserve existing")
        with self.assertRaises(FileExistsError):
            restore_helper._copy_dump(
                self.source,
                self.destination,
                restore_helper._dump_identity(self.source.stat()),
                lambda count: None,
            )
        self.assertEqual(self.destination.read_bytes(), b"preserve existing")

    def test_bulk_init_restore_and_verification_have_no_elapsed_deadline(self):
        private = self.root / "private"
        private.mkdir()
        payload = {
            "root": str(private),
            "tools": "/reserved-example/postgres",
            "uid": os.geteuid(),
            "gid": os.getegid(),
            "source_binding": {"fixture": True},
            "dump_path": str(self.source),
            "dump_identity": restore_helper._dump_identity(self.source.stat()),
        }
        request = private / "payload.json"
        request.write_text(json.dumps(payload))
        server = Mock()
        server.poll.return_value = None
        server.wait.return_value = 0
        connections = [Mock(), Mock()]
        connections[0].cursor.return_value.__enter__ = Mock(return_value=Mock())
        connections[0].cursor.return_value.__exit__ = Mock(return_value=False)
        proof = {"verified": True, "fixture": "bound result"}
        calls, stages = [], []
        original_copy = restore_helper._copy_dump

        def copy_dump(*args):
            stages.append("copy")
            return original_copy(*args)

        def confine(*args):
            stages.append("confine")

        def run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0)

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(restore_helper.sys, "argv", ["helper", str(request)])
            )
            for name in ("setgroups", "setgid", "setuid", "chown"):
                stack.enter_context(patch.object(restore_helper.os, name))
            stack.enter_context(patch.object(restore_helper.signal, "signal"))
            stack.enter_context(
                patch.object(restore_helper, "confine", side_effect=confine)
            )
            stack.enter_context(
                patch.object(restore_helper, "_copy_dump", side_effect=copy_dump)
            )
            stack.enter_context(
                patch.object(restore_helper.subprocess, "run", side_effect=run)
            )
            stack.enter_context(
                patch.object(restore_helper.subprocess, "Popen", return_value=server)
            )
            stack.enter_context(
                patch.object(
                    restore_helper.psycopg2, "connect", side_effect=connections
                )
            )
            stack.enter_context(
                patch.object(
                    restore_helper, "verify_restored_connection", return_value=proof
                )
            )
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            restore_helper.main()
            connected_ports = [
                call.kwargs["port"]
                for call in restore_helper.psycopg2.connect.call_args_list
            ]
        self.assertEqual(json.loads(output.getvalue()), proof)
        self.assertEqual(
            (private / "backup.dump").read_bytes(), self.source.read_bytes()
        )
        self.assertEqual(
            [Path(command[0]).name for command, _ in calls],
            ["initdb", "pg_restore", "pg_ctl"],
        )
        self.assertNotIn("timeout", calls[0][1])
        self.assertNotIn("timeout", calls[1][1])
        self.assertIn("--verbose", calls[1][0])
        self.assertEqual(calls[1][0][calls[1][0].index("-p") + 1], "5432")
        self.assertEqual(stages, ["confine", "copy"])
        self.assertEqual(connected_ports, [5432, 5432])
        self.assertEqual(Path(calls[1][1]["stderr"].name).name, "restore-progress.log")
        self.assertEqual(calls[2][1]["timeout"], 15)
        self.assertEqual(
            json.loads((private / "copy-progress.json").read_text())["stage"],
            "verified",
        )
        server.wait.assert_called_once_with(timeout=5)


if __name__ == "__main__":
    unittest.main()
