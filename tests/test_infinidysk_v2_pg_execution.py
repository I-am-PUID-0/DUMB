"""Callback identity survives the snapshot's redacting restore boundary."""

from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from utils import infinidysk_v2_execution as execution
from utils.infinidysk_v2_subprocess import MigrationProcessCleanupError


class Cancelled(Exception):
    pass


class PostgresExecutionCallbacks(unittest.TestCase):
    def test_restore_receives_the_active_job_cancel_and_progress_callbacks(self):
        cancel, progress = Mock(), Mock()
        binding, settings = {"binding": "fixture"}, {"password": "reserved-fixture"}

        def snapshot(plan, workspace, **kwargs):
            kwargs["check_cancel"]()
            return kwargs["verify_restore"]("dump", binding, settings)

        def verify(dump, actual_binding, actual_settings, **kwargs):
            self.assertEqual(
                (dump, actual_binding, actual_settings), ("dump", binding, settings)
            )
            kwargs["check_cancel"]()
            kwargs["progress"]("restore", "Still running.")
            return {"verified": True}

        with (
            patch.object(execution, "create_postgres_snapshot", side_effect=snapshot),
            patch.object(execution, "verify_postgres_restore", side_effect=verify),
            patch.object(
                execution, "postgres_tools", return_value=Path("/reserved/tools")
            ),
        ):
            result = execution.InfiniDyskV2Executor._postgres_snapshot(
                {}, {"check_cancel": cancel}, progress
            )
        self.assertTrue(result["verified"])
        self.assertEqual(cancel.call_count, 2)
        progress.assert_called_once_with("restore", "Still running.")

    def test_restore_cancel_survives_snapshot_error_redaction(self):
        cancelled = Cancelled("cancel requested")

        def snapshot(plan, workspace, **kwargs):
            try:
                kwargs["verify_restore"]("dump", {}, {})
            except Cancelled:
                raise execution.InfiniDyskV2PostgresSnapshotError("redacted") from None

        def verify(*args, **kwargs):
            kwargs["check_cancel"]()

        with (
            patch.object(execution, "create_postgres_snapshot", side_effect=snapshot),
            patch.object(execution, "verify_postgres_restore", side_effect=verify),
            patch.object(
                execution, "postgres_tools", return_value=Path("/reserved/tools")
            ),
        ):
            with self.assertRaises(Cancelled) as caught:
                execution.InfiniDyskV2Executor._postgres_snapshot(
                    {}, {"check_cancel": Mock(side_effect=cancelled)}, Mock()
                )
        self.assertIs(caught.exception, cancelled)

    def test_unconfirmed_cleanup_takes_priority_over_cancellation(self):
        cancelled = Cancelled("cancel requested")
        cleanup = MigrationProcessCleanupError(
            "owned worker pending", process_group=123
        )

        def snapshot(plan, workspace, **kwargs):
            try:
                kwargs["check_cancel"]()
            except Cancelled:
                raise cleanup

        with (
            patch.object(execution, "create_postgres_snapshot", side_effect=snapshot),
            patch.object(
                execution, "postgres_tools", return_value=Path("/reserved/tools")
            ),
        ):
            with self.assertRaises(MigrationProcessCleanupError) as caught:
                execution.InfiniDyskV2Executor._postgres_snapshot(
                    {}, {"check_cancel": Mock(side_effect=cancelled)}, Mock()
                )
        self.assertIs(caught.exception, cleanup)


class NativeCleanupRecovery(unittest.TestCase):
    def candidate(self):
        candidate = object.__new__(execution.NativeCandidate)
        candidate.process = Mock(pid=123)
        candidate.process.poll.return_value = 0
        candidate.output = Mock()
        candidate.payload_path = Mock()
        candidate.nntp_bridge = Mock()
        candidate.stopped = False
        return candidate

    def test_native_reap_timeout_preserves_recovery_hold(self):
        candidate = self.candidate()
        candidate.process.wait.side_effect = execution.subprocess.TimeoutExpired(
            "reserved-helper", 5
        )
        with patch.object(execution.os, "killpg"):
            with self.assertRaises(MigrationProcessCleanupError) as caught:
                candidate.__exit__(None, None, None)
        self.assertEqual(caught.exception.cleanup_evidence, {"process_group": 123})
        self.assertFalse(candidate.stopped)
        candidate.nntp_bridge.close.assert_called_once()

    def test_native_unconfirmed_membership_preserves_recovery_hold(self):
        candidate = self.candidate()
        with (
            patch.object(execution.os, "killpg"),
            patch.object(
                execution,
                "_native_group_has_live_members",
                side_effect=execution.ContainedImportError("membership unavailable"),
            ),
        ):
            with self.assertRaises(MigrationProcessCleanupError) as caught:
                candidate.__exit__(None, None, None)
        self.assertFalse(caught.exception.cleanup_complete)
        self.assertFalse(candidate.stopped)


if __name__ == "__main__":
    unittest.main()
