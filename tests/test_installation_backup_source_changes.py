"""Late cold-source changes retain bounded evidence without accepting an archive."""

import hashlib
import io
import json
import os
import unittest
from unittest.mock import patch

from tests import test_installation_backup as fixtures
from utils import installation_backup_archive as archive
from utils.installation_backup_codec import zstd_available


class LateSourceDiagnosticsTests(unittest.TestCase):
    setUp = fixtures.ArchiveTests.setUp

    def late_error(self, mutate, *, compression="none"):
        output, changed = io.BytesIO(), []

        def observe(**event):
            if event["stage"] == "checking_source" and not changed:
                # This phase starts only after tar and its optional codec close.
                self.assertGreater(output.tell(), 0)
                changed.append(True)
                mutate()

        with self.assertRaises(archive.InstallationArchiveError) as caught:
            archive.write_archive(
                output,
                self.metadata,
                self.roots,
                self.scratch,
                progress=observe,
                compression=compression,
            )
        self.assertEqual(changed, [True])
        self.assertEqual(list(self.scratch.glob(".installation-manifest-*")), [])
        detail = str(caught.exception)
        self.assertNotIn("\n", detail)
        self.assertLess(len(detail), 1600)
        return detail

    def test_added_entry_is_named_despite_ancestor_directory_timestamp_changes(self):
        path = self.source / "new\nstate.sqlite"
        secret = b"reserved new payload must not enter diagnostics"
        detail = self.late_error(lambda: path.write_bytes(secret))
        self.assertIn("Added entry at " + json.dumps(str(path)), detail)
        self.assertNotIn("Removed entry", detail)
        self.assertNotIn(secret.decode(), detail)
        self.assertNotIn(hashlib.sha256(secret).hexdigest(), detail)

    def test_removed_entry_is_named_despite_ancestor_directory_timestamp_changes(self):
        path = self.source / "removed.sqlite"
        path.write_bytes(b"reserved original content")
        detail = self.late_error(path.unlink)
        self.assertIn("Removed entry at " + json.dumps(str(path)), detail)
        self.assertNotIn("Added entry", detail)

    def test_same_size_content_change_is_identified_with_original_mtime_restored(self):
        path = self.source / "credentials.json"
        before, after = b"reserved old private value", b"reserved new private value"
        self.assertEqual(len(before), len(after))
        path.write_bytes(before)
        stamp = path.stat()

        def mutate():
            path.write_bytes(after)
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

        detail = self.late_error(mutate)
        self.assertIn("Changed entry at " + json.dumps(str(path)), detail)
        self.assertIn("changed fields: content.", detail)
        for payload in (before, after):
            self.assertNotIn(payload.decode(), detail)
            self.assertNotIn(hashlib.sha256(payload).hexdigest(), detail)
        self.assertNotIn("mtime_ns", detail)

    def test_late_ctime_only_change_keeps_existing_acceptance_and_archive_bytes(self):
        path = self.source / "stable.sqlite"
        path.write_bytes(b"reserved unchanged content")
        path.chmod(0o600)
        baseline = io.BytesIO()
        archive.write_archive(baseline, self.metadata, self.roots, self.scratch)
        output, changed = io.BytesIO(), []
        before = path.stat()

        def observe(**event):
            if event["stage"] == "checking_source" and not changed:
                changed.append(True)
                path.chmod(0o640)
                path.chmod(0o600)

        archive.write_archive(
            output, self.metadata, self.roots, self.scratch, progress=observe
        )
        self.assertEqual(changed, [True])
        self.assertNotEqual(path.stat().st_ctime_ns, before.st_ctime_ns)
        self.assertEqual(output.getvalue(), baseline.getvalue())
        proof = archive.inspect_archive(io.BytesIO(output.getvalue()), self.scratch)
        self.assertTrue(proof["verified"])
        self.assertEqual(list(self.scratch.glob(".installation-manifest-*")), [])

    def test_permission_change_reports_field_name_without_values(self):
        path = self.source / "private-key"
        path.write_bytes(b"reserved private value")
        path.chmod(0o600)
        detail = self.late_error(lambda: path.chmod(0o640))
        self.assertIn("Changed entry at " + json.dumps(str(path)), detail)
        self.assertIn("changed fields: mode.", detail)
        self.assertNotIn("reserved private value", detail)
        self.assertNotIn("640", detail)

    def test_symlink_change_names_link_without_disclosing_either_target(self):
        path = self.source / "library-link"
        path.symlink_to("/reserved/original-private-target")

        def mutate():
            path.unlink()
            path.symlink_to("/reserved/replacement-private-target")

        detail = self.late_error(mutate)
        self.assertIn("Changed entry at " + json.dumps(str(path)), detail)
        self.assertIn("link_target", detail)
        self.assertNotIn("original-private-target", detail)
        self.assertNotIn("replacement-private-target", detail)

    def test_many_changes_and_long_paths_keep_one_bounded_sample_per_category(self):
        parent = self.source
        for _ in range(6):
            parent = parent / ("reserved-" + "x" * 180)
            parent.mkdir()
        removed = [parent / f"old-{index:03d}" for index in range(20)]
        for path in removed:
            path.write_bytes(b"reserved removable payload")
        changed = parent / "retained"
        changed.write_bytes(b"reserved initial payload")

        def mutate():
            for path in removed:
                path.unlink()
            for index in range(20):
                (parent / f"new-{index:03d}").write_bytes(b"reserved added payload")
            changed.write_bytes(b"reserved changed payload")

        detail = self.late_error(mutate)
        # The frontend's shared error formatter retains at most 1,600 chars.
        visible = detail[:1600]
        self.assertEqual(visible, detail)
        self.assertEqual(visible.count("Added entry at "), 1)
        self.assertEqual(visible.count("Removed entry at "), 1)
        self.assertEqual(visible.count("Changed entry at "), 1)
        self.assertIn("changed fields:", visible)
        self.assertIn("content", visible)
        self.assertIn("[path truncated]", visible)
        for payload in ("removable payload", "added payload", "changed payload"):
            self.assertNotIn(payload, detail)

    @unittest.skipUnless(zstd_available(), "Native Zstandard codec is unavailable")
    def test_compressed_output_keeps_same_late_change_rejection_and_evidence(self):
        path = self.source / "compressed-late-addition"
        detail = self.late_error(
            lambda: path.write_bytes(b"reserved"), compression="zstd"
        )
        self.assertIn("Added entry at " + json.dumps(str(path)), detail)


class ManagerLateSourceChangeTests(unittest.TestCase):
    setUp = fixtures.ManagerTests.setUp
    join_worker = fixtures.ManagerTests.join_worker
    start_backup = fixtures.ManagerTests.start_backup

    def test_real_late_content_change_reports_path_resumes_and_publishes_no_archive(
        self,
    ):
        path = self.payload
        old = path.read_bytes()
        original = self.manager._backup_progress
        changed = []

        def progress(identifier):
            report = original(identifier)

            def observe(**event):
                if event["stage"] == "checking_source" and not changed:
                    changed.append(True)
                    path.write_bytes(old + b"reserved late change")
                report(**event)

            return observe

        with patch.object(self.manager, "_backup_progress", progress):
            job = self.start_backup()
        self.assertEqual(changed, [True])
        self.assertEqual(job["status"], "failed", job)
        self.assertFalse(job["can_rollback"])
        self.assertIn("Changed entry at " + json.dumps(str(path)), job["errors"][0])
        self.assertIn("content", job["errors"][0])
        self.assertNotIn("reserved late change", job["errors"][0])
        self.assertEqual(self.handler.events, [("stop", "App"), ("start", "App")])
        self.assertFalse(self.manager.has_blocking_job())
        self.assertEqual(self.manager.catalog("fixture")["artifacts"], [])
        self.assertEqual(list(self.root.joinpath("archives").rglob("*.partial")), [])
        self.assertFalse((self.manager.workspace_root / job["job_id"]).exists())
        self.assertEqual(
            list(self.manager.workspace_root.rglob(".installation-manifest-*")), []
        )
        self.assertEqual(path.read_bytes(), old + b"reserved late change")


if __name__ == "__main__":
    unittest.main()
