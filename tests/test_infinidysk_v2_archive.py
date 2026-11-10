"""Portable cold archives use bounded local manifests and complete readback."""

import io
import hashlib
import os
from pathlib import Path
import stat
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from utils.backup_storage import (
    BackupStorage,
    BackupStorageError,
    parse_backup_reference,
)
from utils import infinidysk_v2_archive as archive_module
from utils.infinidysk_v2_archive import (
    ArchiveError,
    create_retained_archive,
    verify_retained_archive,
)
from utils.infinidysk_v2_inventory import tree_fingerprint


def identity(path):
    value = path.lstat()
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "mode": stat.S_IFMT(value.st_mode),
    }


class ColdArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dumb-archive-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source, self.runtime = self.root / "v1-data", self.root / "v1-runtime"
        self.destination, self.scratch = self.root / "mounted", self.root / ("a" * 32)
        for path in (self.source, self.runtime, self.destination, self.scratch):
            path.mkdir(mode=0o700)
        (self.source / "db.sqlite").write_bytes(b"closed database fixture")
        (self.source / "content").mkdir()
        (self.source / "content" / "movie").write_bytes(b"blob payload")
        (self.source / "relative").symlink_to("content/movie")
        (self.runtime / "application.dll").write_bytes(b"legacy runtime")
        self.config = SimpleNamespace(
            config={"backup_storage": {"profiles": {}, "default_destination_id": ""}},
            file_path=str(self.root / "config.json"),
            save_config=Mock(),
        )
        self.storage = BackupStorage(
            self.config, state_root=self.root / "storage-control"
        )
        self.storage.save_profile(
            {
                "id": "nas",
                "name": "Cold archives",
                "path": str(self.destination),
                "enabled": True,
            }
        )
        config_backup = self.scratch / "dumb-config.json"
        config_backup.write_text('{"example":true}')
        config_backup.chmod(0o600)
        self.workspace = {
            "root": str(self.scratch),
            "config_backup": str(config_backup),
        }
        self.plan = {
            "source": {
                "data_dir": str(self.source),
                "runtime_dir": str(self.runtime),
                "data_identity": identity(self.source),
                "runtime_identity": {"directory": identity(self.runtime)},
            },
            "backup_storage": {"destination_id": "nas"},
        }

    def _fingerprints(self):
        return {
            str(path): tree_fingerprint(path, scratch_root=self.scratch)
            for path in dict.fromkeys((self.source, self.runtime))
        }

    def _archive(self, postgres_snapshot=None, **kwargs):
        fingerprints = self._fingerprints()
        result = create_retained_archive(
            self.plan, self.workspace, self.storage, fingerprints, **kwargs
        )
        snapshot = {
            "source_snapshot": str(self.scratch),
            "original_fingerprints": fingerprints,
            "archive_roots": result["archive_roots"],
            "cold_archive": result["receipt"],
        }
        if postgres_snapshot is not None:
            snapshot["postgres_snapshot"] = postgres_snapshot
        proof = verify_retained_archive(snapshot, self.plan, self.storage)
        self.assertTrue(proof["verified"])
        self.assertEqual(fingerprints, proof["original_fingerprints"])
        return snapshot

    def test_archived_postgres_dump_must_match_the_independent_restore_receipt(self):
        dump = self.scratch / "v1-postgres.dump"
        dump.write_bytes(b"independently verified PostgreSQL dump fixture")
        dump.chmod(0o600)
        self.plan["source"]["provider"] = "postgres"
        receipt = {
            "source_read_only": True,
            "verified_restore": True,
            "source_binding": {"binding_fingerprint": "c" * 64},
            "restore_proof": {"verified": True, "source_binding": "c" * 64},
            "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
        }
        snapshot = self._archive(
            postgres_snapshot=receipt, extra_files={"v1-postgres.dump": dump}
        )
        snapshot["postgres_snapshot"]["dump_sha256"] = "0" * 64
        with self.assertRaisesRegex(ArchiveError, "PostgreSQL"):
            verify_retained_archive(snapshot, self.plan, self.storage)
        snapshot["postgres_snapshot"]["dump_sha256"] = hashlib.sha256(
            dump.read_bytes()
        ).hexdigest()
        snapshot["postgres_snapshot"]["restore_proof"]["source_binding"] = "d" * 64
        with self.assertRaisesRegex(ArchiveError, "PostgreSQL"):
            verify_retained_archive(snapshot, self.plan, self.storage)

    def test_real_archive_roundtrip_preserves_blobs_runtime_links_and_receipt(self):
        snapshot = self._archive()
        receipt = snapshot["cold_archive"]
        self.assertTrue(receipt["verified"])
        self.assertEqual("infinidysk_v2_archive", receipt["artifact_kind"])
        self.assertEqual("tar", receipt["extension"])
        self.assertEqual(2, len(snapshot["archive_roots"]))
        with self.storage.open_artifact(receipt["reference"]) as handle:
            with tarfile.open(fileobj=handle, mode="r|") as archive:
                found = set()
                while (info := archive.next()) is not None:
                    found.add(info.name)
                    archive.members.clear()
                self.assertIn("trees/tree_0/content/movie", found)
                self.assertIn("trees/tree_1/application.dll", found)
                self.assertIn(archive_module.FOOTER, found)
        self.assertFalse(list(self.scratch.glob(".archive-manifest-*")))
        self.assertEqual(
            b"blob payload", (self.source / "content" / "movie").read_bytes()
        )

    def test_exact_negative_nanoseconds_unicode_surrogates_and_hardlinks_round_trip(
        self,
    ):
        unusual = self.source / "content" / "caf\u00e9_\udcff"
        unusual.write_bytes(b"unusual filename")
        os.utime(unusual, ns=(-1, -1))
        (self.source / "unusual-link").symlink_to("content/" + unusual.name)
        os.link(unusual, self.source / "hardlink-copy")
        snapshot = self._archive()
        with self.storage.open_artifact(
            snapshot["cold_archive"]["reference"]
        ) as handle:
            with tarfile.open(
                fileobj=handle, mode="r|", encoding="utf-8", errors="surrogateescape"
            ) as archive:
                matched = False
                while (info := archive.next()) is not None:
                    archive.members.clear()
                    self.assertFalse(info.islnk())
                    if info.name.endswith(unusual.name):
                        self.assertEqual("-1", info.pax_headers["DUMB.mtime_ns"])
                        self.assertEqual("-0.000000001", info.pax_headers["mtime"])
                        matched = True
                self.assertTrue(matched)

    def test_identical_data_and_runtime_roots_are_archived_once(self):
        self.runtime = self.source
        self.plan["source"].update(
            runtime_dir=str(self.source),
            runtime_identity={"directory": identity(self.source)},
        )
        snapshot = self._archive()
        self.assertEqual(1, len(snapshot["archive_roots"]))

    def test_wide_archive_bounds_both_tarfile_member_caches(self):
        for index in range(1024):
            (self.source / "content" / f"item-{index:04d}").touch()
        addfile, next_member = tarfile.TarFile.addfile, tarfile.TarFile.next
        maxima = {"write": 0, "read": 0}

        def add(archive, *args, **kwargs):
            result = addfile(archive, *args, **kwargs)
            maxima["write"] = max(maxima["write"], len(archive.members))
            return result

        def next_value(archive):
            result = next_member(archive)
            maxima["read"] = max(maxima["read"], len(archive.members))
            return result

        with (
            patch.object(tarfile.TarFile, "addfile", add),
            patch.object(tarfile.TarFile, "next", next_value),
        ):
            self._archive()
        self.assertLessEqual(maxima["write"], 1)
        self.assertLessEqual(maxima["read"], 1)

    def test_changed_source_refuses_publication_and_cleans_partial(self):
        fingerprints = self._fingerprints()

        def change(stage, *args, **kwargs):
            if stage == "archiving":
                (self.source / "content" / "movie").write_bytes(b"changed source")

        with self.assertRaises(ArchiveError):
            create_retained_archive(
                self.plan, self.workspace, self.storage, fingerprints, progress=change
            )
        self.assertFalse(list(self.destination.rglob("*.tar")))
        self.assertFalse(list(self.destination.rglob("*.partial")))

    def test_corrupt_closed_archive_is_rejected_again_on_open(self):
        snapshot = self._archive()
        profile, namespace, artifact = parse_backup_reference(
            snapshot["cold_archive"]["reference"]
        )
        path = (
            self.destination / ".dumb-backups" / profile / namespace / f"{artifact}.tar"
        )
        with path.open("r+b") as handle:
            handle.seek(1024)
            handle.write(b"corruption")
        with self.assertRaises(BackupStorageError):
            verify_retained_archive(snapshot, self.plan, self.storage)

    def test_nonzero_bytes_after_tar_end_marker_are_rejected(self):
        snapshot = self._archive()
        with self.storage.open_artifact(
            snapshot["cold_archive"]["reference"]
        ) as handle:
            data = io.BytesIO(handle.read() + b"unexpected trailing payload")
        with self.assertRaisesRegex(ArchiveError, "follow"):
            archive_module._verify_stream(
                data,
                self.scratch,
                snapshot["archive_roots"],
                snapshot["original_fingerprints"],
            )

    def test_portable_nanosecond_metadata_must_match_standard_pax_time(self):
        record = {
            "name": "trees/tree_0",
            "mode": stat.S_IFDIR | 0o700,
            "uid": os.geteuid(),
            "gid": os.getegid(),
            "mtime_ns": 0,
            "size": 0,
        }
        info = archive_module._header(record)
        info.pax_headers["mtime"] = "1.000000000"
        with self.assertRaisesRegex(ArchiveError, "timestamp"):
            archive_module._record_from_header(info)

    def test_sparse_pax_extensions_are_rejected_before_sparse_map_parsing(self):
        for version in ("0.0", "0.1", "1.0"):
            data = io.BytesIO()
            with tarfile.open(
                fileobj=data, mode="w", format=tarfile.PAX_FORMAT
            ) as archive:
                info = tarfile.TarInfo("trees/tree_0/sparse")
                info.size = 4
                info.pax_headers = {
                    "0.0": {
                        "GNU.sparse.size": str(2**60),
                        "GNU.sparse.offset": "0",
                        "GNU.sparse.numbytes": "1",
                    },
                    "0.1": {"GNU.sparse.map": "0,1", "GNU.sparse.size": str(2**60)},
                    "1.0": {
                        "GNU.sparse.major": "1",
                        "GNU.sparse.minor": "0",
                        "GNU.sparse.realsize": str(2**60),
                    },
                }[version]
                archive.addfile(info, io.BytesIO(b"0\nxx"))
            data.seek(0)
            with self.subTest(version=version), self.assertRaises(ArchiveError):
                archive_module._verify_stream(
                    data,
                    self.scratch,
                    {str(self.source): "tree_0"},
                    {str(self.source): "unused"},
                )

    def test_duplicate_escaping_and_special_members_are_rejected(self):
        base = {
            "name": "trees/tree_0",
            "mode": stat.S_IFDIR | 0o700,
            "uid": os.geteuid(),
            "gid": os.getegid(),
            "mtime_ns": 0,
            "size": 0,
        }
        for scenario in ("duplicate", "escape", "special"):
            data = io.BytesIO()
            with tarfile.open(
                fileobj=data, mode="w", format=tarfile.PAX_FORMAT
            ) as archive:
                info = archive_module._header(base)
                if scenario == "escape":
                    info.name = "../outside"
                if scenario == "special":
                    info.type = tarfile.FIFOTYPE
                archive.addfile(info)
                if scenario == "duplicate":
                    archive.addfile(archive_module._header(base))
            data.seek(0)
            with self.subTest(scenario=scenario), self.assertRaises(ArchiveError):
                archive_module._verify_stream(
                    data,
                    self.scratch,
                    {str(self.source): "tree_0"},
                    {str(self.source): "unused"},
                )
            self.assertFalse((self.root / "outside").exists())


if __name__ == "__main__":
    unittest.main()
