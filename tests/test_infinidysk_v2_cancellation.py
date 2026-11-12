"""Cancellation journal and worker/admission handoff regressions."""

import threading
import unittest
from unittest.mock import patch

from tests import test_infinidysk_v2_migration as migration_fixtures


class _TerminalHandoffLock:
    """Pause the finishing worker just after its outermost lock release."""

    def __init__(self, wrapped, terminal_written, yielded, resume, watched_job):
        self.wrapped = wrapped
        self.terminal_written = terminal_written
        self.yielded = yielded
        self.resume = resume
        self.watched_job = watched_job
        self.local = threading.local()

    def acquire(self, *args, **kwargs):
        acquired = self.wrapped.acquire(*args, **kwargs)
        if acquired:
            self.local.depth = getattr(self.local, "depth", 0) + 1
        return acquired

    def release(self):
        self.local.depth -= 1
        self.wrapped.release()
        expected = "infinidysk-v2-" + self.watched_job[0][:8]
        if (
            self.local.depth == 0
            and threading.current_thread().name == expected
            and self.terminal_written.is_set()
            and not self.yielded.is_set()
        ):
            self.yielded.set()
            if not self.resume.wait(timeout=5):
                raise AssertionError("The terminal handoff observer did not finish.")

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *unused):
        self.release()


class InfiniDyskV2CancellationJournalTests(unittest.TestCase):
    def setUp(self):
        # Compose the established fixture without importing/inheriting its
        # TestCase class into this module's discovery namespace.
        self.fixture = migration_fixtures.InfiniDyskV2MigrationTests(
            "test_preview_is_private_metadata_and_never_runs_converter"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_accepted_cancel_survives_stale_worker_journal_and_new_manager(self):
        fixture = self.fixture
        fixture.executor.gate = threading.Event()
        _, request = fixture.preview_request()
        response = fixture.manager.rehearse(request)
        self.assertTrue(fixture.executor.started.wait(timeout=1))
        job_id = response["job"]["job_id"]
        path = fixture.manager._job_path(job_id)
        stale = fixture.manager._read(path)
        public = fixture.manager.get_job(job_id)["job"]
        fixture.manager.cancel(
            {
                "job_id": job_id,
                "confirmation": public["cancel_confirmation_text"],
            }
        )

        # A worker retained this dictionary before the request was accepted.
        fixture.manager._write(path, stale)
        self.assertTrue(fixture.manager._read(path)["cancel_requested"])
        fixture.manager._recovery_progress(stale, "restoring_source")
        self.assertTrue(fixture.manager._read(path)["cancel_requested"])

        replacement = fixture.new_manager()
        fixture.managers.append(replacement)
        restored = replacement.get_job(job_id)["job"]
        self.assertTrue(restored["cancel_requested"])
        self.assertEqual("interrupted", restored["status"])
        self.assertTrue(restored["can_rollback"])
        self.assertFalse(restored["can_cancel"])

        fixture.executor.gate.set()
        finished = fixture.wait(response)
        self.assertTrue(finished["cancel_requested"])
        self.assertTrue(finished["result"]["source_restored"])

    def test_terminal_publication_releases_admission_before_next_worker_starts(self):
        fixture = self.fixture
        manager = fixture.manager
        terminal_written = threading.Event()
        yielded = threading.Event()
        resume = threading.Event()
        watched_job = [""]
        self.addCleanup(resume.set)
        original_write = manager._write

        def observe_write(path, payload):
            original_write(path, payload)
            if payload.get("status") == "queued" and not watched_job[0]:
                watched_job[0] = payload["job_id"]
            if (
                payload.get("job_id") == watched_job[0]
                and payload.get("status") == "rehearsed"
            ):
                terminal_written.set()

        manager._lock = _TerminalHandoffLock(
            manager._lock, terminal_written, yielded, resume, watched_job
        )
        with patch.object(manager, "_write", side_effect=observe_write):
            _, request = fixture.preview_request()
            first = manager.rehearse(request)
            first_thread = manager._thread
            self.assertTrue(yielded.wait(timeout=2))
            public = manager.get_job(first["job"]["job_id"])["job"]
            self.assertEqual("rehearsed", public["status"])
            self.assertFalse(
                fixture.updater.updating.locked(),
                "A terminal job must not expose completion while holding admission.",
            )
            self.assertIsNone(manager._active_id)

            fixture.executor.gate = threading.Event()
            fixture.executor.started.clear()
            second = manager.rehearse(request)
            self.assertTrue(fixture.executor.started.wait(timeout=1))
            second_id = second["job"]["job_id"]
            self.assertEqual(second_id, manager._active_id)
            self.assertTrue(fixture.updater.updating.locked())

            # The previous worker may still execute its finally block after
            # this point. It must not clear the replacement worker's identity.
            resume.set()
            first_thread.join(timeout=2)
            self.assertFalse(first_thread.is_alive())
            self.assertEqual(second_id, manager._active_id)
            self.assertTrue(fixture.updater.updating.locked())
            fixture.executor.gate.set()
            finished = fixture.wait(second)
            self.assertEqual("rehearsed", finished["status"], finished)
            self.assertIsNone(manager._active_id)
            self.assertFalse(fixture.updater.updating.locked())


if __name__ == "__main__":
    unittest.main()
