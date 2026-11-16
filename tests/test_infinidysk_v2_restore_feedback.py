"""Preserve real source locales and publish only authored restore diagnostics."""

import contextlib
import errno
import os
import subprocess
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts import infinidysk_v2_pg_restore as helper
from tests import test_infinidysk_v2_restore as restore_fixtures
from utils import infinidysk_v2_restore as restore
from utils.infinidysk_v2_subprocess import MigrationProcessError


class LocaleTests(unittest.TestCase):
    def test_source_locale_and_encoding_are_recreated_as_sql_parameters(self):
        for characteristics in (
            ["UTF8", "C", "C"],
            ["UTF8", "C.UTF-8", "C.UTF-8"],
            ["SQL_ASCII", "C", "C"],
            ["UTF8", "locale'with_quote", "locale'with_quote"],
        ):
            with self.subTest(characteristics=characteristics):
                connection = MagicMock()
                helper._create_verification_database(
                    connection, {"database_characteristics": characteristics}
                )
                connection.cursor.return_value.__enter__.return_value.execute.assert_called_once_with(
                    "CREATE DATABASE verification TEMPLATE template0 ENCODING %s LC_COLLATE %s LC_CTYPE %s",
                    tuple(characteristics),
                )
                self.assertTrue(connection.autocommit)

    def test_missing_or_invalid_source_characteristics_fail_closed(self):
        for values in (None, [], ["UTF8", "C"], ["UTF8", "C", 1], ["UTF8", "", "C"]):
            connection = MagicMock()
            with self.subTest(values=values):
                with self.assertRaises(restore.InfiniDyskV2PostgresSnapshotError):
                    helper._create_verification_database(
                        connection, {"database_characteristics": values}
                    )
                connection.cursor.assert_not_called()


class RestoreFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.fixture = restore_fixtures.RestoreTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_owned_child_failure_retains_safe_stage_before_scratch_cleanup(self):
        paths = []

        def supervise(command, **kwargs):
            root = Path(json.loads(Path(command[-1]).read_text())["root"])
            paths.append(root)
            (root / "copy-progress.json").write_text(
                json.dumps(
                    {
                        "stage": "creating_database",
                        "failed": True,
                        "failure_code": "locale",
                        "private": "source-private-password",
                    }
                )
            )
            raise MigrationProcessError("source-private-password untrusted stderr")

        self.fixture.supervisor.side_effect = supervise
        with self.assertLogs(restore.__name__, level="ERROR") as logs:
            with self.assertRaises(restore.InfiniDyskV2PostgresSnapshotError) as caught:
                restore.verify_postgres_restore(
                    self.fixture.dump, self.fixture.binding, {}
                )
        message = str(caught.exception)
        self.assertIn("creating the verification database", message)
        self.assertIn("locale", message)
        self.assertIn("did not modify the source database", message)
        self.assertNotIn("source-private-password", message + str(logs.output))
        self.assertFalse(paths[0].exists())
        self.assertTrue(self.fixture.dump.exists())

    def test_invalid_or_missing_child_status_never_becomes_public_error_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for state in (
                {},
                {"stage": "source-private-password", "failure_code": "secret"},
            ):
                (root / "copy-progress.json").write_text(json.dumps(state))
                with self.assertLogs(restore.__name__, level="ERROR"):
                    error = restore._restore_failure(
                        root, MigrationProcessError("secret")
                    )
                self.assertIn("entering the isolated", str(error))
                self.assertNotIn("source-private-password", str(error))
                self.assertNotIn("secret", str(error))
            (root / "copy-progress.json").unlink()
            (root / "copy-progress.json").symlink_to(self.fixture.dump)
            self.assertEqual(restore._restore_status(root), {})

    def test_failure_codes_are_fixed_and_never_use_exception_messages(self):
        self.assertEqual(
            restore.restore_failure_code(OSError(errno.ENOSPC, "secret")), "storage"
        )
        self.assertEqual(
            restore.restore_failure_code(OSError(errno.EACCES, "secret")), "permissions"
        )
        self.assertEqual(restore.restore_failure_code(RuntimeError("secret")), "helper")


class RestoreChildStageTests(unittest.TestCase):
    def test_failed_child_records_owned_substage_without_private_exception_output(self):
        for failure_stage in (
            "initializing_database",
            "restoring_database",
            "verifying_database",
        ):
            with (
                self.subTest(failure_stage=failure_stage),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                dump = root / "original.dump"
                dump.write_bytes(b"retained dump bytes")
                private = root / "private"
                private.mkdir()
                payload = {
                    "root": str(private),
                    "tools": "/reserved-example/postgres",
                    "uid": os.geteuid(),
                    "gid": os.getegid(),
                    "source_binding": {
                        "database_characteristics": ["UTF8", "C.UTF-8", "C.UTF-8"]
                    },
                    "dump_path": str(dump),
                    "dump_identity": helper._dump_identity(dump.stat()),
                }
                request = private / "payload.json"
                request.write_text(json.dumps(payload))
                server, connection = MagicMock(), MagicMock()
                server.poll.return_value = None
                server.wait.return_value = 0

                def run(command, **kwargs):
                    stage = {
                        "initdb": "initializing_database",
                        "pg_restore": "restoring_database",
                    }.get(Path(command[0]).name)
                    if stage == failure_stage:
                        raise subprocess.CalledProcessError(
                            1, command, stderr=b"private-password untrusted"
                        )
                    return subprocess.CompletedProcess(command, 0)

                def compare(*args):
                    raise restore.InfiniDyskV2PostgresSnapshotError(
                        "private-password database row"
                    )

                with contextlib.ExitStack() as stack:
                    stack.enter_context(
                        patch.object(helper.sys, "argv", ["helper", str(request)])
                    )
                    for name in ("setgroups", "setgid", "setuid", "chown"):
                        stack.enter_context(patch.object(helper.os, name))
                    stack.enter_context(patch.object(helper.signal, "signal"))
                    stack.enter_context(patch.object(helper, "confine"))
                    stack.enter_context(
                        patch.object(helper.subprocess, "run", side_effect=run)
                    )
                    stack.enter_context(
                        patch.object(helper.subprocess, "Popen", return_value=server)
                    )
                    stack.enter_context(
                        patch.object(
                            helper.psycopg2, "connect", return_value=connection
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            helper, "verify_restored_connection", side_effect=compare
                        )
                    )
                    with self.assertRaises(
                        (
                            subprocess.CalledProcessError,
                            restore.InfiniDyskV2PostgresSnapshotError,
                        )
                    ):
                        helper.main()
                state = json.loads((private / "copy-progress.json").read_text())
                self.assertEqual(state["stage"], failure_stage)
                self.assertTrue(state["failed"])
                self.assertEqual(
                    state["failure_code"],
                    "mismatch" if failure_stage == "verifying_database" else "helper",
                )
                self.assertNotIn("private-password", json.dumps(state))
                self.assertEqual(dump.read_bytes(), b"retained dump bytes")
                if failure_stage != "initializing_database":
                    server.wait.assert_called_once_with(timeout=5)
