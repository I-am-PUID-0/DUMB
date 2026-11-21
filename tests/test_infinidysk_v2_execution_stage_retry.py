"""DUMB verification retries retain the already completed native conversion."""

from contextlib import contextmanager
import errno
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from tests import test_infinidysk_v2_execution as execution_fixtures
from utils.infinidysk_v2_execution import ContainedImportError, InfiniDyskV2Executor


class ExecutionStageRetryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = execution_fixtures.ExecutionTests(
            methodName="test_prepare_then_activate_reuses_one_native_import_and_keeps_apps_paused"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.operations = []

        def retry(operation, callback):
            self.operations.append(operation)
            try:
                return callback()
            except OSError:
                return callback()

        self.fixture.workspace["run_stage"] = retry

    def test_inventory_retry_keeps_snapshot_and_native_conversion_completed(self):
        report = {"files": 0, "history": 0, "skipped_files": 0, "failed_files": 0}
        with patch(
            "utils.infinidysk_v2_execution.verify_import_inventory",
            side_effect=[OSError(errno.EIO, "temporary input read"), report],
        ) as verify:
            receipt = self.fixture.executor.prepare(
                self.fixture.plan, self.fixture.workspace, self.fixture.progress
            )
        self.assertTrue(receipt["candidate_prepared"])
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(len(self.fixture.snapshots), 1)
        self.assertEqual(self.fixture.native.call_count, 1)
        self.assertEqual(self.fixture.candidate.wait_import.call_count, 1)
        self.assertEqual(self.operations, ["verify_import"])
        self.assertFalse(any(row[0] == "start" for row in self.fixture.handler.calls))

    def test_inventory_retry_recreates_each_external_census_scope(self):
        external = self.fixture.root / "external-blobs"
        external.mkdir()
        self.fixture.plan["options"] = {"offload_legacy_blobs": True}
        self.fixture.workspace["external_blob_source"] = Mock(return_value=external)
        entered, exited = [], []

        @contextmanager
        def census():
            identity = object()
            entered.append(identity)
            try:
                yield external, Mock()
            finally:
                exited.append(identity)

        self.fixture.workspace["source_blob_census"] = census
        report = {"files": 0, "history": 0, "skipped_files": 0, "failed_files": 0}
        attempts = 0

        def verify_after_readback(*args, source_blob_census, **kwargs):
            nonlocal attempts
            attempts += 1
            source_blob_census(external, "fixture-identifier")
            if attempts == 1:
                raise OSError(errno.ETIMEDOUT, "temporary read")
            return report

        with patch(
            "utils.infinidysk_v2_execution.verify_import_inventory",
            side_effect=verify_after_readback,
        ) as verify:
            self.fixture.executor.prepare(
                self.fixture.plan, self.fixture.workspace, self.fixture.progress
            )
        self.assertEqual(entered, exited)
        self.assertEqual(len(entered), 2)
        self.assertIsNot(entered[0], entered[1])
        self.assertEqual(self.operations, ["external_input", "verify_import"])
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(self.fixture.native.call_count, 1)

    def test_unconfirmed_native_shutdown_never_enters_retryable_verification(self):
        self.fixture.candidate.stopped = False
        with self.assertRaisesRegex(ContainedImportError, "shutdown is unverified"):
            self.fixture.executor.prepare(
                self.fixture.plan, self.fixture.workspace, self.fixture.progress
            )
        self.assertEqual(self.operations, [])

    def test_postgres_restore_retry_does_not_repeat_snapshot_creation(self):
        self.fixture.plan["source"]["provider"] = "postgres"
        proof = {"verified": True}
        dump = str(self.fixture.root / "captured.dump")
        binding = {"binding_fingerprint": "a" * 64}

        def create(plan, workspace, *, verify_restore, **kwargs):
            return verify_restore(dump, binding, {})

        with (
            patch(
                "utils.infinidysk_v2_execution.create_postgres_snapshot",
                side_effect=create,
            ) as capture,
            patch(
                "utils.infinidysk_v2_execution.verify_postgres_restore",
                side_effect=[
                    OSError(errno.ENOSPC, "temporary scratch pressure"),
                    proof,
                ],
            ) as restore,
            patch(
                "utils.infinidysk_v2_execution.postgres_tools",
                return_value=Path("/fixture-tools"),
            ),
        ):
            result = InfiniDyskV2Executor._postgres_snapshot(
                self.fixture.plan, self.fixture.workspace, self.fixture.progress
            )
        self.assertIs(result, proof)
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(restore.call_count, 2)
        self.assertEqual(self.operations, ["postgres_restore"])

    def test_resumed_restore_does_not_rethrow_a_recovered_progress_error(self):
        def create(plan, workspace, *, verify_restore, **kwargs):
            verify_restore("/fixture.dump", {}, {})
            raise ValueError("later snapshot validation failure")

        def restore(*args, **kwargs):
            kwargs["progress"]("postgres_restore_verification", "Fixture restore")
            return {"verified": True}

        self.fixture.progress.side_effect = [
            OSError(errno.ENOSPC, "temporary progress checkpoint pressure"),
            None,
        ]
        with (
            patch(
                "utils.infinidysk_v2_execution.create_postgres_snapshot",
                side_effect=create,
            ),
            patch(
                "utils.infinidysk_v2_execution.verify_postgres_restore",
                side_effect=restore,
            ) as verify,
            patch(
                "utils.infinidysk_v2_execution.postgres_tools",
                return_value=Path("/fixture-tools"),
            ),
            self.assertRaisesRegex(ValueError, "later snapshot validation failure"),
        ):
            InfiniDyskV2Executor._postgres_snapshot(
                self.fixture.plan, self.fixture.workspace, self.fixture.progress
            )
        self.assertEqual(verify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
