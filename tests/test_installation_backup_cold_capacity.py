"""Reuse the cold census for admission without another source walk."""

import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_installation_backup as fixtures
from utils import installation_backup_archive as archive
from utils import installation_backup_inventory as census
from utils import installation_backup_limits as limits


class ColdCensusCapacityTests(unittest.TestCase):
    setUp = fixtures.ArchiveTests.setUp

    def write(self, output, callback, *, compression="none"):
        return archive.write_archive(
            output,
            self.metadata,
            self.roots,
            self.scratch,
            compression=compression,
            inventory_ready=callback,
        )

    def measurement(self):
        return {
            "entries": 2,
            "logical_bytes": 2048,
            "root_count": 1,
            "tar_bytes": 4096,
            "footer_bytes": 500,
            "ledger_bytes": 8192,
            "ledger_quota_bytes": 1024**2,
        }

    def test_census_callback_precedes_every_archive_byte_for_both_formats(self):
        payload = b"reserved application state" * 2048
        (self.source / "payload").write_bytes(payload)
        os.symlink("/reserved-example/library/movie.mkv", self.source / "movie.mkv")
        for compression in ("none", "zstd"):
            with self.subTest(compression=compression):
                output, seen = io.BytesIO(), []

                def ready(measurement):
                    self.assertEqual(output.getvalue(), b"")
                    self.assertEqual(len(list(self.scratch.iterdir())), 1)
                    self.assertEqual(measurement["entries"], 3)
                    self.assertEqual(measurement["logical_bytes"], len(payload))
                    self.assertGreater(measurement["ledger_bytes"], 0)
                    report = census.capacity_from_census(
                        measurement,
                        self.scratch,
                        self.root,
                        compression=compression,
                    )
                    self.assertTrue(report["from_census"])
                    self.assertEqual(report["status"], "sufficient")
                    seen.append(report)

                with patch.object(
                    census,
                    "source_rows",
                    side_effect=AssertionError("extra source walk"),
                ):
                    self.write(output, ready, compression=compression)
                self.assertEqual(len(seen), 1)
                raw = output.getvalue()
                self.assertLessEqual(
                    len(raw), seen[0]["source"]["estimated_archive_bytes"]
                )
                proof = archive.inspect_archive(io.BytesIO(raw), self.scratch)
                self.assertTrue(proof["verified"])
                self.assertEqual(proof["entries"], 3)
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_insufficient_destination_refuses_before_plain_or_compressed_output(self):
        (self.source / "payload").write_bytes(b"reserved state")
        usage = census.shutil.disk_usage(self.root)
        for compression in ("none", "zstd"):
            with self.subTest(compression=compression):
                output = io.BytesIO()

                def reject(measurement):
                    with patch.object(
                        census.shutil, "disk_usage", return_value=usage._replace(free=0)
                    ):
                        report = census.capacity_from_census(
                            measurement,
                            self.scratch,
                            self.root,
                            compression=compression,
                        )
                    self.assertEqual(report["status"], "insufficient")
                    raise archive.InstallationArchiveError(
                        "Insufficient destination space after the cold census."
                    )

                with self.assertRaisesRegex(
                    archive.InstallationArchiveError, "Insufficient destination"
                ):
                    self.write(output, reject, compression=compression)
                self.assertEqual(output.getvalue(), b"")
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_shared_filesystem_charges_only_unallocated_verification_space(self):
        measurement = self.measurement()
        paired = limits.paired_workspace_bytes(measurement["ledger_bytes"])
        remaining = paired - measurement["ledger_bytes"]
        stored = (
            measurement["tar_bytes"]
            + measurement["footer_bytes"]
            + limits.ARCHIVE_FIXED_OVERHEAD_BYTES
        )
        base = remaining + stored
        required = base + (base + 9) // 10
        usage = census.shutil.disk_usage(self.root)
        with patch.object(
            census.shutil, "disk_usage", return_value=usage._replace(free=required)
        ):
            report = census.capacity_from_census(measurement, self.scratch, self.root)
            unallocated = census.capacity_from_census(
                measurement, self.scratch, self.root, ledger_present=False
            )
        self.assertEqual(report["status"], "sufficient")
        self.assertEqual(unallocated["status"], "insufficient")
        self.assertEqual(len(report["filesystems"]), 1)
        group = report["filesystems"][0]
        self.assertEqual(group["roles"], ["local_verification", "cold_archive"])
        self.assertEqual(group["base_required_bytes"], base)
        self.assertEqual(group["required_bytes"], required)
        self.assertEqual(report["verification"]["paired_workspace_bytes"], paired)
        self.assertEqual(report["verification"]["remaining_workspace_bytes"], remaining)
        with patch.object(
            census.shutil,
            "disk_usage",
            return_value=usage._replace(free=required - 1),
        ):
            report = census.capacity_from_census(measurement, self.scratch, self.root)
        self.assertEqual(report["status"], "insufficient")
        self.assertEqual(report["filesystems"][0]["shortfall_bytes"], 1)

    def test_distinct_filesystems_apply_their_own_space_requirements(self):
        measurement = self.measurement()
        destination = self.root / "destination"
        destination.mkdir()
        original_stat = Path.stat
        local = (
            limits.paired_workspace_bytes(measurement["ledger_bytes"])
            - measurement["ledger_bytes"]
        )
        stored = (
            measurement["tar_bytes"]
            + measurement["footer_bytes"]
            + limits.ARCHIVE_FIXED_OVERHEAD_BYTES
        )
        usage = census.shutil.disk_usage(self.root)

        def devices(path, *args, **kwargs):
            value = original_stat(path, *args, **kwargs)
            if path == destination:
                values = list(value)
                values[2] += 1
                return os.stat_result(values)
            return value

        def space(path):
            base = stored if path == destination else local
            return usage._replace(free=base + (base + 9) // 10)

        with (
            patch.object(Path, "stat", devices),
            patch.object(census.shutil, "disk_usage", side_effect=space),
        ):
            report = census.capacity_from_census(measurement, self.scratch, destination)
        self.assertEqual(report["status"], "sufficient")
        self.assertEqual(len(report["filesystems"]), 2)
        self.assertEqual(
            [row["base_required_bytes"] for row in report["filesystems"]],
            [local, stored],
        )

    def test_cold_growth_replaces_the_warm_estimate_and_matches_preview_formula(self):
        path = self.source / "payload"
        path.write_bytes(b"small")
        warm, _ = census.capacity(self.roots, self.scratch, self.root)
        path.write_bytes(b"reserved" * (1024**2))
        (self.source / "additional").write_bytes(b"new persistent state")
        output, seen = io.BytesIO(), []

        def ready(measurement):
            self.assertEqual(output.getvalue(), b"")
            seen.append(
                census.capacity_from_census(measurement, self.scratch, self.root)
            )

        self.write(output, ready)
        cold = seen[0]
        preview, _ = census.capacity(self.roots, self.scratch, self.root)
        self.assertGreater(
            cold["source"]["estimated_archive_bytes"],
            warm["source"]["estimated_archive_bytes"],
        )
        self.assertGreater(
            cold["source"]["logical_bytes"], warm["source"]["logical_bytes"]
        )
        self.assertEqual(cold["source"], preview["source"])
        self.assertGreater(
            cold["source"]["estimated_archive_bytes"], len(output.getvalue())
        )
        self.assertTrue(
            archive.inspect_archive(io.BytesIO(output.getvalue()), self.scratch)[
                "verified"
            ]
        )

    def test_invalid_measurements_are_not_capacity_proofs(self):
        for field in (
            "entries",
            "logical_bytes",
            "tar_bytes",
            "footer_bytes",
            "ledger_bytes",
        ):
            for value in (-1, True, "100"):
                with self.subTest(field=field, value=value):
                    measurement = self.measurement()
                    measurement[field] = value
                    with self.assertRaises(
                        (archive.InstallationArchiveError, ValueError)
                    ):
                        census.capacity_from_census(
                            measurement, self.scratch, self.root
                        )


if __name__ == "__main__":
    unittest.main()
