"""Converter initialization must finish before importing or accepting history."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from utils.infinidysk_v2_execution import (
    ContainedImportError,
    prepare_import_history,
)
from utils.infinidysk_v2_subprocess import MigrationProcessCleanupError


class HistoryInitializationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data, self.private = self.root / "candidate", self.root / "private"
        self.data.mkdir()
        self.private.mkdir()
        self.source = self.data / "db.sqlite"
        self.source.write_bytes(b"retained source fixture")
        self.events = []
        self.worker = Mock()
        self.worker.process = None
        self.worker.stopped = False
        self.worker.check_cancel = lambda: None
        self.worker.__enter__ = Mock(return_value=self.worker)

        def stopped(*unused):
            self.events.append("stopped")
            self.worker.stopped = True
            return False

        self.worker.__exit__ = Mock(side_effect=stopped)
        self.service = {
            "v2_runtime_dir": "/opt/example-converter",
            "v2_data_dir": str(self.data),
            "env": {"INFINIDYSK__ARR_INSTANCES": "untrusted", "TZ": "UTC"},
        }

    def initialize(self):
        import os

        return prepare_import_history(
            self.service, self.data, self.private, os.geteuid(), os.getegid(), "UTC"
        )

    def test_only_the_empty_schema_is_copied_after_shutdown(self):
        def start(service, data, control, uid, gid, timezone, legacy, **unused):
            self.assertNotEqual(Path(data), self.data)
            self.assertFalse((Path(data) / "db.sqlite").exists())
            self.assertEqual(
                service["env"],
                {
                    "TZ": "UTC",
                    "INFINIDYSK__SAB__CATEGORIES": "[]",
                    "INFINIDYSK__SAB__MANUAL_CATEGORY": "*",
                },
            )
            self.assertEqual(legacy, {"DATABASE_PROVIDER": "sqlite"})
            self.assertEqual(service["v2_runtime_dir"], self.service["v2_runtime_dir"])
            (Path(data) / "infinidysk.sqlite").write_bytes(b"native empty schema")
            self.events.append("initialized")
            return self.worker

        def guard(source, destination, *, native_stopped, uid, gid):
            self.assertTrue(native_stopped)
            self.assertEqual(self.events, ["initialized", "stopped"])
            self.assertEqual(source.read_bytes(), b"native empty schema")
            self.assertEqual(destination, self.data / "infinidysk.sqlite")
            self.events.append("guarded")
            return {"version": 1}

        with (
            patch("utils.infinidysk_v2_execution.native_auth"),
            patch("utils.infinidysk_v2_execution.NativeCandidate", side_effect=start),
            patch(
                "utils.infinidysk_v2_execution.prepare_history_guard", side_effect=guard
            ),
        ):
            self.assertEqual(self.initialize(), {"version": 1})
        self.worker.wait_import.assert_called_once()
        self.assertIs(self.worker.wait_import.call_args.args[0], False)
        self.assertEqual(self.source.read_bytes(), b"retained source fixture")
        self.assertEqual(list(self.data.iterdir()), [self.source])
        self.assertFalse(list(self.private.iterdir()))

    def test_unverified_shutdown_cannot_publish_the_bootstrap(self):
        self.worker.__exit__.side_effect = None
        with (
            patch("utils.infinidysk_v2_execution.native_auth"),
            patch(
                "utils.infinidysk_v2_execution.NativeCandidate",
                return_value=self.worker,
            ),
            patch("utils.infinidysk_v2_execution.prepare_history_guard") as guard,
        ):
            with self.assertRaisesRegex(ContainedImportError, "unverified writers"):
                self.initialize()
        guard.assert_not_called()

    def test_failed_bootstrap_cannot_publish_a_target(self):
        self.worker.wait_import.side_effect = ContainedImportError(
            "fixture bootstrap failure"
        )
        with (
            patch("utils.infinidysk_v2_execution.native_auth"),
            patch(
                "utils.infinidysk_v2_execution.NativeCandidate",
                return_value=self.worker,
            ),
            patch("utils.infinidysk_v2_execution.prepare_history_guard") as guard,
        ):
            with self.assertRaisesRegex(ContainedImportError, "bootstrap failure"):
                self.initialize()
        guard.assert_not_called()
        self.assertEqual(list(self.data.iterdir()), [self.source])

    def test_live_bootstrap_retains_both_roots_and_cannot_publish(self):
        self.worker.process = Mock(pid=123456789)
        self.worker.process.poll.return_value = None
        self.worker.wait_import.side_effect = ContainedImportError("fixture failure")
        with (
            patch("utils.infinidysk_v2_execution.native_auth"),
            patch(
                "utils.infinidysk_v2_execution.NativeCandidate",
                return_value=self.worker,
            ),
            patch("utils.infinidysk_v2_execution.prepare_history_guard") as guard,
        ):
            with self.assertRaises(MigrationProcessCleanupError) as raised:
                self.initialize()
        self.assertIs(raised.exception.cleanup_complete, False)
        self.assertEqual(len(list(self.data.glob(".dumb-history-bootstrap-*"))), 1)
        self.assertEqual(len(list(self.private.glob(".history-bootstrap-*"))), 1)
        self.assertEqual(self.source.read_bytes(), b"retained source fixture")
        guard.assert_not_called()


class HistoryAcceptanceOrderTests(unittest.TestCase):
    def setUp(self):
        from tests.test_infinidysk_v2_execution import ExecutionTests

        self.fixture = ExecutionTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_guard_is_removed_after_native_stops_and_before_inventory(self):
        events = []
        with (
            patch(
                "utils.infinidysk_v2_execution.prepare_import_history",
                return_value={"version": 1},
            ) as initialize,
            patch(
                "utils.infinidysk_v2_execution.remove_history_guard",
                side_effect=lambda *a, **k: events.append(
                    ("remove", k["native_stopped"])
                ),
            ),
            patch(
                "utils.infinidysk_v2_execution.verify_import_inventory",
                side_effect=lambda *a, **k: events.append(("verify", True))
                or {"files": 0, "history": 0},
            ),
        ):
            self.fixture.executor.prepare(
                self.fixture.plan, self.fixture.workspace, self.fixture.progress
            )
        initialize.assert_called_once()
        self.assertEqual(events, [("remove", True), ("verify", True)])

    def test_guard_refusal_cannot_be_accepted_or_offered_for_retry(self):
        with patch(
            "utils.infinidysk_v2_execution.remove_history_guard",
            side_effect=ValueError("changed history guard"),
        ):
            with self.assertRaisesRegex(ValueError, "changed history guard"):
                self.fixture.executor.prepare(
                    self.fixture.plan, self.fixture.workspace, self.fixture.progress
                )
        self.fixture.workspace["accept_import"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
