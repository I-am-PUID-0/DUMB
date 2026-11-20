"""A paused DUMB verification resumes its checkpoint, not the native import."""

import errno
import hashlib
import json
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from tests import test_infinidysk_v2_low_space_pipeline as pipeline_fixtures
from utils import infinidysk_v2_archive as archive_module
from utils import infinidysk_v2_execution as execution
from utils import infinidysk_v2_restore as restore
from utils.infinidysk_v2_inventory import tree_fingerprint
from utils.infinidysk_v2_migration import (
    InfiniDyskV2Cancelled,
    InfiniDyskV2MigrationError,
)
from utils.infinidysk_v2_pg_snapshot import InfiniDyskV2PostgresSnapshotError
from utils.infinidysk_v2_stage_retry import retryable_error


class StageRetryPipelineTests(unittest.TestCase):
    def setUp(self):
        # Keep real journal, archive, low-space recovery and inventory checks;
        # only the established fixture's process/native boundaries are mocked.
        self.pipeline = pipeline_fixtures.LowSpacePipelineTests("runTest")
        self.pipeline.setUp()
        self.addCleanup(self.pipeline.doCleanups)
        self.manager = self.pipeline.manager
        self.addCleanup(self.finish_owned_worker)

    def finish_owned_worker(self):
        worker = self.manager._thread
        if worker is None or not worker.is_alive():
            return
        job = self.manager.status().get("job") or {}
        if job.get("can_cancel"):
            self.manager.cancel(
                {
                    "job_id": job["job_id"],
                    "confirmation": job["cancel_confirmation_text"],
                }
            )
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "the held test worker leaked")

    def prepare(self, *, pause=True):
        _, request = self.pipeline.preview()
        if pause:
            request["pause_on_stage_error"] = True
        return self.manager.prepare(request)

    def paused(self, response, *, attempt=1):
        job_id = response["job"]["job_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.manager.get_job(job_id)["job"]
            retry = job.get("stage_retry") or {}
            if (
                job.get("status") == "stage_retry_required"
                and retry.get("state") == "waiting"
                and retry.get("attempt") == attempt
            ):
                return job
            if not self.manager._thread.is_alive():
                self.fail(f"the worker stopped instead of waiting: {job}")
            threading.Event().wait(0.01)
        self.fail(f"the worker did not publish retry attempt {attempt}: {job}")

    @staticmethod
    def retry_request(job):
        return {
            "job_id": job["job_id"],
            "confirmation": job["confirmation_texts"]["retry_stage"],
        }

    def saved_snapshot(self, job):
        private = self.manager._read(self.manager._job_path(job["job_id"]))
        return json.dumps(private["snapshot"], sort_keys=True)

    def test_archive_readback_retry_keeps_the_closed_transfer_and_source_capture(self):
        original_verify = archive_module._verify_stream
        calls = []

        def verify(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OSError(errno.EIO, "temporary archive read failure")
            return original_verify(*args, **kwargs)

        with (
            patch.object(
                self.manager, "_capture", wraps=self.manager._capture
            ) as capture,
            patch.object(
                archive_module,
                "create_retained_archive",
                wraps=archive_module.create_retained_archive,
            ) as create,
            patch.object(
                archive_module, "iter_tree", wraps=archive_module.iter_tree
            ) as inventory,
            patch.object(archive_module, "_verify_stream", side_effect=verify),
        ):
            response = self.prepare()
            held = self.paused(response)
            self.assertEqual("archive_readback", held["stage_retry"]["operation"])
            self.assertEqual(1, capture.call_count)
            self.assertEqual(1, create.call_count)
            # This fixture has a shared data/runtime source, so the archive
            # producer enumerates that source once, before its closed readback.
            self.assertEqual(1, inventory.call_count)
            self.assertFalse(self.pipeline.native_candidates)
            self.assertTrue(self.pipeline.source.exists())
            partial = next(self.pipeline.archive.rglob("*.partial"))
            identity = partial.stat()
            digest = hashlib.sha256(partial.read_bytes()).hexdigest()
            self.assertFalse(list(self.pipeline.archive.rglob("*.tar")))

            self.manager.retry_stage(self.retry_request(held))
            prepared = self.pipeline.wait(response)
            self.assertEqual("prepared", prepared["status"], prepared)
            self.assertEqual(1, capture.call_count)
            self.assertEqual(1, create.call_count)
            self.assertEqual(1, inventory.call_count)
            self.assertEqual(1, len(self.pipeline.native_candidates))
            self.assertGreaterEqual(len(calls), 2)
            self.assertFalse(list(self.pipeline.archive.rglob("*.partial")))
            archives = list(self.pipeline.archive.rglob("*.tar"))
            self.assertEqual(1, len(archives))
            published = archives[0]
            self.assertEqual(
                (identity.st_dev, identity.st_ino),
                (published.stat().st_dev, published.stat().st_ino),
            )
            self.assertEqual(digest, hashlib.sha256(published.read_bytes()).hexdigest())

    def test_retry_verification_reuses_archive_snapshot_and_completed_native_import(
        self,
    ):
        original_verify = execution.verify_import_inventory
        retry_started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []

        def verify(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OSError(errno.EIO, "temporary archive input read failure")
            retry_started.set()
            if not release.wait(timeout=5):
                raise AssertionError("the retry observer did not release verification")
            return original_verify(*args, **kwargs)

        with (
            patch.object(
                self.manager, "_capture", wraps=self.manager._capture
            ) as capture,
            patch.object(execution, "verify_import_inventory", side_effect=verify),
        ):
            response = self.prepare()
            held = self.paused(response)
            self.assertEqual("validating", held["stage"])
            journal = self.manager._read(self.manager._job_path(held["job_id"]))
            self.assertEqual("running", journal["status"])
            self.assertEqual("waiting", journal["stage_retry"]["state"])
            self.assertEqual("verify_import", held["stage_retry"]["operation"])
            self.assertTrue(held["can_retry_stage"])
            self.assertTrue(held["actions"]["retry_stage"])
            self.assertTrue(held["can_cancel"])
            self.assertFalse(held["can_activate"])
            self.assertTrue(self.manager.has_blocking_job())
            self.assertTrue(self.pipeline.fixture.updater.updating.locked())
            self.assertFalse(self.pipeline.source.exists())
            self.assertEqual(1, len(self.pipeline.native_candidates))
            self.assertEqual(1, capture.call_count)
            snapshot = self.saved_snapshot(held)
            archive = next(self.pipeline.archive.rglob("*.tar"))
            archive_identity = archive.stat()
            archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()

            request = self.retry_request(held)
            self.manager.retry_stage(request)
            self.assertTrue(retry_started.wait(timeout=5))
            with self.assertRaises(InfiniDyskV2MigrationError):
                self.manager.retry_stage(request)
            release.set()
            prepared = self.pipeline.wait(response)

            self.assertEqual("prepared", prepared["status"], prepared)
            self.assertEqual("completed", prepared["stage_retry"]["state"])
            self.assertFalse(prepared["can_retry_stage"])
            self.assertEqual(snapshot, self.saved_snapshot(prepared))
            self.assertEqual(1, capture.call_count)
            self.assertEqual(1, len(self.pipeline.native_candidates))
            self.assertEqual(2, len(calls))
            self.assertEqual(
                (archive_identity.st_dev, archive_identity.st_ino),
                (archive.stat().st_dev, archive.stat().st_ino),
            )
            self.assertEqual(
                archive_digest, hashlib.sha256(archive.read_bytes()).hexdigest()
            )
            self.assertFalse(self.pipeline.executor._running("InfiniDysk"))
            self.assertFalse(self.pipeline.executor._running("rclone InfiniDysk"))

    def test_second_failure_requires_fresh_confirmation_and_rejects_stale_requests(
        self,
    ):
        original_verify = execution.verify_import_inventory
        calls = []

        def verify(*args, **kwargs):
            calls.append(1)
            if len(calls) <= 2:
                raise OSError(errno.ESTALE, "temporary stale network handle")
            return original_verify(*args, **kwargs)

        with patch.object(execution, "verify_import_inventory", side_effect=verify):
            response = self.prepare()
            first = self.paused(response)
            stale = self.retry_request(first)
            for invalid in (
                {**stale, "confirmation": "incorrect confirmation"},
                {**stale, "confirmation": "réessayer"},
                {**stale, "confirmation": "a" * 8192},
                {**stale, "job_id": "f" * 32},
            ):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(InfiniDyskV2MigrationError):
                        self.manager.retry_stage(invalid)
            self.manager.retry_stage(stale)
            second = self.paused(response, attempt=2)
            self.assertNotEqual(stale, self.retry_request(second))
            with self.assertRaises(InfiniDyskV2MigrationError):
                self.manager.retry_stage(stale)
            self.assertEqual(2, len(calls))
            self.manager.retry_stage(self.retry_request(second))
            prepared = self.pipeline.wait(response)
            self.assertEqual("prepared", prepared["status"], prepared)
            self.assertEqual(3, len(calls))
            self.assertEqual(1, len(self.pipeline.native_candidates))

    def test_cancel_while_waiting_restores_v1_without_retrying_the_failed_step(self):
        original = tree_fingerprint(
            self.pipeline.source, scratch_root=self.pipeline.fixture.root
        )
        with patch.object(
            execution,
            "verify_import_inventory",
            side_effect=OSError(errno.EIO, "temporary input read failure"),
        ) as verify:
            response = self.prepare()
            held = self.paused(response)
            self.manager.cancel(
                {
                    "job_id": held["job_id"],
                    "confirmation": held["cancel_confirmation_text"],
                }
            )
            restored = self.pipeline.wait(response)
            self.assertEqual("cancelled", restored["status"], restored)
            self.assertTrue(restored["result"]["source_restored"])
            self.assertEqual(1, verify.call_count)
            self.assertFalse(self.manager.has_blocking_job())
            self.assertEqual(
                original,
                tree_fingerprint(
                    self.pipeline.source, scratch_root=self.pipeline.fixture.root
                ),
            )
            self.assertEqual(1, len(self.pipeline.native_candidates))

    def test_restart_retains_recovery_but_cannot_recreate_a_live_retry_continuation(
        self,
    ):
        with patch.object(
            execution,
            "verify_import_inventory",
            side_effect=OSError(errno.ETIMEDOUT, "temporary input timeout"),
        ) as verify:
            response = self.prepare()
            held = self.paused(response)
            replacement = self.pipeline.fixture.new_manager()
            self.pipeline.fixture.managers.append(replacement)
            public = replacement.get_job(held["job_id"])["job"]
            self.assertEqual("interrupted", public["status"])
            self.assertFalse(public["can_retry_stage"])
            self.assertTrue(public["can_rollback"])
            with self.assertRaises(InfiniDyskV2MigrationError):
                replacement.retry_stage(self.retry_request(held))
            self.assertEqual(1, verify.call_count)
            self.assertEqual(1, len(self.pipeline.native_candidates))
            # The original fixture worker still exists; stop it through its
            # real cancellation path rather than leaving a test thread behind.
            self.manager.cancel(
                {
                    "job_id": held["job_id"],
                    "confirmation": held["cancel_confirmation_text"],
                }
            )
            restored = self.pipeline.wait(response)
            self.assertEqual("cancelled", restored["status"], restored)

    def test_integrity_failure_is_not_made_retryable_by_the_opt_in(self):
        with patch.object(
            execution,
            "verify_import_inventory",
            side_effect=execution.ContainedImportError("candidate inventory mismatch"),
        ):
            result = self.pipeline.wait(self.prepare())
        self.assertEqual("failed_restored", result["status"], result)
        self.assertFalse(result["can_retry_stage"])
        self.assertEqual(1, len(self.pipeline.native_candidates))
        self.assertTrue(result["result"]["source_restored"])

    def test_legacy_request_without_opt_in_retains_automatic_restore(self):
        with patch.object(
            execution,
            "verify_import_inventory",
            side_effect=OSError(errno.EIO, "temporary input read failure"),
        ):
            result = self.pipeline.wait(self.prepare(pause=False))
        self.assertEqual("failed_restored", result["status"], result)
        self.assertFalse(result["can_retry_stage"])
        self.assertEqual(1, len(self.pipeline.native_candidates))
        self.assertTrue(result["result"]["source_restored"])


class StageRetryClassificationTests(unittest.TestCase):
    def test_wrapped_storage_error_is_retryable_but_a_plain_integrity_error_is_not(
        self,
    ):
        failure = RuntimeError("storage read could not complete")
        failure.__cause__ = OSError(errno.EIO, "temporary device error")
        self.assertTrue(retryable_error(failure))
        self.assertFalse(retryable_error(RuntimeError("inventory content mismatch")))
        self.assertFalse(retryable_error(PermissionError(errno.EACCES, "denied")))

    def test_sqlite_storage_and_busy_codes_are_distinct_from_corruption(self):
        for code, expected in (
            (sqlite3.SQLITE_BUSY, True),
            (sqlite3.SQLITE_LOCKED, True),
            (sqlite3.SQLITE_IOERR | (1 << 8), True),
            (sqlite3.SQLITE_FULL, True),
            (sqlite3.SQLITE_CORRUPT, False),
            (sqlite3.SQLITE_NOTADB, False),
        ):
            with self.subTest(code=code):
                failure = sqlite3.OperationalError("database access failed")
                failure.sqlite_errorcode = code
                self.assertEqual(expected, retryable_error(failure))

    def test_only_the_typed_postgres_storage_marker_can_authorize_retry(self):
        storage = InfiniDyskV2PostgresSnapshotError("restore storage unavailable")
        storage.retry_errno = errno.ENOSPC
        self.assertTrue(retryable_error(storage))
        for failure, code in (
            (RuntimeError("untyped marker"), errno.ENOSPC),
            (InfiniDyskV2PostgresSnapshotError("unexpected code"), errno.EIO),
        ):
            with self.subTest(kind=type(failure).__name__):
                failure.retry_errno = code
                self.assertFalse(retryable_error(failure))

    def test_cancellation_and_uncertain_cleanup_override_a_transient_io_cause(self):
        cancelled = InfiniDyskV2Cancelled("cancellation requested")
        cancelled.__cause__ = OSError(errno.EIO, "temporary device error")
        self.assertFalse(retryable_error(cancelled))
        for marker in ("cleanup_complete", "stage_retry_safe"):
            for unsafe_outer in (True, False):
                with self.subTest(marker=marker, unsafe_outer=unsafe_outer):
                    outer = RuntimeError("retained helper state")
                    inner = OSError(errno.EIO, "temporary device error")
                    setattr(outer if unsafe_outer else inner, marker, False)
                    outer.__cause__ = inner
                    self.assertFalse(retryable_error(outer))

    def test_restore_failure_marks_only_the_fixed_storage_failure_code(self):
        for code in ("storage", "mismatch", "permissions", "helper"):
            with (
                self.subTest(code=code),
                patch.object(
                    restore,
                    "_restore_status",
                    return_value={
                        "stage": "restoring_database",
                        "failed": True,
                        "failure_code": code,
                    },
                ),
                self.assertLogs(restore.__name__, level="ERROR"),
            ):
                failure = restore._restore_failure(None, RuntimeError("helper stopped"))
            if code == "storage":
                self.assertEqual(errno.ENOSPC, failure.retry_errno)
                self.assertTrue(retryable_error(failure))
            else:
                self.assertFalse(hasattr(failure, "retry_errno"))
                self.assertFalse(retryable_error(failure))


if __name__ == "__main__":
    unittest.main()
