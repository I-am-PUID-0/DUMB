"""Measure the acceptance pass without changing its preservation decisions."""

import hashlib
import itertools
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import uuid

from tests.test_infinidysk_v2_import_report import native_report
from utils import infinidysk_v2_import_report as inventory


class ImportInventoryProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.target = (
            self.root / "source.sqlite",
            self.root / "target.sqlite",
        )
        self.first, self.second, self.history = (uuid.UUID(int=n) for n in (10, 11, 12))
        with sqlite3.connect(self.source) as database:
            database.execute(
                "CREATE TABLE DavItems(Id TEXT,Name TEXT,Path TEXT,Type INTEGER,FileBlobId TEXT)"
            )
            database.executemany(
                "INSERT INTO DavItems VALUES(?,?,?,2,NULL)",
                [
                    (str(self.first), "first.mkv", "/content/first.mkv"),
                    (str(self.second), "second.mkv", "/content/second.mkv"),
                ],
            )
            database.execute(
                "CREATE TABLE HistoryItems(Id TEXT,DownloadStatus INTEGER)"
            )
            database.execute(
                "INSERT INTO HistoryItems VALUES(?,2)", (str(self.history),)
            )
        with sqlite3.connect(self.target) as database:
            database.execute("CREATE TABLE items(id BLOB,name TEXT,kind INTEGER)")
            database.execute(
                "INSERT INTO items VALUES(?,?,2)", (self.first.bytes, "first.mkv")
            )
            database.execute("CREATE TABLE history(id BLOB)")
            database.execute("INSERT INTO history VALUES(?)", (self.history.bytes,))
        self.report = native_report(
            1, skipped=[("/content/second.mkv", "no metadata blob")]
        )

    def verify(self, **kwargs):
        return inventory.verify_import_inventory(
            self.source, self.target, self.report, **kwargs
        )

    @staticmethod
    def advancing_clock():
        return mock.patch(
            "utils.infinidysk_v2_io_progress.time.monotonic",
            side_effect=itertools.count(step=2).__next__,
        )

    def add_blob(self, contents):
        identifier = uuid.UUID(int=100)
        key = identifier.hex
        leaf = self.root / "blobs" / key[:2] / key[2:4] / str(identifier)
        leaf.parent.mkdir(parents=True)
        leaf.write_bytes(contents)
        with sqlite3.connect(self.source) as database:
            database.execute(
                "UPDATE DavItems SET FileBlobId=? WHERE Id=?",
                (str(identifier), str(self.first)),
            )
        return leaf, identifier

    def test_measured_pass_has_exact_counts_and_identical_acceptance_proof(self):
        expected = self.verify()
        # Frozen from the preceding verifier for this exact fixture: telemetry
        # and earlier history checks must not change persisted consent digests.
        proof = expected["omission_proof"]
        self.assertEqual(
            proof["source_media"]["sha256"],
            "fc69eb7c9cee4d8db9164761698c75c950e3e7d68f55bd66611102cd927548c0",
        )
        self.assertEqual(
            proof["source_history"]["sha256"],
            "deda010bef2bd2cd91edd17c4cbfb4e07b147792376b92e2d0fcc7631347c970",
        )
        self.assertEqual(
            proof["review_digest"],
            "fcde9c444be9e036e21ff0050050114a7cd62c5119da862a12df252dbe5ab711",
        )
        events = []
        actual = self.verify(
            progress=lambda stage, message, **details: events.append(
                details["io_progress"]
            )
        )
        self.assertEqual(actual, expected)
        complete = {event["phase"]: event for event in events if event["complete"]}
        for phase, total in (
            ("import_source_media", 2),
            ("import_source_history", 1),
            ("import_target_items", 1),
            ("import_target_history", 1),
            ("import_history_check", 2),
            ("import_media_check", 2),
            ("import_media_census", 2),
            ("import_history_census", 1),
        ):
            self.assertEqual(complete[phase]["processed"], total, phase)
            self.assertEqual(complete[phase]["total"], total, phase)
        integrity = complete["import_target_integrity"]
        self.assertIsNone(integrity["total"])
        self.assertEqual(integrity["processed"], 0)
        self.assertIsNone(integrity["estimated_remaining_seconds"])

    def test_missing_history_is_reported_before_any_payload_reads(self):
        with sqlite3.connect(self.target) as database:
            database.execute("DELETE FROM history")
        events = []
        with mock.patch.object(inventory, "_blob_census") as census:
            with self.assertRaisesRegex(
                inventory.ImportReportError, "0 completed and 1 failed"
            ):
                self.verify(
                    progress=lambda stage, message, **detail: events.append(
                        detail["io_progress"]
                    )
                )
        census.assert_not_called()
        self.assertEqual(events[-1]["phase"], "import_history_check")
        self.assertFalse(events[-1]["complete"])
        self.assertNotIn("import_media_census", {event["phase"] for event in events})

    def test_invalid_media_is_reported_before_any_payload_reads(self):
        with sqlite3.connect(self.target) as database:
            database.execute("UPDATE items SET name='changed.mkv'")
        with mock.patch.object(inventory, "_blob_census") as census:
            with self.assertRaisesRegex(inventory.ImportReportError, "media names"):
                self.verify()
        census.assert_not_called()

    def test_row_reads_publish_intermediate_batches_and_final_exact_total(self):
        with sqlite3.connect(self.source) as database:
            database.executemany(
                "INSERT INTO HistoryItems VALUES(?,2)",
                [(str(uuid.UUID(int=1000 + index)),) for index in range(4096)],
            )
        with sqlite3.connect(self.target) as database:
            database.executemany(
                "INSERT INTO history VALUES(?)",
                [(uuid.UUID(int=1000 + index).bytes,) for index in range(4096)],
            )
        events = []
        with self.advancing_clock():
            self.verify(
                progress=lambda stage, message, **details: events.append(
                    details["io_progress"]
                )
            )
        history = [
            event for event in events if event["phase"] == "import_source_history"
        ]
        self.assertTrue(
            any(
                0 < event["processed"] < 4097 and not event["complete"]
                for event in history
            )
        )
        self.assertEqual(history[-1]["processed"], 4097)
        self.assertEqual(history[-1]["total"], 4097)

    def test_many_small_payloads_do_not_create_a_phase_per_file(self):
        for index in range(100):
            identifier = uuid.UUID(int=1000 + index)
            key = identifier.hex
            path = self.root / "blobs" / key[:2] / key[2:4] / str(identifier)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"example")
            with sqlite3.connect(self.source) as database:
                database.execute(
                    "INSERT INTO DavItems VALUES(?,?,?,1,?)",
                    (
                        str(uuid.UUID(int=2000 + index)),
                        "directory",
                        None,
                        str(identifier),
                    ),
                )
        events = []
        with mock.patch(
            "utils.infinidysk_v2_io_progress.time.monotonic", return_value=0
        ):
            self.verify(
                progress=lambda stage, message, **details: events.append(
                    details["io_progress"]
                )
            )
        self.assertNotIn("import_source_blobs", {event["phase"] for event in events})
        self.assertLess(len(events), 40)

    def test_large_payload_counts_real_read_bytes_and_resumes_record_phase(self):
        contents = b"data" * (300 * 1024)
        self.add_blob(contents)
        baseline = self.verify()
        events = []
        with mock.patch.object(inventory, "_LARGE_BLOB", 1024 * 1024):
            with self.advancing_clock():
                result = self.verify(
                    progress=lambda stage, message, **details: events.append(
                        details["io_progress"]
                    )
                )
        self.assertEqual(result, baseline)
        blobs = [event for event in events if event["phase"] == "import_source_blobs"]
        self.assertEqual(blobs[-1]["processed"], len(contents))
        self.assertEqual(blobs[-1]["total"], len(contents))
        self.assertTrue(blobs[-1]["complete"])
        self.assertTrue(any(0 < event["processed"] < len(contents) for event in blobs))
        last_blob = max(
            index
            for index, event in enumerate(events)
            if event["phase"] == "import_source_blobs"
        )
        self.assertTrue(
            any(
                event["phase"] == "import_media_census"
                for event in events[last_blob + 1 :]
            )
        )

    def test_verified_payload_lookup_keeps_proof_and_does_not_read_again(self):
        contents = b"a large original payload"
        _leaf, identifier = self.add_blob(contents)
        baseline = self.verify()
        lookup = mock.Mock(
            return_value={
                "state": "present",
                "bytes": len(contents),
                "sha256": hashlib.sha256(contents).hexdigest(),
            }
        )
        events = []
        with mock.patch.object(
            inventory.os, "read", side_effect=AssertionError("extra read")
        ):
            result = self.verify(
                source_blob_census=lookup,
                progress=lambda stage, message, **details: events.append(
                    details["io_progress"]
                ),
            )
        self.assertEqual(result, baseline)
        lookup.assert_called_once_with(self.root / "blobs", identifier)
        self.assertNotIn("import_source_blobs", {event["phase"] for event in events})

    def test_slow_small_payload_keeps_current_record_elapsed_without_fake_work(self):
        self.add_blob(b"x" * (3 * 1024 * 1024))
        events = []
        clock = 0
        original_read = inventory.os.read

        def slow_read(descriptor, amount):
            nonlocal clock
            data = original_read(descriptor, amount)
            if data:
                clock += 11
            return data

        with mock.patch(
            "utils.infinidysk_v2_io_progress.time.monotonic", lambda: clock
        ):
            with mock.patch.object(inventory.os, "read", slow_read):
                self.verify(
                    progress=lambda stage, message, **details: events.append(
                        details["io_progress"]
                    )
                )
        waiting = [
            event
            for event in events
            if event["phase"] == "import_media_census"
            and event["processed"] == 0
            and event["elapsed_seconds"] >= 10
            and not event["complete"]
        ]
        self.assertGreaterEqual(len(waiting), 3)
        self.assertTrue(all(event["rate_per_second"] is None for event in waiting))
        self.assertTrue(
            all(event["estimated_remaining_seconds"] is None for event in waiting)
        )
        self.assertNotIn("import_source_blobs", {event["phase"] for event in events})

    def test_trusted_payload_lookup_failures_keep_the_original_exception(self):
        self.add_blob(b"original payload")
        for failure in (
            ValueError("trusted staged-readback cancellation"),
            OSError("trusted staged-readback storage failure"),
        ):
            with self.subTest(error_type=type(failure).__name__):
                with self.assertRaises(type(failure)) as caught:
                    self.verify(source_blob_census=mock.Mock(side_effect=failure))
                self.assertIs(caught.exception, failure)
                self.assertNotIsInstance(caught.exception, inventory.ImportReportError)

    def test_malformed_trusted_payload_lookup_result_remains_an_inventory_block(self):
        self.add_blob(b"original payload")
        with self.assertRaisesRegex(
            inventory.ImportReportError, "verified source blob census is invalid"
        ):
            self.verify(source_blob_census=lambda *args: {"state": "unverified"})

    def test_trusted_callback_failures_are_the_original_exception(self):
        for callback_name in ("progress", "check_cancel"):
            with self.subTest(callback_name=callback_name):
                failure = ValueError("trusted cancellation or journal failure")

                def fail(*args, **kwargs):
                    raise failure

                with self.assertRaises(ValueError) as caught:
                    self.verify(**{callback_name: fail})
                self.assertIs(caught.exception, failure)
                self.assertNotIsInstance(caught.exception, inventory.ImportReportError)

    def test_callback_failure_inside_sqlite_progress_handler_is_preserved(self):
        original = inventory._source_records
        for callback_name in ("progress", "check_cancel"):
            with self.subTest(callback_name=callback_name):
                failure = ValueError("trusted SQL cancellation")
                active = False

                def callback(*args, **kwargs):
                    if active:
                        raise failure

                def slow_read(connection, table, fields, progress):
                    nonlocal active
                    progress.phase("import_source_media", "Counting original rows.")
                    active = True
                    try:
                        connection.execute(
                            "WITH RECURSIVE counter(n) AS (VALUES(0) UNION ALL "
                            "SELECT n+1 FROM counter WHERE n<100000) SELECT sum(n) FROM counter"
                        ).fetchone()
                    finally:
                        active = False
                    return original(connection, table, fields, progress)

                with self.advancing_clock():
                    with mock.patch.object(inventory, "_source_records", slow_read):
                        with self.assertRaises(ValueError) as caught:
                            self.verify(**{callback_name: callback})
                self.assertIs(caught.exception, failure)

    def test_legacy_two_argument_progress_callback_remains_supported(self):
        events = []
        self.verify(progress=lambda stage, message: events.append((stage, message)))
        self.assertTrue(events)
        self.assertTrue(all(stage == "validating" for stage, _message in events))


if __name__ == "__main__":
    unittest.main()
