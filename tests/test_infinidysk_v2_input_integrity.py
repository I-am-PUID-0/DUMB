"""CIFS metadata refresh cannot replace content, identity or release proofs."""

from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_infinidysk_v2_blob_staging as fixtures
from utils import infinidysk_v2_blob_staging as staging
from utils.infinidysk_v2_input_integrity import input_change, public_input_integrity
from utils.infinidysk_v2_inventory import InventoryError


@contextmanager
def fixture(cifs=True, *, additional=None):
    value = fixtures.ExternalBlobInputTests("runTest")

    def nested(data):
        (data / "blobs/nested").mkdir()
        (data / "blobs/nested/child").write_bytes(b"nested retained data")
        if additional is not None:
            additional(data)

    value._source_preparer = nested
    value.setUp()
    try:
        if cifs:
            original = value.staging._storage

            def storage(job):
                store, bound = original(job)
                return store, dict(bound, filesystem_type="cifs")

            with patch.object(value.staging, "_storage", side_effect=storage):
                yield value
        else:
            yield value
    finally:
        value.doCleanups()


def historical_ctime(database):
    rows = database.execute("SELECT path,staged FROM entries").fetchall()
    for path, raw in rows:
        stamp = json.loads(raw)
        stamp[5] += 10**9
        database.execute(
            "UPDATE entries SET staged=? WHERE path=?", (json.dumps(stamp), path)
        )


@contextmanager
def cached_copy_stamp():
    """Model the measured owned WRFD timestamps; reopened observations are real."""
    original = os.fstat
    observed = []

    def stat_copy(descriptor):
        value = original(descriptor)
        try:
            path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            write_only = (
                fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY
            )
        except OSError:
            return value
        if (
            write_only
            and staging.NAMESPACE in path.parts
            and stat.S_ISREG(value.st_mode)
        ):
            fields = {
                name: getattr(value, name)
                for name in dir(value)
                if name.startswith("st_")
            }
            fields["st_mtime_ns"] -= 12_000_000
            fields["st_ctime_ns"] -= 12_000_000
            observed.append(path.name)
            return SimpleNamespace(**fields)
        return value

    with patch.object(os, "fstat", side_effect=stat_copy):
        yield observed


class StagedCifsIntegrityTests(unittest.TestCase):
    def test_cifs_write_close_finalization_uses_the_stable_reopened_stamp(self):
        with fixture() as f, cached_copy_stamp() as observed:
            output = f.prepare()
            self.assertIn("metadata", observed)
            with closing(
                sqlite3.connect(f.snapshot["external_blob_staging"]["manifest"])
            ) as database:
                recorded = json.loads(
                    database.execute(
                        "SELECT staged FROM entries WHERE path=?", (b"metadata",)
                    ).fetchone()[0]
                )
            self.assertEqual(recorded[4], (output / "metadata").stat().st_mtime_ns)
            self.assertEqual(
                (output / "metadata").read_bytes(), b"retained original blob"
            )
            self.assertEqual(f.staging.verify(f.job), output)
            f.controller.restore(f.job, lambda *_: None)
            self.assertEqual(
                (f.data / "blobs/metadata").read_bytes(), b"retained original blob"
            )
            f.staging.cleanup(f.job)
            self.assertEqual(f.snapshot["external_blob_staging"]["state"], "cleaned")

    def test_non_cifs_write_close_timestamp_change_is_refused(self):
        with fixture(cifs=False) as f, cached_copy_stamp():
            with self.assertRaisesRegex(ValueError, "mtime_ns"):
                f.prepare()
            self.assertEqual(f.snapshot["external_blob_staging"]["released_bytes"], 0)
            self.assertEqual(
                f.job["input_integrity"]["changed_fields"], ["mtime_ns", "ctime_ns"]
            )
            self.assertTrue((f.candidate / "blobs/metadata").exists())

    def test_fresh_reopened_ctime_mutation_is_never_close_finalization(self):
        with fixture() as f:
            original = f.staging._closed_copy_stamp
            changed = []

            def closing_stamp(job, root, relative, database, written, **options):
                check = options["check"]
                calls = 0

                def window():
                    nonlocal calls
                    check()
                    calls += 1
                    if relative == "metadata" and calls == 2:
                        path = root / relative
                        path.chmod(stat.S_IMODE(path.stat().st_mode))
                        changed.append(relative)

                return original(
                    job,
                    root,
                    relative,
                    database,
                    written,
                    **dict(options, check=window),
                )

            with patch.object(
                f.staging, "_closed_copy_stamp", side_effect=closing_stamp
            ):
                with self.assertRaisesRegex(ValueError, "ctime_ns"):
                    f.prepare()
            self.assertEqual(changed, ["metadata"])
            self.assertEqual(f.snapshot["external_blob_staging"]["released_bytes"], 0)

    def test_tampering_after_canonical_close_still_requires_full_sha_readback(self):
        with fixture() as f:
            original = f.staging._closed_copy_stamp

            def tamper(job, root, relative, database, written, **options):
                result = original(job, root, relative, database, written, **options)
                if relative == "metadata":
                    path = root / relative
                    before = path.stat()
                    path.write_bytes(b"x" * before.st_size)
                    os.utime(path, ns=(before.st_atime_ns, result[4]))
                return result

            with patch.object(f.staging, "_closed_copy_stamp", side_effect=tamper):
                with self.assertRaisesRegex(ValueError, "changed fields: sha256"):
                    f.prepare()
            self.assertEqual(f.job["input_integrity"]["changed_fields"], ["sha256"])
            self.assertEqual(f.snapshot["external_blob_staging"]["released_bytes"], 0)
            self.assertEqual(
                (f.candidate / "blobs/metadata").read_bytes(), b"retained original blob"
            )

    def test_pre_metadata_failure_keeps_the_complete_ledger_and_safe_fields(self):
        relative = "private-media-label"
        with fixture(
            additional=lambda data: (data / "blobs" / relative).write_bytes(b"retained")
        ) as f:
            original = f.staging._io
            checked = []

            def phase(job, name, **options):
                if name == "blob_metadata":
                    record = f.snapshot["external_blob_staging"]
                    with closing(sqlite3.connect(record["manifest"])) as database:
                        checked.append(
                            database.execute("SELECT COUNT(*) FROM entries").fetchone()[
                                0
                            ]
                        )
                    path = Path(record["directory"]) / "blobs" / relative
                    before = path.stat()
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 10**9))
                return original(job, name, **options)

            with patch.object(f.staging, "_io", side_effect=phase):
                with self.assertRaises(ValueError) as caught:
                    f.prepare()
            record = f.snapshot["external_blob_staging"]
            self.assertEqual(checked, [record["entries"]])
            with closing(sqlite3.connect(record["manifest"])) as database:
                self.assertEqual(
                    database.execute("SELECT COUNT(*) FROM entries").fetchone()[0],
                    record["entries"],
                )
            evidence = f.job["input_integrity"]
            self.assertEqual(evidence["changed_fields"], ["mtime_ns"])
            self.assertEqual(evidence["entry_type"], "file")
            self.assertEqual(
                evidence["path_digest"], hashlib.sha256(relative.encode()).hexdigest()
            )
            self.assertNotIn(relative, str(caught.exception))
            self.assertNotIn(relative, json.dumps(evidence))
            self.assertEqual(record["released_bytes"], 0)

    def test_directory_pre_metadata_diagnostics_include_only_guarded_fields(self):
        relative = "private-directory-label"
        with fixture(additional=lambda data: (data / "blobs" / relative).mkdir()) as f:
            original = f.staging._io

            def phase(job, name, **options):
                if name == "blob_metadata":
                    path = (
                        Path(f.snapshot["external_blob_staging"]["directory"])
                        / "blobs"
                        / relative
                    )
                    before = path.stat()
                    path.chmod(stat.S_IMODE(before.st_mode) | stat.S_IRGRP)
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 10**9))
                return original(job, name, **options)

            with patch.object(f.staging, "_io", side_effect=phase):
                with self.assertRaises(ValueError):
                    f.prepare()
            self.assertEqual(f.job["input_integrity"]["entry_type"], "directory")
            self.assertEqual(f.job["input_integrity"]["changed_fields"], ["mode"])
            self.assertEqual(f.snapshot["external_blob_staging"]["released_bytes"], 0)

    def test_cifs_historical_ctime_refresh_preserves_full_readback_release_and_cleanup(
        self,
    ):
        with fixture() as f:
            original = f.staging._verify_tree
            modified = False

            def verify(job, root, database, **options):
                nonlocal modified
                if options["stage"] and not modified:
                    historical_ctime(database)
                    modified = True
                return original(job, root, database, **options)

            with patch.object(f.staging, "_verify_tree", side_effect=verify):
                output = f.prepare()
                self.assertEqual(
                    (output / "metadata").read_bytes(), b"retained original blob"
                )
                self.assertEqual(f.staging.verify(f.job), output)
                f.staging.cleanup(f.job)
            self.assertEqual(f.snapshot["external_blob_staging"]["state"], "cleaned")
            self.assertFalse(output.exists())
            self.assertTrue(f.snapshot["cold_archive"]["verified"])
            self.assertFalse(list(f.scratch.glob(".input-verified-*")))
            self.assertEqual(
                f.snapshot["external_blob_staging"]["copied_bytes"],
                f.snapshot["external_blob_staging"]["logical_bytes"],
            )

    def test_non_cifs_historical_ctime_change_is_refused_before_local_release(self):
        with fixture(cifs=False) as f:
            original = f.staging._verify_tree

            def verify(job, root, database, **options):
                if options["stage"]:
                    historical_ctime(database)
                return original(job, root, database, **options)

            with patch.object(f.staging, "_verify_tree", side_effect=verify):
                with self.assertRaisesRegex(ValueError, "ctime_ns"):
                    f.prepare()
            self.assertEqual(
                (f.candidate / "blobs/metadata").read_bytes(), b"retained original blob"
            )
            self.assertEqual(f.job["input_integrity"]["side"], "staged")
            self.assertIn("ctime_ns", f.job["input_integrity"]["changed_fields"])

    def test_source_ctime_remains_exact_even_when_destination_is_cifs(self):
        with fixture() as f:
            original = f.staging._verify_tree

            def verify(job, root, database, **options):
                if not options["stage"]:
                    path, raw = database.execute(
                        "SELECT path,source FROM entries WHERE path=?", (b"metadata",)
                    ).fetchone()
                    expected = json.loads(raw)
                    expected[5] += 10**9
                    database.execute(
                        "UPDATE entries SET source=? WHERE path=?",
                        (json.dumps(expected), path),
                    )
                return original(job, root, database, **options)

            with patch.object(f.staging, "_verify_tree", side_effect=verify):
                with self.assertRaisesRegex(ValueError, "Local source.*ctime_ns"):
                    f.prepare()
            self.assertEqual(f.job["input_integrity"]["side"], "source")
            self.assertEqual(
                (f.candidate / "blobs/metadata").read_bytes(), b"retained original blob"
            )
            self.assertEqual(f.snapshot["external_blob_staging"]["released_bytes"], 0)

    def test_cifs_content_mtime_mode_identity_and_hardlink_changes_remain_blocked(self):
        for change in ("content", "mtime", "mode", "inode", "hardlink"):
            with self.subTest(change=change), fixture() as f:
                original = f.staging._verify_tree
                changed = False

                def verify(job, root, database, **options):
                    nonlocal changed
                    if options["stage"] and not changed:
                        changed = True
                        path = root / "metadata"
                        before = path.stat()
                        if change == "content":
                            path.write_bytes(b"x" * before.st_size)
                            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                        elif change == "mtime":
                            os.utime(
                                path,
                                ns=(
                                    before.st_atime_ns,
                                    before.st_mtime_ns + 1000000000,
                                ),
                            )
                        elif change == "mode":
                            path.chmod(0o400)
                        elif change == "inode":
                            replacement = root.parent / "unowned-replacement"
                            replacement.write_bytes(b"retained original blob")
                            os.utime(
                                replacement, ns=(before.st_atime_ns, before.st_mtime_ns)
                            )
                            os.replace(replacement, path)
                        else:
                            os.link(path, root / "additional-hardlink")
                    return original(job, root, database, **options)

                with patch.object(f.staging, "_verify_tree", side_effect=verify):
                    with self.assertRaises((ValueError, InventoryError)):
                        f.prepare()
                self.assertEqual(
                    (f.candidate / "blobs/metadata").read_bytes(),
                    b"retained original blob",
                )
                self.assertEqual(
                    f.snapshot["external_blob_staging"]["released_bytes"], 0
                )
                if change == "content":
                    self.assertEqual(
                        f.job["input_integrity"]["changed_fields"], ["sha256"]
                    )

    def test_cifs_within_open_ctime_change_is_not_accepted_as_historical_drift(self):
        with fixture() as f:
            original_verify, original_read = f.staging._verify_tree, os.read
            active = False
            altered = False

            def read(fd, amount):
                nonlocal altered
                data = original_read(fd, amount)
                if active and data and not altered:
                    target = Path(os.readlink(f"/proc/self/fd/{fd}"))
                    if target.name == "metadata":
                        altered = True
                        target.chmod(stat.S_IMODE(target.stat().st_mode))
                return data

            def verify(*args, **kwargs):
                nonlocal active
                active = kwargs["stage"]
                try:
                    return original_verify(*args, **kwargs)
                finally:
                    active = False

            with (
                patch.object(f.staging, "_verify_tree", side_effect=verify),
                patch.object(os, "read", side_effect=read),
            ):
                with self.assertRaisesRegex(ValueError, "ctime_ns"):
                    f.prepare()
            self.assertTrue(altered)
            self.assertTrue((f.candidate / "blobs/metadata").exists())

    def test_cleanup_does_not_delete_changed_bytes_with_restored_size_and_mtime(self):
        with fixture() as f:
            output = f.prepare()
            original = f.staging._verify_tree

            def alter_after_readback(*args, **kwargs):
                result = original(*args, **kwargs)
                path = output / "metadata"
                before = path.stat()
                path.write_bytes(b"x" * before.st_size)
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                return result

            with patch.object(
                f.staging, "_verify_tree", side_effect=alter_after_readback
            ):
                with self.assertRaisesRegex(ValueError, "replaced before cleanup"):
                    f.staging.cleanup(f.job)
            self.assertEqual(
                (output / "metadata").read_bytes(),
                b"x" * len(b"retained original blob"),
            )
            self.assertFalse(list(f.scratch.glob(".input-verified-*")))

    def test_changed_copy_totals_never_complete_or_release_source(self):
        for change in ("missing_bytes", "extra_bytes", "entry_drift"):
            with self.subTest(change=change), fixture() as f:
                real = staging.blob_summary

                def summary(*args, **kwargs):
                    result = real(*args, **kwargs)
                    if change == "missing_bytes":
                        result["logical_bytes"] += 1
                    elif change == "extra_bytes":
                        result["logical_bytes"] -= 1
                    else:
                        result["entries"] += 1
                    return result

                with patch.object(staging, "blob_summary", side_effect=summary):
                    with self.assertRaisesRegex(ValueError, "changed|grew"):
                        f.prepare()
                self.assertEqual(
                    (f.candidate / "blobs/metadata").read_bytes(),
                    b"retained original blob",
                )
                self.assertEqual(
                    f.snapshot["external_blob_staging"]["released_bytes"], 0
                )
                self.assertFalse(
                    any(
                        value.get("io_progress", {}).get("phase") == "blob_copy"
                        and value.get("io_progress", {}).get("complete")
                        for value in f.states
                    )
                )

    def test_metadata_is_cancellable_and_reports_its_own_entry_phase(self):
        class Cancelled(RuntimeError):
            pass

        with fixture() as f:

            def cancel():
                if f.job.get("stage") == "preparing_blob_staging":
                    raise Cancelled("fixture cancellation")

            with self.assertRaises(Cancelled):
                f.prepare(cancel)
            self.assertEqual(
                (f.candidate / "blobs/metadata").read_bytes(), b"retained original blob"
            )
            self.assertEqual(f.job["io_progress"]["phase"], "blob_metadata")
            self.assertEqual(f.job["io_progress"]["processed"], 0)
            self.assertEqual(
                f.snapshot["external_blob_staging"]["copied_bytes"],
                f.snapshot["external_blob_staging"]["logical_bytes"],
            )

    def test_cleanup_during_recovery_keeps_the_recovery_status_publisher(self):
        with fixture() as f:
            f.prepare()
            f.job["status"] = "rolling_back"
            calls = []

            def recovery(job, stage, message, **detail):
                calls.append(stage)
                job.update(status="rolling_back", stage=stage, **detail)
                f.manager._write(f.manager._job_path(job["job_id"]), job)

            f.manager._recovery_progress = recovery
            with patch.object(
                f.manager,
                "_progress",
                side_effect=AssertionError(
                    "normal publisher must not run during recovery"
                ),
            ):
                f.staging.cleanup(f.job)
            self.assertTrue(calls)
            self.assertEqual(f.job["status"], "rolling_back")
            self.assertIn("cleaning_blob_staging", calls)

    def test_verification_and_copy_metrics_are_distinct_and_complete(self):
        with fixture() as f:
            f.prepare()
            events = [
                value["io_progress"]
                for value in f.states
                if isinstance(value.get("io_progress"), dict)
            ]
            completed = {value["phase"]: value for value in events if value["complete"]}
            for phase in (
                "blob_copy",
                "blob_metadata",
                "blob_verify_staged",
                "blob_verify_source",
                "blob_release",
            ):
                self.assertIn(phase, completed)
                self.assertEqual(
                    completed[phase]["processed"], completed[phase]["total"]
                )
            self.assertNotEqual(
                completed["blob_copy"]["phase_id"],
                completed["blob_verify_staged"]["phase_id"],
            )


class MetadataAndDiagnosticTests(unittest.TestCase):
    def test_shared_readable_cifs_permissions_issue_no_owner_or_mode_rpc(self):
        value = SimpleNamespace(st_uid=1001, st_gid=1001, st_mode=stat.S_IFREG | 0o750)
        with (
            patch.object(os, "fstat", return_value=value),
            patch.object(os, "fchown") as owner,
            patch.object(os, "fchmod") as mode,
        ):
            staging._prepare_metadata(3, 1001, 1001, allowed=True, staged_cifs=True)
        owner.assert_not_called()
        mode.assert_not_called()

    def test_private_destinations_enforce_mode_but_skip_actual_noops(self):
        value = SimpleNamespace(st_uid=1001, st_gid=1001, st_mode=stat.S_IFREG | 0o750)
        prepared = SimpleNamespace(
            st_uid=1001, st_gid=1001, st_mode=stat.S_IFREG | 0o600
        )
        with (
            patch.object(os, "fstat", side_effect=[value, prepared]),
            patch.object(os, "fchown") as owner,
            patch.object(os, "fchmod") as mode,
        ):
            staging._prepare_metadata(3, 1001, 1001, allowed=False, staged_cifs=True)
        owner.assert_not_called()
        mode.assert_called_once_with(3, 0o600)
        with (
            patch.object(os, "fstat", return_value=value),
            patch.object(os, "fchown"),
            patch.object(os, "fchmod"),
        ):
            with self.assertRaisesRegex(ValueError, "Private staged input"):
                staging._prepare_metadata(
                    3, 1001, 1001, allowed=False, staged_cifs=True
                )
        with (
            patch.object(os, "fstat", return_value=prepared),
            patch.object(os, "fchown") as owner,
            patch.object(os, "fchmod") as mode,
        ):
            staging._prepare_metadata(3, 1001, 1001, allowed=False, staged_cifs=False)
        owner.assert_not_called()
        mode.assert_not_called()

    def test_directory_access_requires_traversal_and_diagnostics_exclude_paths(self):
        value = SimpleNamespace(st_uid=1001, st_gid=1001, st_mode=stat.S_IFDIR | 0o400)
        self.assertFalse(staging._readable(value, 1001, 1001))
        relative = "private-example/file"
        evidence = input_change(
            relative, stat.S_IFREG | 0o600, staged=True, changed_fields=["ctime_ns"]
        )
        self.assertEqual(
            evidence["path_digest"], hashlib.sha256(os.fsencode(relative)).hexdigest()
        )
        self.assertNotIn(
            relative,
            json.dumps(public_input_integrity(dict(evidence, private="secret"))),
        )
        for bad in (
            None,
            [],
            {"code": []},
            dict(evidence, changed_fields=["private-value"]),
        ):
            self.assertIsNone(public_input_integrity(bad))
