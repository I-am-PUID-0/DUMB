"""The real executor forwards post-conversion progress and cancellation."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock, patch

from utils.infinidysk_v2_execution import verify_import_inventory


class VerificationProgressWiringTests(unittest.TestCase):
    def test_wrapper_preserves_explicit_hooks_with_the_real_verifier_signature(self):
        progress, cancel, census = Mock(), Mock(), Mock()
        with patch(
            "utils.infinidysk_v2_import_report.verify_import_inventory", autospec=True
        ) as verify:
            verify_import_inventory(
                "/source.sqlite",
                "/candidate.sqlite",
                "native report",
                source_blob_root="/staged/blobs",
                source_blob_census=census,
                progress=progress,
                check_cancel=cancel,
            )
        verify.assert_called_once_with(
            "/source.sqlite",
            "/candidate.sqlite",
            "native report",
            source_blob_root="/staged/blobs",
            source_blob_census=census,
            progress=progress,
            check_cancel=cancel,
        )

    def test_actual_executor_connects_job_progress_and_cancel_to_inventory(self):
        from tests.test_infinidysk_v2_execution import ExecutionTests

        fixture = ExecutionTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        cancel = Mock()
        fixture.workspace["check_cancel"] = cancel
        with patch(
            "utils.infinidysk_v2_execution.verify_import_inventory",
            autospec=verify_import_inventory,
            return_value={"files": 0, "history": 0, "skipped_files": 0},
        ) as verify:
            fixture.executor.prepare(fixture.plan, fixture.workspace, fixture.progress)
        self.assertIs(verify.call_args.kwargs["progress"], fixture.progress)
        self.assertIs(verify.call_args.kwargs["check_cancel"], cancel)


class LazyExternalReadbackTests(unittest.TestCase):
    def setUp(self):
        from tests.test_infinidysk_v2_execution import ExecutionTests

        self.fixture = ExecutionTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.external = self.fixture.root / "external-blobs"
        self.external.mkdir()
        self.fixture.plan["options"] = {"offload_legacy_blobs": True}
        self.fixture.workspace["external_blob_source"] = Mock(
            return_value=self.external
        )
        self.fixture.workspace["record_import_report"] = Mock()
        with sqlite3.connect(self.fixture.root / "source.sqlite") as db:
            db.executescript(
                "CREATE TABLE DavItems(Id TEXT,Name TEXT,Path TEXT,Type INTEGER);"
                "CREATE TABLE HistoryItems(Id TEXT,DownloadStatus INTEGER);"
            )
        original_import = self.fixture.candidate.wait_import.side_effect

        def imported(*args, **kwargs):
            result = original_import(*args, **kwargs)
            with sqlite3.connect(
                Path(self.fixture.workspace["candidate_data"]) / "infinidysk.sqlite"
            ) as db:
                db.executescript(
                    "CREATE TABLE items(id BLOB,name TEXT,kind INTEGER);CREATE TABLE history(id BLOB);"
                )
            return result

        self.fixture.candidate.wait_import.side_effect = imported
        self.events = []

    def prepare(self):
        with patch(
            "utils.infinidysk_v2_execution.verify_import_inventory",
            autospec=verify_import_inventory,
            side_effect=verify_import_inventory,
        ):
            return self.fixture.executor.prepare(
                self.fixture.plan, self.fixture.workspace, self.fixture.progress
            )

    @contextmanager
    def census(self):
        self.events.append("entered")
        yield self.external, Mock(
            side_effect=AssertionError("no payloads are referenced")
        )
        self.events.append("closed")

    def test_missing_history_never_starts_the_external_payload_readback(self):
        with sqlite3.connect(self.fixture.root / "source.sqlite") as db:
            db.execute(
                "INSERT INTO HistoryItems VALUES('11111111-1111-4111-8111-111111111111',2)"
            )
        self.fixture.candidate.wait_import.return_value = (
            "Imported 0 directories, 0 files, 0 NZBs, 1 history rows, 0 queued jobs"
        )
        self.fixture.workspace["source_blob_census"] = self.census
        with self.assertRaisesRegex(ValueError, "history identities"):
            self.prepare()
        self.assertEqual(self.events, [])
        self.fixture.workspace["accept_import"].assert_not_called()

    def test_success_without_payload_references_still_requires_closed_readback(self):
        self.fixture.workspace["source_blob_census"] = self.census
        self.fixture.workspace["accept_import"].side_effect = (
            lambda receipt: self.assertEqual(self.events, ["entered", "closed"])
        )
        self.assertTrue(self.prepare()["candidate_prepared"])
        self.assertEqual(self.events, ["entered", "closed"])

    def test_changed_external_scope_cannot_be_accepted(self):
        @contextmanager
        def changed():
            yield self.fixture.root, Mock()

        self.fixture.workspace["source_blob_census"] = changed
        with self.assertRaisesRegex(ValueError, "External migration input changed"):
            self.prepare()
        self.fixture.workspace["accept_import"].assert_not_called()

    def test_invalid_external_lookup_cannot_be_accepted(self):
        @contextmanager
        def invalid():
            yield self.external, None

        self.fixture.workspace["source_blob_census"] = invalid
        with self.assertRaisesRegex(ValueError, "External migration input changed"):
            self.prepare()
        self.fixture.workspace["accept_import"].assert_not_called()

    def test_failed_external_context_exit_cannot_record_accepted_diagnostics(self):
        @contextmanager
        def failed_exit():
            yield self.external, Mock()
            raise ValueError("fixture exit failure")

        self.fixture.workspace["source_blob_census"] = failed_exit
        with self.assertRaisesRegex(ValueError, "fixture exit failure"):
            self.prepare()
        self.fixture.workspace["accept_import"].assert_not_called()
        recorded = self.fixture.workspace["record_import_report"].call_args_list
        self.assertFalse(
            any(
                len(call.args) > 1 and (call.args[1] or {}).get("status") == "accepted"
                for call in recorded
            )
        )


if __name__ == "__main__":
    unittest.main()
