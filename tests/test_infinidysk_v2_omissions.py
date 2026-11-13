"""Full private omission consent, immutable inputs and strict history preservation."""

from copy import deepcopy
import hashlib
import json
import os
import shutil
import sqlite3
import unittest
import uuid
from unittest.mock import patch

from tests import test_infinidysk_v2_import_report as fixtures
from utils.infinidysk_v2_import_report import ImportReportError, verify_import_inventory
from utils.infinidysk_v2_omissions import (
    OmissionProofError,
    make_omission_proof,
    validate_omission_proof,
    verify_omission_subset,
)


def sample_proof(count=3):
    return make_omission_proof(
        {"count": 500, "sha256": "a" * 64},
        {"count": 12, "sha256": "b" * 64},
        [
            {
                "id": uuid.UUID(int=index + 10).hex,
                "name": f"file-{index}",
                "path": f"/content/file-{index}",
                "reason": "no segments",
            }
            for index in range(count)
        ],
    )


class CompleteProofTests(unittest.TestCase):
    def test_same_or_fewer_exact_losses_including_zero_are_allowed(self):
        reviewed = sample_proof()
        self.assertTrue(verify_omission_subset(reviewed, sample_proof()))
        self.assertTrue(verify_omission_subset(reviewed, sample_proof(1)))
        self.assertTrue(verify_omission_subset(reviewed, sample_proof(0)))

    def test_fewer_count_with_new_identity_or_changed_record_is_not_consent(self):
        reviewed = sample_proof()
        for field, value in (
            ("id", uuid.UUID(int=999).hex),
            ("name", "renamed"),
            ("path", "/content/renamed"),
            ("reason", "no metadata blob"),
        ):
            with self.subTest(field=field):
                rows = deepcopy(reviewed["omitted"][:1])
                rows[0][field] = value
                actual = make_omission_proof(
                    reviewed["source_media"], reviewed["source_history"], rows
                )
                with self.assertRaisesRegex(
                    OmissionProofError, "outside the exact reviewed"
                ):
                    verify_omission_subset(reviewed, actual)

    def test_source_census_or_policy_change_fails_even_without_remaining_losses(self):
        reviewed = sample_proof()
        for field in ("source_media", "source_history"):
            for key, value in (("count", 501), ("sha256", "c" * 64)):
                with self.subTest(field=field, key=key):
                    actual = sample_proof(0)
                    actual[field][key] = value
                    actual = make_omission_proof(
                        actual["source_media"], actual["source_history"], []
                    )
                    with self.assertRaisesRegex(OmissionProofError, "census changed"):
                        verify_omission_subset(reviewed, actual)
        actual = sample_proof()
        actual["policy"] = "old-policy"
        with self.assertRaises(OmissionProofError):
            validate_omission_proof(actual)

    def test_incomplete_tampered_duplicate_or_provider_proof_is_refused(self):
        for mutate in (
            lambda value: value.pop("source_history"),
            lambda value: value["omitted"].pop(),
            lambda value: value["omitted"].append(deepcopy(value["omitted"][0])),
            lambda value: value["omitted"][0].update(reason="provider unavailable"),
            lambda value: value.update(review_digest="0" * 64),
        ):
            proof = sample_proof()
            mutate(proof)
            with self.assertRaises(OmissionProofError):
                validate_omission_proof(proof)

    def test_full_proof_bound_and_canonical_records_are_enforced(self):
        with patch("utils.infinidysk_v2_omissions.MAX_OMISSION_PROOF_BYTES", 200):
            with self.assertRaisesRegex(OmissionProofError, "storage bound"):
                validate_omission_proof(sample_proof())
        proof = sample_proof()
        proof["omitted"].reverse()
        with self.assertRaises(OmissionProofError):
            validate_omission_proof(proof)

    def test_all_records_past_public_sample_limit_remain_part_of_consent(self):
        reviewed = sample_proof(201)
        actual = make_omission_proof(
            reviewed["source_media"],
            reviewed["source_history"],
            reviewed["omitted"][200:],
        )
        self.assertTrue(verify_omission_subset(reviewed, actual))
        rows = deepcopy(actual["omitted"])
        rows[0]["reason"] = "no file size"
        changed = make_omission_proof(
            reviewed["source_media"], reviewed["source_history"], rows
        )
        with self.assertRaises(OmissionProofError):
            verify_omission_subset(reviewed, changed)


class SourceBoundProofTests(unittest.TestCase):
    setUp = fixtures.ImportReportTests.setUp

    def proof(self, *, reason="no metadata blob", history=1):
        return verify_import_inventory(
            self.source,
            self.target,
            fixtures.native_report(
                1, history=history, skipped=[("/content/second.mkv", reason)]
            ),
        )["omission_proof"]

    def blob(self, value, data):
        identifier = uuid.UUID(value)
        path = (
            self.source.parent
            / "blobs"
            / identifier.hex[:2]
            / identifier.hex[2:4]
            / str(identifier)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_real_reconciliation_three_to_one_then_zero_retains_same_source_proof(self):
        extra = [(uuid.UUID(int=30), "third.mkv"), (uuid.UUID(int=31), "fourth.mkv")]
        with sqlite3.connect(self.source) as db:
            for identifier, name in extra:
                db.execute(
                    "INSERT INTO DavItems VALUES(?,?,?,2)",
                    (str(identifier), name, "/content/" + name),
                )
        skipped = [("/content/second.mkv", "no segments")] + [
            ("/content/" + name, "no segments") for _id, name in extra
        ]
        reviewed = verify_import_inventory(
            self.source, self.target, fixtures.native_report(1, skipped=skipped)
        )["omission_proof"]
        with sqlite3.connect(self.target) as db:
            db.execute(
                "INSERT INTO items VALUES(?,?,2)", (self.second.bytes, "second.mkv")
            )
            db.execute(
                "INSERT INTO items VALUES(?,?,2)", (extra[0][0].bytes, extra[0][1])
            )
        actual = verify_import_inventory(
            self.source, self.target, fixtures.native_report(3, skipped=skipped[-1:])
        )["omission_proof"]
        self.assertTrue(verify_omission_subset(reviewed, actual))
        with sqlite3.connect(self.target) as db:
            db.execute(
                "INSERT INTO items VALUES(?,?,2)", (extra[1][0].bytes, extra[1][1])
            )
        receipt = verify_import_inventory(
            self.source, self.target, fixtures.native_report(4)
        )
        self.assertIsNone(receipt["import_report"]["review_digest"])
        self.assertEqual(receipt["omission_proof"]["omitted"], [])
        self.assertTrue(verify_omission_subset(reviewed, receipt["omission_proof"]))

    def test_payload_metadata_change_is_bound_but_health_scheduling_is_not(self):
        with sqlite3.connect(self.source) as db:
            db.executescript(
                "ALTER TABLE DavItems ADD FileSize INTEGER; ALTER TABLE DavItems ADD LastHealthCheck INTEGER; ALTER TABLE DavItems ADD NextHealthCheck INTEGER;"
            )
        reviewed = self.proof()
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE DavItems SET LastHealthCheck=10,NextHealthCheck=20")
        self.assertEqual(reviewed, self.proof())
        with sqlite3.connect(self.source) as db:
            db.execute(
                "UPDATE DavItems SET FileSize=123 WHERE Id=?", (str(self.second),)
            )
        with self.assertRaisesRegex(OmissionProofError, "census changed"):
            verify_omission_subset(reviewed, self.proof())

    def test_metadata_and_nzb_blob_content_changes_are_bound_even_with_same_guid(self):
        for table, field, identifier in (
            ("DavItems", "FileBlobId", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            ("DavItems", "NzbBlobId", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            ("HistoryItems", "NzbBlobId", "cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        ):
            with self.subTest(table=table, field=field):
                with sqlite3.connect(self.source) as db:
                    db.execute(f"ALTER TABLE {table} ADD {field} TEXT")
                    db.execute(f"UPDATE {table} SET {field}=?", (identifier,))
                path = self.blob(identifier, b"reserved original blob")
                reviewed = self.proof()
                path.write_bytes(b"reserved replaced blob")
                with self.assertRaisesRegex(OmissionProofError, "census changed"):
                    verify_omission_subset(reviewed, self.proof())

    def test_missing_blob_becoming_present_changes_source_proof(self):
        identifier = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        with sqlite3.connect(self.source) as db:
            db.execute("ALTER TABLE DavItems ADD FileBlobId TEXT")
            db.execute("UPDATE DavItems SET FileBlobId=?", (identifier,))
        reviewed = self.proof()
        self.blob(identifier, b"reserved newly present metadata")
        with self.assertRaisesRegex(OmissionProofError, "census changed"):
            verify_omission_subset(reviewed, self.proof())

    def test_blob_symlink_does_not_follow_or_disclose_external_content(self):
        identifier = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        with sqlite3.connect(self.source) as db:
            db.execute("ALTER TABLE DavItems ADD FileBlobId TEXT")
            db.execute("UPDATE DavItems SET FileBlobId=?", (identifier,))
        path = self.blob(identifier, b"old")
        external = self.root / "external-private"
        secret = b"reserved secret content"
        external.write_bytes(secret)
        path.unlink()
        path.symlink_to(external)
        with self.assertRaises(ImportReportError) as caught:
            self.proof()
        detail = str(caught.exception)
        self.assertNotIn(secret.decode(), detail)
        self.assertNotIn(hashlib.sha256(secret).hexdigest(), detail)

    def test_relocated_database_uses_explicit_staged_blob_root_without_fallback(self):
        identifier = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        with sqlite3.connect(self.source) as db:
            db.execute("ALTER TABLE DavItems ADD FileBlobId TEXT")
            db.execute("UPDATE DavItems SET FileBlobId=?", (identifier,))
        path = self.blob(identifier, b"reserved staged metadata")
        reviewed = self.proof()
        relocated = self.root / "archive-extras" / "source-database.sqlite"
        relocated.parent.mkdir()
        shutil.copyfile(self.source, relocated)
        report = fixtures.native_report(
            1, skipped=[("/content/second.mkv", "no metadata blob")]
        )
        result = verify_import_inventory(
            relocated, self.target, report, source_blob_root=self.root / "blobs"
        )
        self.assertEqual(result["omission_proof"], reviewed)
        path.write_bytes(b"reserved changed staged metadata")
        changed = verify_import_inventory(
            relocated, self.target, report, source_blob_root=self.root / "blobs"
        )
        with self.assertRaisesRegex(OmissionProofError, "census changed"):
            verify_omission_subset(reviewed, changed["omission_proof"])
        with self.assertRaisesRegex(ImportReportError, "directory is missing"):
            verify_import_inventory(
                relocated,
                self.target,
                report,
                source_blob_root=relocated.parent / "blobs",
            )

    def test_explicit_blob_root_is_direct_and_cannot_blindly_follow_aliases(self):
        actual = self.root / "blobs"
        actual.mkdir()
        alias = self.root / "blob-alias"
        alias.symlink_to(actual, target_is_directory=True)
        report = fixtures.native_report(
            1, skipped=[("/content/second.mkv", "no segments")]
        )
        for root in (alias, "relative-blobs"):
            with self.subTest(root=str(root)):
                with self.assertRaises(ImportReportError):
                    verify_import_inventory(
                        self.source, self.target, report, source_blob_root=root
                    )
        result = verify_import_inventory(
            self.source, self.target, report, source_blob_root=actual
        )
        self.assertTrue(validate_omission_proof(result["omission_proof"]))

    def test_absent_explicit_blob_directory_is_allowed_only_without_guid_references(
        self,
    ):
        report = fixtures.native_report(
            1, skipped=[("/content/second.mkv", "no segments")]
        )
        result = verify_import_inventory(
            self.source, self.target, report, source_blob_root=self.root / "no-blobs"
        )
        self.assertTrue(validate_omission_proof(result["omission_proof"]))
        with sqlite3.connect(self.source) as db:
            db.execute("ALTER TABLE HistoryItems ADD NzbBlobId TEXT")
            db.execute(
                "UPDATE HistoryItems SET NzbBlobId=?", (str(uuid.UUID(int=100)),)
            )
        with self.assertRaisesRegex(ImportReportError, "directory is missing"):
            verify_import_inventory(
                self.source,
                self.target,
                report,
                source_blob_root=self.root / "no-blobs",
            )

    def test_blob_fifo_is_rejected_without_waiting_for_a_writer(self):
        identifier = "99999999-9999-4999-8999-999999999999"
        with sqlite3.connect(self.source) as db:
            db.execute("ALTER TABLE DavItems ADD FileBlobId TEXT")
            db.execute("UPDATE DavItems SET FileBlobId=?", (identifier,))
        path = self.blob(identifier, b"old")
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(ImportReportError, "not a regular file"):
            self.proof()

    def test_history_per_attempt_count_can_be_lower_only_when_every_identity_is_present(
        self,
    ):
        proof = self.proof(history=0)
        self.assertTrue(validate_omission_proof(proof))
        with sqlite3.connect(self.target) as db:
            db.execute("DELETE FROM history")
        with self.assertRaisesRegex(
            ImportReportError, "required history identities missing"
        ):
            self.proof(history=0)

    def test_missing_failed_history_remains_blocking_with_useful_counts(self):
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE HistoryItems SET DownloadStatus=2")
        with sqlite3.connect(self.target) as db:
            db.execute("DELETE FROM history")
        with self.assertRaises(ImportReportError) as caught:
            self.proof()
        self.assertIn("0 completed and 1 failed", str(caught.exception))
        self.assertEqual(caught.exception.import_report["history"]["missing_failed"], 1)

    def test_unknown_status_and_invalid_or_reserved_history_are_not_warned_away(self):
        for identifier, status, expected in (
            (str(self.history), 3, "unsupported status"),
            ("invalid-guid", 2, "invalid/reserved"),
            (str(uuid.UUID(int=3)), 2, "invalid/reserved"),
        ):
            with self.subTest(identifier=identifier, status=status):
                with sqlite3.connect(self.source) as db:
                    db.execute(
                        "UPDATE HistoryItems SET Id=?,DownloadStatus=?",
                        (identifier, status),
                    )
                with self.assertRaisesRegex(ImportReportError, expected):
                    self.proof()

    def test_overcount_or_canonical_duplicate_history_remains_blocking(self):
        with self.assertRaisesRegex(ImportReportError, "history identities or totals"):
            self.proof(history=2)
        with sqlite3.connect(self.source) as db:
            db.execute(
                "INSERT INTO HistoryItems VALUES(?,2)", (str(self.history).upper(),)
            )
        with self.assertRaisesRegex(ImportReportError, "history identities or totals"):
            self.proof(history=1)

    def test_omission_proof_is_private_and_public_report_retains_only_samples_and_digest(
        self,
    ):
        receipt = verify_import_inventory(
            self.source,
            self.target,
            fixtures.native_report(1, skipped=[("/content/second.mkv", "no segments")]),
        )
        proof = receipt["omission_proof"]
        self.assertEqual(
            proof["review_digest"], receipt["import_report"]["review_digest"]
        )
        self.assertNotIn("omitted", receipt["import_report"])
        self.assertNotIn("source_media", receipt["import_report"])
        self.assertIn(self.second.hex, json.dumps(proof))


if __name__ == "__main__":
    unittest.main()
