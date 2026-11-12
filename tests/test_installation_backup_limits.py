"""Capacity-driven installation admission and bounded untrusted verification."""

from contextlib import contextmanager, closing
import io
import os
from pathlib import Path
import stat
import tarfile
import unittest
from unittest.mock import patch

from tests import test_installation_backup as fixtures
from utils import installation_backup_archive as archive
from utils import installation_backup_inventory as census
from utils import installation_backup_limits as limits
from utils import infinidysk_v2_inventory as native_inventory
from utils.installation_backup import InstallationBackupManager


class InstallationResourceTests(unittest.TestCase):
    setUp = fixtures.ArchiveTests.setUp
    archive = fixtures.ArchiveTests.archive

    def add_files(self, count=8):
        for index in range(count):
            (self.source / f"file-{index:04d}").write_bytes(b"fixture bytes")

    def test_installation_roundtrip_is_independent_of_former_shared_migration_caps(
        self,
    ):
        self.add_files()
        with (
            patch.object(native_inventory, "MAX_TREE_ENTRIES", 2),
            patch.object(native_inventory, "MAX_TREE_MANIFEST_BYTES", 1024),
        ):
            raw = self.archive()
            proof = archive.inspect_archive(io.BytesIO(raw), self.scratch)
            self.assertEqual(proof["entries"], 9)
            target = self.root / "stage"
            target.mkdir()
            archive.inspect_archive(io.BytesIO(raw), self.scratch, extract_root=target)
            self.assertEqual(
                (target / "root_000/file-0000").read_bytes(), b"fixture bytes"
            )
            with self.assertRaises(native_inventory.InventoryError):
                list(native_inventory.iter_tree(self.source))
        self.assertEqual(native_inventory.MAX_TREE_ENTRIES, 2_000_000)
        self.assertIsNone(limits.installation_limits()["aggregate_entry_limit"])
        self.assertEqual(
            limits.installation_limits()["ledger_allocation"], "available_local_space"
        )
        self.assertEqual(proof["metadata"]["schema_version"], 1)
        self.assertGreater(proof["ledger_bytes"], 0)

    def test_sqlite_reads_back_quota_without_preallocating_it(self):
        usage = archive.shutil.disk_usage(self.scratch)
        with patch.object(
            archive.shutil,
            "disk_usage",
            return_value=usage._replace(free=128 * 1024**4),
        ):
            with archive.manifest(self.scratch, max_bytes=24 * 1024**4) as database:
                self.assertEqual(
                    database.execute("PRAGMA page_size").fetchone()[0], 4096
                )
                self.assertEqual(
                    database.execute("PRAGMA cache_size").fetchone()[0], -2048
                )
                self.assertEqual(database.execute("PRAGMA temp_store").fetchone()[0], 2)
                accepted = (
                    database.execute("PRAGMA max_page_count").fetchone()[0] * 4096
                )
                self.assertEqual(archive.ledger_quota(database), accepted)
                self.assertLessEqual(accepted, 24 * 1024**4)
                self.assertGreater(accepted, 8 * 1024**3)
                self.assertLess(archive.ledger_footprint(database), 1024**2)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_actual_sqlite_quota_failure_is_bounded_and_cleans_up(self):
        with self.assertRaisesRegex(
            archive.InstallationArchiveError, "ledger.*safe local workspace"
        ):
            with archive.manifest(self.scratch, max_bytes=8192) as database:
                row = {
                    "name": "roots/root_000/" + "a" * 3000,
                    "mode": stat.S_IFREG | 0o600,
                    "uid": os.geteuid(),
                    "gid": os.getegid(),
                    "mtime_ns": 0,
                    "size": 0,
                }
                archive._insert(
                    database,
                    row,
                    self.source / ("b" * 3000),
                    [1, 2, 3],
                    reserve_hash=True,
                )
                archive.ledger_footprint(database)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_capacity_proves_placeholder_ledger_and_estimates_complete_encoded_archive(
        self,
    ):
        (self.source / "café").write_bytes(b"payload with padding")
        (self.source / os.fsdecode(b"raw-\xff")).write_bytes(b"raw byte name")
        os.symlink("/reserved-example/media.mkv", self.source / "media.mkv")
        report, _details = census.capacity(self.roots, self.scratch, self.root)
        self.assertTrue(report["verification"]["ledger_verified"])
        self.assertGreater(
            report["verification"]["ledger_quota_bytes"],
            report["verification"]["ledger_bytes"],
        )
        observed = []
        original = archive.ledger_footprint

        def measured(database):
            amount = original(database)
            observed.append(amount)
            return amount

        with patch.object(archive, "ledger_footprint", measured):
            raw = self.archive()
        self.assertEqual(observed[0], report["verification"]["ledger_bytes"])
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as handle:
            footer = handle.getmember(archive.FOOTER)
            self.assertEqual(footer.size, report["verification"]["footer_bytes"])
        self.assertGreaterEqual(report["source"]["estimated_archive_bytes"], len(raw))
        self.assertEqual(report["verification"]["paired_workspace_bytes"], 1024**3)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_census_quota_failure_precedes_archive_and_cleans_up(self):
        for index in range(16):
            (self.source / (f"{index:03d}-" + "a" * 220)).write_bytes(b"fixture")
        with patch.object(census, "safe_ledger_budget", return_value=8192):
            with self.assertRaisesRegex(archive.InstallationArchiveError, "ledger"):
                census.capacity(self.roots, self.scratch, self.root)
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual(len(list(self.source.iterdir())), 16)

    def test_failed_source_census_closes_generator_and_removes_ledger(self):
        closed = []
        original = archive.source_rows

        def interrupted(*args, **kwargs):
            try:
                with closing(original(*args, **kwargs)) as rows:
                    yield next(rows)
                raise archive.InstallationArchiveError("fixture incomplete census")
            finally:
                closed.append(True)

        with patch.object(census, "source_rows", interrupted):
            with self.assertRaisesRegex(
                archive.InstallationArchiveError, "incomplete census"
            ):
                census.capacity(self.roots, self.scratch, self.root)
        self.assertEqual(closed, [True])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_small_installation_retains_previous_workspace_floor(self):
        self.add_files(2)
        usage = archive.shutil.disk_usage(self.scratch)
        with patch.object(
            archive.shutil,
            "disk_usage",
            return_value=usage._replace(
                total=4 * 1024**3, used=2 * 1024**3, free=2 * 1024**3
            ),
        ):
            report, _details = census.capacity(self.roots, self.scratch, self.root)
        self.assertEqual(report["status"], "sufficient")
        self.assertEqual(report["verification"]["paired_workspace_bytes"], 1024**3)
        self.assertLess(report["filesystems"][0]["required_bytes"], 2 * 1024**3)
        self.assertGreater(
            limits.safe_ledger_budget(128 * 1024**3, paired=True), 8 * 1024**3
        )

    def test_low_headroom_is_reported_before_allocating_a_census_ledger(self):
        usage = archive.shutil.disk_usage(self.scratch)
        with (
            patch.object(
                archive.shutil, "disk_usage", return_value=usage._replace(free=1024)
            ),
            patch.object(
                census,
                "manifest",
                side_effect=AssertionError("No low-space ledger allocation"),
            ),
        ):
            report, details = census.capacity(self.roots, self.scratch, self.root)
        self.assertEqual(report["status"], "insufficient")
        self.assertFalse(report["complete"])
        self.assertIsNone(report["source"])
        self.assertEqual(details, [])
        self.assertFalse(report["filesystems"][0]["sufficient"])

    def test_restore_accounts_measured_pair_once_on_real_filesystem(self):
        second = self.root / "second-target"
        second.mkdir()
        roots = self.roots + [
            {"id": "root_001", "path": str(second), "kind": "directory"}
        ]
        manager = object.__new__(InstallationBackupManager)
        manager.clock = lambda: 1.0
        proof = {
            "entries": 2,
            "logical_bytes": 4,
            "root_sizes": {"root_000": 2, "root_001": 2},
            "ledger_bytes": 8192,
        }
        report = manager._restore_capacity(proof, roots, self.scratch)
        self.assertEqual(len(report["filesystems"]), 1)
        self.assertEqual(report["filesystems"][0]["base_required_bytes"], 1024**3 + 8)
        proof["ledger_bytes"] = 2 * 1024**3
        report = manager._restore_capacity(proof, roots, self.scratch)
        self.assertEqual(
            report["filesystems"][0]["base_required_bytes"], 4 * 1024**3 + 8
        )
        proof.pop("ledger_bytes")
        report = manager._restore_capacity(proof, roots, self.scratch)
        self.assertEqual(report["filesystems"][0]["base_required_bytes"], 1024**3 + 8)

    def test_footer_rejects_declared_size_beyond_current_verification_quota(self):
        (self.source / "file").write_bytes(b"fixture")
        raw = self.archive()
        malicious = io.BytesIO()
        with (
            tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as original,
            tarfile.open(fileobj=malicious, mode="w:") as output,
        ):
            for member in original:
                if member.name == archive.FOOTER:
                    member.size = 8193
                    output.addfile(member, io.BytesIO(b" " * member.size))
                else:
                    output.addfile(
                        member,
                        original.extractfile(member) if member.isfile() else None,
                    )
        real_manifest = archive.manifest

        @contextmanager
        def tiny_quota(scratch, **_options):
            with real_manifest(scratch, max_bytes=8192) as database:
                yield database

        with patch.object(archive, "manifest", tiny_quota):
            with self.assertRaisesRegex(
                archive.InstallationArchiveError,
                "manifest exceeds the verification space",
            ):
                archive.inspect_archive(io.BytesIO(malicious.getvalue()), self.scratch)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_structural_record_bound_remains_independent_of_aggregate_capacity(self):
        row = {
            "name": "roots/root_000/link",
            "mode": stat.S_IFLNK | 0o777,
            "uid": 0,
            "gid": 0,
            "mtime_ns": 0,
            "size": 0,
            "link": os.fsdecode(b"\xff" * 12000),
        }
        with self.assertRaisesRegex(archive.InstallationArchiveError, "record exceeds"):
            archive.footer_record_bytes(row)
        self.assertEqual(limits.MAX_INSTALLATION_RECORD_BYTES, 65536)
        self.assertEqual(limits.MAX_INSTALLATION_DEPTH, 128)
        self.assertEqual(limits.MAX_INSTALLATION_ROOTS, 256)


if __name__ == "__main__":
    unittest.main()
