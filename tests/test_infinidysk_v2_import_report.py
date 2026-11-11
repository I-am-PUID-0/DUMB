import hashlib
import sqlite3
import tempfile
from pathlib import Path
import unittest
import uuid

from utils.infinidysk_v2_import_report import (
    ImportReportError,
    MAX_REPORT_BYTES,
    parse_import_report,
    verify_import_inventory,
)


def native_report(files, history=1, skipped=(), fetched=0):
    report = (
        f"Imported 1 directories, {files} files, 0 NZBs, {history} history rows, 0 queued jobs\n"
        f"  fetched {fetched} articles to learn segment sizes\n"
    )
    if skipped:
        report += f"Files not carried over: {len(skipped)}\n"
        groups = {}
        for path, reason in skipped:
            groups.setdefault(reason, []).append(path)
        for reason, paths in groups.items():
            report += f"  {len(paths)}: {reason}\n"
            report += "".join(f"    {path}\n" for path in paths)
    return report


def native_preamble():
    return (
        "C# schema 20260824160000_Example: supported\n"
        "Items: 1 directories; files: 2 NZB, 0 multipart, 0 RAR (legacy)\n"
        "  decoded 1, failed 0, metadata blob missing 1, NZB blob missing 0\n"
        "Import must:\n"
        "  derive segment sizes for 0 segment runs that never recorded them\n"
        "  resolve trailing RAR volumes of 0 files\n"
        "  check 0 files whose ranges disagree with their size\n"
        "Carried over:\n"
        "  0 encrypted files, decrypted on read\n"
        "  0 segment runs with unvalidated sizes\n"
        "  0 files with segments missing or corrupt everywhere\n"
        "  0 segment runs with fallback message-ids\n"
        "  0 files with PAR2 proofs\n"
        "  0 STRM outputs\n"
        "History: 1 completed, 0 failed; queue: 0\n"
        "Settings:\n"
        "  admin login: accepted from C# at the first sign-in\n"
        "  SAB API key kept\n"
        "  categories: movies (manual: manual)\n"
        "  mount dir: /mnt/example\n"
        "  public URL: http://example.invalid\n"
        "  single sign-on: off\n"
        "  import strategy Symlinks; STRM directory /strm; STRM key kept; library folders: \n"
        "  WebDAV user example, password kept\n"
        "  imports at once: 1; extra blocklist: \n"
        "  health checks on, degraded tolerance off\n"
        "  download limit: none\n"
        "  provider example: catalog example, backbone own, level 0, 1 connections\n"
        "Note: example native migration note\n"
    )


class ImportReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.sqlite"
        self.target = self.root / "target.sqlite"
        self.first = uuid.UUID(int=10)
        self.second = uuid.UUID(int=11)
        self.history = uuid.UUID(int=12)
        with sqlite3.connect(self.source) as database:
            database.execute(
                "CREATE TABLE DavItems(Id TEXT,Name TEXT,Path TEXT,Type INTEGER)"
            )
            database.executemany(
                "INSERT INTO DavItems VALUES(?,?,?,2)",
                [
                    (str(self.first), "first.mkv", "/content/first.mkv"),
                    (str(self.second), "second.mkv", "/content/second.mkv"),
                ],
            )
            database.execute("CREATE TABLE HistoryItems(Id TEXT)")
            database.execute("INSERT INTO HistoryItems VALUES(?)", (str(self.history),))
        with sqlite3.connect(self.target) as database:
            database.execute("CREATE TABLE items(id BLOB,name TEXT,kind INTEGER)")
            database.execute(
                "INSERT INTO items VALUES(?,?,?)", (self.first.bytes, "first.mkv", 2)
            )
            database.execute("CREATE TABLE history(id BLOB)")
            database.execute("INSERT INTO history VALUES(?)", (self.history.bytes,))

    def verify(self, report):
        return verify_import_inventory(self.source, self.target, report)

    def test_actual_upgrade_survey_settings_preamble_is_supported(self):
        report = native_preamble() + native_report(
            1, skipped=[("/content/second.mkv", "no metadata blob")]
        )
        result = self.verify(report)
        self.assertEqual(result["import_report"]["status"], "review_required")

    def test_unknown_or_ambiguous_native_preamble_is_blocked(self):
        tail = native_report(1, skipped=[("/content/second.mkv", "no metadata blob")])
        for prefix in (
            "unknown preamble\n",
            native_preamble().replace("Settings:", "New settings:"),
            native_preamble() + "New policy section:\n",
            tail,
        ):
            with self.subTest(prefix=prefix[:20]):
                with self.assertRaises(ImportReportError):
                    self.verify(prefix + tail)

    def test_complete_import_is_accepted(self):
        with sqlite3.connect(self.target) as database:
            database.execute(
                "INSERT INTO items VALUES(?,?,?)", (self.second.bytes, "second.mkv", 3)
            )
        result = self.verify(native_report(2))
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["skipped_files"], 0)
        self.assertEqual(result["import_report"]["status"], "accepted")
        self.assertFalse(result["import_report"]["review_required"])
        self.assertIsNone(result["import_report"]["review_digest"])

    def test_known_exact_omission_requires_review_and_preserves_details(self):
        report = native_report(1, skipped=[("/content/second.mkv", "no metadata blob")])
        result = self.verify(report)
        details = result["import_report"]
        self.assertEqual(details["status"], "review_required")
        self.assertEqual(details["skipped_files"], 1)
        self.assertEqual(details["skipped"][0]["path"], "/content/second.mkv")
        self.assertEqual(
            details["reasons"], [{"reason": "no metadata blob", "count": 1}]
        )
        self.assertRegex(details["review_digest"], r"^[a-f0-9]{64}$")
        self.assertEqual(
            result["native_report_sha256"], hashlib.sha256(report.encode()).hexdigest()
        )

    def test_review_digest_binds_identity_reason_and_all_omissions(self):
        first = self.verify(
            native_report(1, skipped=[("/content/second.mkv", "no metadata blob")])
        )["import_report"]["review_digest"]
        different_reason = self.verify(
            native_report(1, skipped=[("/content/second.mkv", "no segments")])
        )["import_report"]["review_digest"]
        self.assertNotEqual(first, different_reason)
        with sqlite3.connect(self.source) as database:
            database.execute(
                "UPDATE DavItems SET Id=? WHERE Id=?",
                (str(uuid.UUID(int=20)), str(self.second)),
            )
        different_id = self.verify(
            native_report(1, skipped=[("/content/second.mkv", "no metadata blob")])
        )["import_report"]["review_digest"]
        self.assertNotEqual(first, different_id)

    def test_fetched_count_does_not_change_exact_omission_review(self):
        skips = [("/content/second.mkv", "no file size")]
        a = self.verify(native_report(1, skipped=skips, fetched=0))
        b = self.verify(native_report(1, skipped=skips, fetched=10))
        self.assertEqual(
            a["import_report"]["review_digest"], b["import_report"]["review_digest"]
        )
        self.assertNotEqual(a["native_report_sha256"], b["native_report_sha256"])

    def test_provider_and_unknown_failures_are_never_reviewable(self):
        for reason in (
            "segment sizes unknown: connection refused",
            "RAR volume unreadable: timeout",
            "metadata blob: permission denied",
            "unreadable id",
            "new unsupported reason",
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(ImportReportError) as caught:
                    self.verify(
                        native_report(1, skipped=[("/content/second.mkv", reason)])
                    )
                details = caught.exception.import_report
                self.assertEqual(details["status"], "blocked")
                self.assertFalse(details["review_required"])
                self.assertIsNone(details["review_digest"])
                self.assertEqual(details["skipped_files"], 1)
                self.assertTrue(details["blocking_reasons"])

    def test_unknown_numeric_file_type_is_not_a_known_exclusion(self):
        with self.assertRaises(ImportReportError):
            self.verify(
                native_report(
                    1, skipped=[("/content/second.mkv", "unknown file type 999")]
                )
            )

    def test_known_bounded_numeric_geometry_reason_is_reviewable(self):
        for reason in ("AES key of 15 bytes", "size 10 differs from C#'s 11"):
            with self.subTest(reason=reason):
                result = self.verify(
                    native_report(1, skipped=[("/content/second.mkv", reason)])
                )
                self.assertEqual(result["import_report"]["status"], "review_required")

    def test_unexplained_missing_identity_and_wrong_totals_block(self):
        for report in (
            native_report(1),
            native_report(2, skipped=[("/content/second.mkv", "no segments")]),
            native_report(1, skipped=[("/content/elsewhere.mkv", "no segments")]),
        ):
            with self.subTest(report=report):
                with self.assertRaises(ImportReportError):
                    self.verify(report)

    def test_reported_directory_skip_cannot_be_accepted_as_file_omission(self):
        with self.assertRaises(ImportReportError):
            self.verify(
                native_report(
                    1,
                    skipped=[
                        ("/content/second.mkv", "no metadata blob"),
                        ("/content/unreadable-directory", "unreadable id"),
                    ],
                )
            )

    def test_renamed_or_duplicate_imported_identity_blocks(self):
        with sqlite3.connect(self.target) as database:
            database.execute("UPDATE items SET name='renamed.mkv'")
        with self.assertRaises(ImportReportError):
            self.verify(
                native_report(1, skipped=[("/content/second.mkv", "no segments")])
            )
        with sqlite3.connect(self.target) as database:
            database.execute("UPDATE items SET name='first.mkv'")
            database.execute(
                "INSERT INTO items VALUES(?,?,?)", (self.first.bytes, "first.mkv", 2)
            )
        with self.assertRaises(ImportReportError):
            self.verify(
                native_report(1, skipped=[("/content/second.mkv", "no segments")])
            )

    def test_carried_media_must_remain_a_supported_file_kind(self):
        for kind in (1, 0, 9, None):
            with self.subTest(kind=kind):
                with sqlite3.connect(self.target) as database:
                    database.execute("UPDATE items SET kind=?", (kind,))
                with self.assertRaises(ImportReportError):
                    self.verify(
                        native_report(
                            1, skipped=[("/content/second.mkv", "no segments")]
                        )
                    )

    def test_missing_native_kind_schema_fails_closed(self):
        with sqlite3.connect(self.target) as database:
            database.execute("ALTER TABLE items RENAME TO old_items")
            database.execute("CREATE TABLE items(id BLOB,name TEXT)")
            database.execute("INSERT INTO items SELECT id,name FROM old_items")
        with self.assertRaises(ImportReportError):
            self.verify(
                native_report(1, skipped=[("/content/second.mkv", "no segments")])
            )

    def test_changed_or_missing_history_blocks(self):
        with sqlite3.connect(self.target) as database:
            database.execute("DELETE FROM history")
        with self.assertRaises(ImportReportError):
            self.verify(
                native_report(1, skipped=[("/content/second.mkv", "no segments")])
            )

    def test_duplicate_source_paths_block_ambiguous_omission(self):
        with sqlite3.connect(self.source) as database:
            database.execute(
                "UPDATE DavItems SET Path='/content/second.mkv' WHERE Id=?",
                (str(self.first),),
            )
        with self.assertRaises(ImportReportError):
            self.verify(
                native_report(1, skipped=[("/content/second.mkv", "no segments")])
            )

    def test_malformed_and_unknown_report_sections_block(self):
        clean = native_report(1)
        reports = (
            "",
            "new summary",
            clean + "Files not carried over: 1\n  1: no segments\n",
            clean
            + "Files not carried over: 2\n  1: no segments\n    /content/second.mkv\n",
            clean + "Files not carried over: 1\n  1: no segments\n    /outside/file\n",
            clean + "unknown additional section\n",
        )
        for report in reports:
            with self.subTest(report=report):
                with self.assertRaises(ImportReportError):
                    parse_import_report(report)

    def test_duplicate_native_path_or_group_blocks(self):
        report = native_report(
            0,
            skipped=[
                ("/content/second.mkv", "no segments"),
                ("/content/second.mkv", "no file size"),
            ],
        )
        with self.assertRaises(ImportReportError):
            parse_import_report(report)
        report = (
            native_report(0)
            + "Files not carried over: 2\n"
            + "  1: no segments\n    /content/first.mkv\n"
            + "  1: no segments\n    /content/second.mkv\n"
        )
        with self.assertRaises(ImportReportError):
            parse_import_report(report)

    def test_public_provider_details_redact_urls_and_credentials(self):
        with self.assertRaises(ImportReportError) as caught:
            self.verify(
                native_report(
                    1,
                    skipped=[
                        (
                            "/content/second.mkv",
                            "segment sizes unknown: nntps://user:secret@example.invalid token=private",
                        )
                    ],
                )
            )
        details = str(caught.exception.import_report)
        self.assertNotIn("user:secret", details)
        self.assertNotIn("token=private", details)
        self.assertIn("[URL omitted]", details)

    def test_public_samples_are_bounded_but_digest_covers_every_omission(self):
        skips = []
        with sqlite3.connect(self.source) as database:
            database.execute("DELETE FROM DavItems WHERE Id=?", (str(self.second),))
            for index in range(201):
                name = f"omitted-{index}.mkv"
                path = f"/content/{name}"
                database.execute(
                    "INSERT INTO DavItems VALUES(?,?,?,2)",
                    (str(uuid.UUID(int=100 + index)), name, path),
                )
                skips.append((path, "no segments"))
        result = self.verify(native_report(1, skipped=skips))
        details = result["import_report"]
        self.assertEqual(details["skipped_files"], 201)
        self.assertEqual(len(details["skipped"]), 200)
        self.assertTrue(details["skipped_details_truncated"])
        self.assertEqual(details["skipped_details_count"], 201)
        changed = skips[:-1] + [(skips[-1][0], "no file size")]
        second = self.verify(native_report(1, skipped=changed))
        self.assertNotEqual(
            details["review_digest"], second["import_report"]["review_digest"]
        )

    def test_extreme_or_non_ascii_report_counters_are_not_native_format(self):
        for report in (
            "Imported "
            + "9" * 5000
            + " directories, 1 files, 0 NZBs, 1 history rows, 0 queued jobs",
            "Imported ١ directories, 1 files, 0 NZBs, 1 history rows, 0 queued jobs",
        ):
            with self.subTest(report=report[:30]):
                with self.assertRaises(ImportReportError):
                    parse_import_report(report)

    def test_report_byte_limit_is_enforced(self):
        with self.assertRaises(ImportReportError):
            parse_import_report("x" * (MAX_REPORT_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
