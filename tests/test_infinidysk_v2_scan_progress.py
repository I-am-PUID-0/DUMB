"""Metadata reporting stays bounded without hiding cancellation or stale estimates."""

from contextlib import ExitStack
import copy
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_infinidysk_v2_migration as fixtures
from utils import infinidysk_v2_inventory as inventory
from utils import infinidysk_v2_scan_progress as reporting
from utils import infinidysk_v2_space_control as reuse
from utils.infinidysk_v2_blob_staging import blob_summary


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class MetadataProgressTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.events = []
        self.checks = 0

        def check():
            self.checks += 1

        self.reporter = reporting.MetadataScanProgress(
            lambda stage, message, **fields: self.events.append((stage, fields)),
            check_cancel=check,
            prior_capacity={"source": {"entries": 1000, "root_count": 1}},
            monotonic=self.clock,
            wall_clock=lambda: 1000 + self.clock.value,
        )

    def test_frozen_clock_thousands_of_entries_only_persist_phase_boundaries(self):
        with self.reporter.scan("source_data", "Measuring source") as update:
            for index in range(1, 10001):
                update(index, index * 8)
        self.assertEqual(len(self.events), 2)
        self.assertGreaterEqual(self.checks, 10000)
        state = self.events[-1][1]["scan_progress"]
        self.assertEqual(state["entries_processed"], 10000)
        self.assertEqual(state["logical_bytes_processed"], 80000)
        self.assertTrue(state["complete"])
        self.assertIsNone(state["estimated_remaining_seconds"])

    def test_cancellation_is_checked_per_entry_when_publication_clock_is_frozen(self):
        class Cancelled(RuntimeError):
            pass

        self.reporter.cancel = lambda: (
            (_ for _ in ()).throw(Cancelled()) if self.checks >= 37 else None
        )
        with self.assertRaises(Cancelled):
            with self.reporter.scan("source_data", "Measuring source") as update:
                for index in range(1, 1000):
                    self.checks = index
                    update(index, index)
        self.assertEqual(self.checks, 37)
        self.assertEqual(len(self.events), 1)
        self.assertFalse(self.events[0][1]["scan_progress"]["complete"])

    def test_real_elapsed_time_bounds_saves_and_current_scan_estimate(self):
        with self.reporter.scan("source_data", "Measuring source") as update:
            for index in range(1, 301):
                self.clock.value = index / 20
                update(index, index * 8)
            state = self.events[-1][1]["scan_progress"]
            self.assertEqual(state["entries_total"], 1000)
            self.assertTrue(state["total_is_estimate"])
            self.assertAlmostEqual(state["rate_entries_per_second"], 20)
            self.assertEqual(state["estimated_remaining_seconds"], 35)
            self.assertEqual(state["estimate_scope"], "current_scan")
            self.assertLessEqual(len(self.events), 17)
            public = reporting.public_scan_progress(state, active=True, now=1015)
            self.assertEqual(public["estimated_remaining_seconds"], 35)
            self.assertIsNone(
                reporting.public_scan_progress(state, active=True, now=1046)[
                    "estimated_remaining_seconds"
                ]
            )
            self.assertIsNone(
                reporting.public_scan_progress(state, active=False, now=1015)[
                    "estimated_remaining_seconds"
                ]
            )
        self.assertIsNone(
            self.events[-1][1]["scan_progress"]["estimated_remaining_seconds"]
        )

    def test_overrun_pause_and_phase_change_do_not_carry_estimates(self):
        self.reporter.prior_capacity["source"]["roots"] = [
            {"role": "data_dir", "entries": 1000},
            {"role": "runtime_dir", "entries": 200},
        ]
        with self.reporter.scan("source_data", "Measuring source") as update:
            self.clock.value = 10
            update(100, 10)
            self.assertIsNotNone(
                self.events[-1][1]["scan_progress"]["estimated_remaining_seconds"]
            )
            self.clock.value = 11
            update(1001, 20)
            self.assertIsNone(
                self.events[-1][1]["scan_progress"]["estimated_remaining_seconds"]
            )
            self.clock.value = 60
            update(1002, 30)
            self.assertIsNone(
                self.events[-1][1]["scan_progress"]["rate_entries_per_second"]
            )
        previous = self.events[-1][1]["scan_progress"]["scan_id"]
        with self.reporter.scan("source_runtime", "Measuring runtime"):
            state = self.events[-1][1]["scan_progress"]
            self.assertNotEqual(state["scan_id"], previous)
            self.assertEqual(state["entries_processed"], 0)
            self.assertEqual(state["entries_total"], 200)
            self.assertIsNone(state["estimated_remaining_seconds"])

    def test_old_multiple_roots_have_unknown_per_root_totals_and_two_arg_callback_works(
        self,
    ):
        self.reporter.prior_capacity = {"source": {"entries": 1000, "root_count": 2}}
        with self.reporter.scan("source_data", "Measuring data"):
            self.assertIsNone(self.events[-1][1]["scan_progress"]["entries_total"])
        calls = []
        reporter = reporting.metadata_progress(
            lambda stage, message: calls.append((stage, message))
        )
        with reporter.scan("source_data", "Measuring data") as update:
            update(1, 3)
        self.assertEqual(len(calls), 2)

    def test_malformed_optional_metadata_does_not_expose_private_or_invalid_estimates(
        self,
    ):
        for value in (None, [], {"phase": []}, {"phase": {}, "scan_id": []}):
            self.assertIsNone(reporting.public_scan_progress(value, active=True))
        with self.reporter.scan("source_data", "Measuring source"):
            state = copy.deepcopy(self.events[-1][1]["scan_progress"])
        state.update(
            private="private-secret",
            rate_entries_per_second=float("inf"),
            estimated_remaining_seconds=123,
        )
        public = reporting.public_scan_progress(state, active=True, now=1000)
        self.assertNotIn("private", public)
        self.assertIsNone(public["rate_entries_per_second"])
        state["entries_processed"] = True
        self.assertIsNone(reporting.public_scan_progress(state, active=True, now=1000))


class RealScanProgressTests(unittest.TestCase):
    def test_reuse_census_and_blob_walk_have_bounded_durable_writes(self):
        fixture = fixtures.InfiniDyskV2MigrationTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        blobs = fixture.source / "blobs"
        for index in range(1000):
            (blobs / str(index)).write_bytes(b"metadata")
        artifact = fixture.root / "external"
        artifact.mkdir()
        plan = {
            "mode": "migrate",
            "source": {
                "data_dir": str(fixture.source),
                "runtime_dir": str(fixture.source),
            },
            "target": {"data_dir": str(fixture.root / "new-data")},
            "backup_storage": {"mode": "cold_archive", "artifact_root": str(artifact)},
        }
        job = {"job_id": "a" * 32, "status": "queued", "plan": {"padding": "x" * 55000}}
        reporter = reporting.MetadataScanProgress(
            lambda stage, message, **details: fixture.manager._progress(
                job, "revalidating", message, **details
            ),
            check_cancel=lambda: fixture.manager._check_cancel(job),
            monotonic=lambda: 0.0,
        )
        real_stat, real_fsync = Path.stat, os.fsync

        def source_device(path, *args, **kwargs):
            if path == artifact:
                return SimpleNamespace(st_dev=real_stat(fixture.source).st_dev + 1)
            return real_stat(path, *args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(reuse, "_mount", return_value={"mount_id": "owned"})
            )
            stack.enter_context(patch.object(reuse, "mount_records", return_value=[]))
            stack.enter_context(patch.object(Path, "stat", source_device))
            sync = stack.enter_context(patch.object(os, "fsync", wraps=real_fsync))
            writes = stack.enter_context(
                patch.object(fixture.manager, "_write", wraps=fixture.manager._write)
            )
            with reporter.scan("reuse_eligibility", "Checking reuse") as update:
                reuse.preflight_reuse(
                    plan, check_cancel=reporter.check_cancel, progress=update
                )
            with reporter.scan("source_data", "Measuring source") as update:
                result = inventory.tree_summary(
                    fixture.source, check_cancel=reporter.check_cancel, progress=update
                )
            with reporter.scan("legacy_blob_allocation", "Measuring blobs") as update:
                blob_result = blob_summary(
                    blobs, check_cancel=reporter.check_cancel, progress=update
                )
        self.assertEqual(writes.call_count, 6)
        self.assertEqual(sync.call_count, 12)
        self.assertGreater(result["entries"], 1000)
        self.assertEqual(blob_result["entries"], 1001)
        self.assertEqual(fixture.executor.calls, [])

    def test_actual_revalidation_does_not_persist_per_source_entry(self):
        fixture = fixtures.InfiniDyskV2MigrationTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        for index in range(1000):
            (fixture.source / "blobs" / str(index)).write_bytes(b"metadata")
        _, request = fixture.preview_request()
        plan = fixture.manager._request_plan(request, revalidate=False)[0]
        job = {"job_id": "b" * 32, "status": "queued", "plan": plan}
        reporter = reporting.MetadataScanProgress(
            lambda stage, message, **details: fixture.manager._progress(
                job, "revalidating", message, **details
            ),
            check_cancel=lambda: fixture.manager._check_cancel(job),
            prior_capacity=plan["capacity"],
            monotonic=lambda: 0.0,
        )
        with patch.object(
            fixture.manager, "_write", wraps=fixture.manager._write
        ) as writes:
            fixture.manager._revalidate_reviewed_plan(plan, progress=reporter)
        self.assertLessEqual(writes.call_count, 8)
        self.assertEqual(
            job["execution"]["scan_progress"]["entries_processed"],
            plan["capacity"]["source"]["entries"],
        )
        self.assertEqual(fixture.executor.calls, [])

    def test_capacity_cancellation_is_not_converted_into_failed_preflight_blocker(self):
        fixture = fixtures.InfiniDyskV2MigrationTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        _, request = fixture.preview_request()
        before = (fixture.source / "db.sqlite").read_bytes()
        seen = []

        def rows(*args, **kwargs):
            for index in range(1000):
                seen.append(index)
                if index == 37:
                    identifier = fixture.manager._active_id
                    fixture.manager.cancel(
                        {
                            "job_id": identifier,
                            "confirmation": f"CANCEL INFINIDYSK V2 {identifier}",
                        }
                    )
                yield {
                    "path": str(index),
                    "mode": stat.S_IFREG | 0o600,
                    "size": 8,
                    "mtime_ns": 0,
                }

        with (
            patch.object(inventory, "iter_tree", side_effect=rows),
            patch.object(reporting.time, "monotonic", return_value=0.0),
        ):
            job = fixture.wait(fixture.manager.rehearse(request))
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["stage"], "cancelled")
        self.assertIsNone(job["error"])
        self.assertTrue(job["result"]["source_untouched"])
        self.assertFalse(job["result"]["source_restored"])
        self.assertEqual(len(seen), 38)
        self.assertEqual(fixture.executor.calls, [])
        self.assertEqual((fixture.source / "db.sqlite").read_bytes(), before)
        self.assertFalse(job["can_rollback"])

    def test_preview_cancellation_remains_per_entry_with_frozen_clock(self):
        fixture = fixtures.InfiniDyskV2MigrationTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        seen = []

        def rows(*args, **kwargs):
            for index in range(1000):
                seen.append(index)
                if index == 37:
                    fixture.manager._preview_tasks.cancelled.set()
                yield {
                    "path": str(index),
                    "mode": stat.S_IFREG | 0o600,
                    "size": 8,
                    "mtime_ns": 0,
                }

        with (
            patch.object(inventory, "iter_tree", side_effect=rows),
            patch.object(reporting.time, "monotonic", return_value=0.0),
        ):
            response = fixture.manager.preview_start(
                {"mode": "migrate", "request_id": "a" * 32}
            )
            fixture.manager._preview_tasks.thread.join(timeout=3)
        self.assertFalse(fixture.manager._preview_tasks.thread.is_alive())
        self.assertEqual(
            fixture.manager.preview_task({"task_id": response["task"]["task_id"]})[
                "task"
            ]["status"],
            "cancelled",
        )
        self.assertEqual(len(seen), 38)
        self.assertEqual(fixture.executor.calls, [])
