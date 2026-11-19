"""Single-pass proof retains receipt, pathname and cancellation authority."""

from pathlib import Path
from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
import uuid
import unittest
from unittest.mock import patch

from tests import test_backup_storage as storage_tests
from tests import test_infinidysk_v2_archive as archive_tests
from tests import test_infinidysk_v2_input_integrity as integrity_tests
from tests import test_infinidysk_v2_import_report as report_tests
from utils import backup_storage as storage_module
from utils.backup_storage import BackupStorageError
from utils.infinidysk_v2_archive import verify_retained_archive
from utils.infinidysk_v2_execution import (
    ContainedImportError,
    verify_import_inventory as verify_execution_import_inventory,
)
from utils.infinidysk_v2_io_progress import IOProgress
from utils.infinidysk_v2_import_report import verify_import_inventory


class ArtifactStreamTests(unittest.TestCase):
    def setUp(self):
        self.fixture = storage_tests.BackupStorageTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.register()
        self.storage = self.fixture.storage
        self.payload = b"complete byte proof" * 100000
        self.receipt = self.storage.write_file(
            "nas", "stream-test", lambda handle: handle.write(self.payload)
        )
        self.artifact = (
            self.fixture.destination
            / ".dumb-backups"
            / "nas"
            / "stream-test"
            / (self.receipt["artifact_id"] + ".tar")
        )

    def test_partial_verifier_drains_once_and_returns_only_after_complete_proof(self):
        returned = []
        original = storage_module._Reader.read

        def read(reader, size=-1):
            data = original(reader, size)
            returned.append(len(data))
            return data

        with patch.object(storage_module._Reader, "read", read):
            result = self.storage.verify_artifact(
                self.receipt["reference"], lambda reader: reader.read(23)
            )
        self.assertEqual(self.payload[:23], result)
        self.assertEqual(len(self.payload), sum(returned))
        self.assertEqual(0, returned[-1])

    def test_unread_tail_tampering_never_returns_verifier_result(self):
        data = bytearray(self.payload)
        data[-1] ^= 1
        self.artifact.write_bytes(data)
        with self.assertRaisesRegex(BackupStorageError, "did not verify"):
            self.storage.verify_artifact(
                self.receipt["reference"], lambda reader: reader.read(23)
            )

    def test_same_bytes_at_replaced_path_do_not_inherit_the_open_file_proof(self):
        def verifier(reader):
            result = reader.read()
            self.artifact.rename(self.artifact.with_suffix(".old"))
            self.artifact.write_bytes(self.payload)
            self.artifact.chmod(0o600)
            return result

        with self.assertRaisesRegex(BackupStorageError, "changed"):
            self.storage.verify_artifact(self.receipt["reference"], verifier)

    def test_cancellation_after_callback_blocks_final_acceptance(self):
        cancelled = False

        def owner():
            if cancelled:
                raise RuntimeError("cancelled")

        def verifier(reader):
            nonlocal cancelled
            reader.read(23)
            cancelled = True
            return "never accepted"

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.storage.verify_artifact(
                self.receipt["reference"], verifier, owner_check=owner
            )

    def test_destination_replacement_during_callback_blocks_acceptance(self):
        def verifier(reader):
            result = reader.read()
            self.fixture.destination.rename(self.fixture.root / "old-share")
            self.fixture.destination.mkdir()
            return result

        with self.assertRaisesRegex(BackupStorageError, "changed"):
            self.storage.verify_artifact(self.receipt["reference"], verifier)


class ArchiveStreamTests(unittest.TestCase):
    def test_semantic_and_receipt_validation_read_one_archive_not_two(self):
        fixture = archive_tests.ColdArchiveTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        snapshot = fixture._archive()
        returned = []
        original = storage_module._Reader.read

        def read(reader, size=-1):
            data = original(reader, size)
            returned.append(len(data))
            return data

        with (
            patch.object(storage_module._Reader, "read", read),
            patch.object(
                fixture.storage,
                "open_artifact",
                side_effect=AssertionError("extra pass"),
            ),
        ):
            proof = verify_retained_archive(snapshot, fixture.plan, fixture.storage)
        self.assertTrue(proof["verified"])
        self.assertEqual(snapshot["cold_archive"]["size_bytes"], sum(returned))


class MountParseTests(unittest.TestCase):
    def test_identical_bytes_reuse_parse_but_changes_and_caller_mutations_do_not(self):
        raw = b"1 0 0:1 / / rw - overlay overlay rw\n"
        changed = b"2 0 0:2 / / rw - overlay overlay rw\n"
        storage_module._parsed_mount_records.cache_clear()
        self.addCleanup(storage_module._parsed_mount_records.cache_clear)
        with (
            patch.object(
                Path, "read_bytes", side_effect=[raw, raw, changed, b"invalid"]
            ),
            patch.object(
                storage_module, "_digest", wraps=storage_module._digest
            ) as digests,
        ):
            first = storage_module.mount_records()
            self.assertEqual(1, digests.call_count)
            first[0]["mount_id"] = "poisoned"
            self.assertEqual("1", storage_module.mount_records()[0]["mount_id"])
            self.assertEqual(1, digests.call_count)
            self.assertEqual("2", storage_module.mount_records()[0]["mount_id"])
            self.assertEqual(2, digests.call_count)
            with self.assertRaises(BackupStorageError):
                storage_module.mount_records()


class TelemetryCheckTests(unittest.TestCase):
    def test_ten_thousand_updates_keep_fast_checks_without_repeating_full_validation(
        self,
    ):
        checks, validations, events = [], [], []
        measured = IOProgress(
            lambda *args, **kwargs: events.append(kwargs["io_progress"]),
            "blob_copy",
            total=10000,
            monotonic=lambda: 0,
            check=lambda: checks.append(1),
            publish_check=lambda: validations.append(1),
        )
        for number in range(1, 10001):
            measured.update(number)
        measured.finish()
        self.assertEqual(10002, len(checks))
        self.assertEqual(2, len(validations))
        self.assertEqual(2, len(events))
        self.assertTrue(events[-1]["complete"])

    def test_throttled_update_still_observes_cancellation(self):
        cancelled = False

        def check():
            if cancelled:
                raise RuntimeError("cancelled")

        measured = IOProgress(lambda *args, **kwargs: None, "blob_copy", check=check)
        cancelled = True
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            measured.add(1)


class VerifiedCensusTests(unittest.TestCase):
    identifier = uuid.UUID("12345678-0000-0000-0000-000000000010")
    payload = b"original source payload"

    def additional(self, data):
        key = self.identifier.hex
        path = data / "blobs" / key[:2] / key[2:4] / str(self.identifier)
        path.parent.mkdir(parents=True)
        path.write_bytes(self.payload)

    def test_fresh_full_verification_reuses_original_hash_without_reading_content_again(
        self,
    ):
        with integrity_tests.fixture(additional=self.additional) as f:
            f.prepare()
            with f.staging.verified_census(f.job) as (root, lookup):
                with patch.object(
                    os, "read", side_effect=AssertionError("extra content read")
                ):
                    result = lookup(root, self.identifier)
                self.assertEqual(
                    {
                        "state": "present",
                        "bytes": len(self.payload),
                        "sha256": hashlib.sha256(self.payload).hexdigest(),
                    },
                    result,
                )
                with self.assertRaisesRegex(ValueError, "out of scope"):
                    lookup(root.parent, self.identifier)
            with self.assertRaisesRegex(ValueError, "out of scope"):
                lookup(root, self.identifier)
            private = Path(f.manager._workspace(f.job)["root"])
            self.assertFalse(list(private.glob(".input-verified-*")))

    def test_real_staged_readback_keeps_one_expensive_check_per_entry_and_chunk(self):
        with integrity_tests.fixture(additional=self.additional) as f:
            f.prepare()
            record = f.job["snapshot"]["external_blob_staging"]
            with sqlite3.connect(
                f"file:{record['manifest']}?mode=ro&immutable=1", uri=True
            ) as database:
                payloads = [
                    json.loads(row[0])
                    for row in database.execute("SELECT payload FROM entries")
                ]
            entries = len(payloads)
            chunks = sum(
                (row.get("size", 0) + 1024**2 - 1) // 1024**2 for row in payloads
            )
            storage, _ = f.staging._storage(f.job)
            original = storage._lease
            checks = []

            @contextmanager
            def lease(*args, **kwargs):
                with original(*args, **kwargs) as (profile, descriptor, check, allowed):

                    def counted():
                        checks.append(1)
                        check()

                    yield profile, descriptor, counted, allowed

            def telemetry(*args, **kwargs):
                return IOProgress(*args, **dict(kwargs, monotonic=lambda: 0))

            with (
                patch.object(storage, "_lease", lease),
                patch("utils.infinidysk_v2_blob_staging.IOProgress", telemetry),
            ):
                f.staging.verify(f.job)
            # Includes start/end proofs, not redundant telemetry checks for every
            # chunk. The complete content read and every explicit check remain.
            self.assertGreaterEqual(len(checks), entries + chunks)
            self.assertLessEqual(len(checks), entries + chunks + 6)

    def test_changed_content_before_fresh_pass_never_becomes_the_expected_hash(self):
        with integrity_tests.fixture(additional=self.additional) as f:
            root = f.prepare()
            key = self.identifier.hex
            path = root / key[:2] / key[2:4] / str(self.identifier)
            before = path.stat()
            path.write_bytes(b"x" * len(self.payload))
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.assertRaisesRegex(ValueError, "sha256"):
                with f.staging.verified_census(f.job):
                    self.fail("unverified content admitted")

    def test_change_after_full_verification_invalidates_exact_fresh_lookup(self):
        with integrity_tests.fixture(additional=self.additional) as f:
            f.prepare()
            with f.staging.verified_census(f.job) as (root, lookup):
                key = self.identifier.hex
                path = root / key[:2] / key[2:4] / str(self.identifier)
                path.write_bytes(b"x" * len(self.payload))
                with self.assertRaisesRegex(ValueError, "changed"):
                    lookup(root, self.identifier)

    def test_inventory_proof_is_identical_with_trusted_lookup_and_no_second_blob_read(
        self,
    ):
        fixture = report_tests.ImportReportTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with sqlite3.connect(fixture.source) as database:
            database.execute("ALTER TABLE DavItems ADD COLUMN FileBlobId TEXT")
            database.execute(
                "UPDATE DavItems SET FileBlobId=?", (str(self.identifier),)
            )
        report = report_tests.native_report(
            1, skipped=[("/content/second.mkv", "no metadata blob")]
        )
        with integrity_tests.fixture(additional=self.additional) as staged:
            root = staged.prepare()
            baseline = verify_import_inventory(
                fixture.source,
                fixture.target,
                report,
                source_blob_root=root,
            )
            with staged.staging.verified_census(staged.job) as (root, lookup):
                with patch.object(
                    os, "read", side_effect=AssertionError("second content read")
                ):
                    actual = verify_execution_import_inventory(
                        fixture.source,
                        fixture.target,
                        report,
                        source_blob_root=root,
                        source_blob_census=lookup,
                    )
        self.assertEqual(baseline, actual)
        self.assertEqual("review_required", actual["import_report"]["status"])
        self.assertRegex(actual["import_report"]["review_digest"], r"^[a-f0-9]{64}$")

    def test_execution_wrapper_retains_blocked_report_for_invalid_census(self):
        fixture = report_tests.ImportReportTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with sqlite3.connect(fixture.source) as database:
            database.execute("ALTER TABLE DavItems ADD COLUMN FileBlobId TEXT")
            database.execute(
                "UPDATE DavItems SET FileBlobId=?", (str(self.identifier),)
            )
        report = report_tests.native_report(
            1, skipped=[("/content/second.mkv", "no metadata blob")]
        )
        with self.assertRaisesRegex(
            ContainedImportError, "census is invalid"
        ) as caught:
            verify_execution_import_inventory(
                fixture.source,
                fixture.target,
                report,
                source_blob_census=lambda *args: {
                    "state": "present",
                    "bytes": len(self.payload),
                    "sha256": hashlib.sha256(self.payload).hexdigest(),
                    "raw_path": "private",
                },
            )
        retained = caught.exception.import_report
        self.assertEqual("blocked", retained["status"])
        self.assertEqual(1, retained["imported_files"])
        self.assertEqual(1, retained["skipped_files"])
        self.assertFalse(retained["review_required"])
        self.assertIn("census is invalid", retained["blocking_reasons"][0])
        self.assertEqual(retained, caught.exception.__cause__.import_report)
