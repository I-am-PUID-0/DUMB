"""Real archive recovery preserves data and refuses unowned destinations."""

import copy
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from utils.backup_storage import (
    BackupStorage,
    BackupStorageError,
    parse_backup_reference,
)
from utils.infinidysk_v2_archive import ArchiveError, create_retained_archive
from utils.infinidysk_v2_inventory import tree_fingerprint
from utils.infinidysk_v2_space import restore_archive_tree
from utils import infinidysk_v2_space as space_module


def identity(path):
    value = path.lstat()
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "mode": stat.S_IFMT(value.st_mode),
    }


class ArchiveSpaceRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dumb-space-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "v1-data"
        self.runtime = self.root / "v1-runtime"
        self.scratch = self.root / ("b" * 32)
        self.destination = self.root / "mounted"
        for path in (self.source, self.runtime, self.scratch, self.destination):
            path.mkdir(mode=0o700)
        (self.source / "blobs").mkdir(mode=0o750)
        (self.source / "blobs" / "caf\u00e9_\udcff").write_bytes(
            b"retained compressed metadata"
        )
        (self.source / "blobs" / "caf\u00e9_\udcff").chmod(0o640)
        os.utime(self.source / "blobs" / "caf\u00e9_\udcff", ns=(-1, -1))
        (self.source / "empty").mkdir(mode=0o711)
        (self.source / "relative").symlink_to("blobs/caf\u00e9_\udcff")
        (self.source / "db.sqlite").write_bytes(b"retained original db bytes")
        (self.runtime / "backend.dll").write_bytes(b"retained runtime")
        config_backup = self.scratch / "config.json"
        config_backup.write_text('{"example":true}')
        config_backup.chmod(0o600)
        self.config = SimpleNamespace(
            config={"backup_storage": {"profiles": {}, "default_destination_id": ""}},
            file_path=str(self.root / "dumb-config.json"),
            save_config=Mock(),
        )
        self.storage = BackupStorage(
            self.config, state_root=self.root / "storage-control"
        )
        self.storage.save_profile(
            {
                "id": "nas",
                "name": "Mounted archives",
                "path": str(self.destination),
                "enabled": True,
            }
        )
        self.plan = {
            "source": {
                "data_dir": str(self.source),
                "runtime_dir": str(self.runtime),
                "data_identity": identity(self.source),
                "runtime_identity": {"directory": identity(self.runtime)},
            },
            "backup_storage": {"destination_id": "nas"},
        }
        fingerprints = {
            str(path): tree_fingerprint(path, scratch_root=self.scratch)
            for path in (self.source, self.runtime)
        }
        result = create_retained_archive(
            self.plan,
            {"root": str(self.scratch), "config_backup": str(config_backup)},
            self.storage,
            fingerprints,
        )
        self.snapshot = {
            "source_snapshot": str(self.scratch),
            "archive_roots": result["archive_roots"],
            "cold_archive": result["receipt"],
            "original_fingerprints": fingerprints,
        }
        self.candidate = self.root / "candidate"
        self.source.rename(self.candidate)
        self.source.mkdir(mode=0o700)
        self.owned = identity(self.source)

    def restore(self, **overrides):
        options = {
            "source": str(self.source),
            "destination": self.source,
            "expected_identity": self.owned,
        }
        options.update(overrides)
        return restore_archive_tree(self.snapshot, self.plan, self.storage, **options)

    def archive_path(self):
        profile, namespace, artifact = parse_backup_reference(
            self.snapshot["cold_archive"]["reference"]
        )
        return (
            self.destination
            / ".dumb-backups"
            / profile
            / namespace
            / (artifact + ".tar")
        )

    def test_real_roundtrip_restores_original_content_links_metadata_and_ownership(
        self,
    ):
        check = Mock()
        proof = self.restore(owner_check=check)
        self.assertTrue(proof["restored"])
        self.assertEqual(
            self.snapshot["original_fingerprints"][str(self.source)],
            proof["fingerprint"],
        )
        self.assertEqual(
            b"retained compressed metadata", (self.source / "relative").read_bytes()
        )
        self.assertEqual(
            "blobs/caf\u00e9_\udcff", os.readlink(self.source / "relative")
        )
        for relative in (
            "blobs",
            "blobs/caf\u00e9_\udcff",
            "relative",
            "empty",
            "db.sqlite",
        ):
            before, after = (self.candidate / relative).lstat(), (
                self.source / relative
            ).lstat()
            self.assertEqual(
                (before.st_mode, before.st_mtime_ns, before.st_uid, before.st_gid),
                (after.st_mode, after.st_mtime_ns, after.st_uid, after.st_gid),
            )
        self.assertGreater(check.call_count, 5)
        self.assertFalse(list(self.scratch.glob(".archive-manifest-*")))
        self.assertFalse(list(self.scratch.glob(".inventory-*")))
        self.assertEqual(
            b"retained runtime", (self.runtime / "backend.dll").read_bytes()
        )

    def test_private_candidate_restore_can_be_published_only_after_verification(self):
        self.plan["target"] = {"data_dir": str(self.root / "v2-data")}
        restored = self.root / (".v2-data.dumb-v2-" + self.scratch.name)
        self.source.rmdir()
        restored.mkdir(mode=0o700)
        proof = self.restore(destination=restored, expected_identity=identity(restored))
        self.assertTrue(proof["restored"])
        self.assertFalse(self.source.exists())
        self.assertEqual(
            b"retained original db bytes", (restored / "db.sqlite").read_bytes()
        )
        restored.rename(self.source)
        self.assertEqual(
            self.snapshot["original_fingerprints"][str(self.source)],
            tree_fingerprint(self.source, scratch_root=self.scratch),
        )

    def test_candidate_path_must_match_target_and_exact_private_job_identity(self):
        self.plan["target"] = {"data_dir": str(self.root / "v2-data")}
        for destination in (
            self.root / ".v2-data.dumb-v2-wrong",
            self.root / (".different.dumb-v2-" + self.scratch.name),
        ):
            destination.mkdir(mode=0o700)
            with self.subTest(destination=destination), self.assertRaises(ArchiveError):
                self.restore(
                    destination=destination, expected_identity=identity(destination)
                )
            self.assertEqual([], list(destination.iterdir()))

    def test_replaced_child_directory_cannot_redirect_writes_outside_owned_root(self):
        replaced = False

        def replace_child():
            nonlocal replaced
            child = self.source / "blobs"
            if not replaced and child.is_dir():
                replaced = True
                child.rename(self.source / "moved-blobs")
                child.symlink_to(self.runtime, target_is_directory=True)

        with self.assertRaises((ArchiveError, OSError)):
            self.restore(owner_check=replace_child)
        self.assertTrue(replaced)
        self.assertEqual(
            ["backend.dll"], [path.name for path in self.runtime.iterdir()]
        )

    def test_nonempty_destination_is_never_adopted_or_overwritten(self):
        (self.source / "unrelated").write_bytes(b"protected")
        with self.assertRaisesRegex(ArchiveError, "empty"):
            self.restore()
        self.assertEqual(b"protected", (self.source / "unrelated").read_bytes())

    def test_unknown_source_arbitrary_destination_and_unrecorded_identity_refuse(self):
        cases = [
            {"source": str(self.root / "unknown")},
            {"destination": self.root / "unrelated"},
            {"expected_identity": {}},
            {"expected_identity": {**self.owned, "inode": True}},
        ]
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ArchiveError):
                self.restore(**options)
        self.assertEqual([], list(self.source.iterdir()))

    def test_replaced_destination_symlink_or_mounted_root_refuses_before_writes(self):
        with self.assertRaisesRegex(ArchiveError, "changed"):
            self.restore(
                expected_identity={**self.owned, "inode": self.owned["inode"] + 1}
            )
        with patch.object(
            space_module,
            "_mount_id",
            side_effect=["root-mount", "root-mount", "foreign-parent"],
        ):
            with self.assertRaisesRegex(ArchiveError, "mounted"):
                self.restore()
        self.source.rmdir()
        self.source.symlink_to(self.runtime, target_is_directory=True)
        with self.assertRaises(OSError):
            self.restore()
        self.assertEqual(
            ["backend.dll"], [path.name for path in self.runtime.iterdir()]
        )

    def test_missing_or_corrupt_archive_leaves_empty_owned_root(self):
        path = self.archive_path()
        original = path.read_bytes()
        path.unlink()
        with self.assertRaises((ArchiveError, BackupStorageError, FileNotFoundError)):
            self.restore()
        self.assertEqual([], list(self.source.iterdir()))
        path.write_bytes(original)
        path.chmod(0o600)
        with path.open("r+b") as handle:
            handle.seek(1024)
            handle.write(b"corrupt archive payload")
        with self.assertRaises((ArchiveError, BackupStorageError)):
            self.restore()
        self.assertEqual([], list(self.source.iterdir()))

    def test_missing_external_destination_never_creates_local_fallback(self):
        self.destination.rename(self.root / "removed-mount")
        with self.assertRaises(BackupStorageError):
            self.restore()
        self.assertFalse(self.destination.exists())
        self.assertEqual([], list(self.source.iterdir()))

    def test_ownership_loss_retains_partial_root_without_automatic_removal(self):
        calls = 0

        def lose_owner():
            nonlocal calls
            calls += 1
            if calls == 6:
                raise ArchiveError("worker ownership lost")

        with self.assertRaisesRegex(ArchiveError, "ownership lost"):
            self.restore(owner_check=lose_owner)
        self.assertEqual(self.owned, identity(self.source))
        self.assertTrue(self.source.is_dir())
        self.assertEqual(
            b"retained compressed metadata", (self.candidate / "relative").read_bytes()
        )
        self.assertFalse(list(self.scratch.glob(".archive-manifest-*")))

    def test_archive_change_after_initial_verification_refuses_second_open(self):
        original_verify = space_module.verify_retained_archive

        def change_after_verify(*args):
            proof = original_verify(*args)
            with self.archive_path().open("r+b") as handle:
                handle.seek(1024)
                handle.write(b"changed after readback")
            return proof

        with patch.object(
            space_module, "verify_retained_archive", side_effect=change_after_verify
        ):
            with self.assertRaises((ArchiveError, BackupStorageError)):
                self.restore()
        self.assertEqual([], list(self.source.iterdir()))

    def test_same_size_receipt_tampering_fails_full_hash_readback(self):
        self.snapshot = copy.deepcopy(self.snapshot)
        self.snapshot["cold_archive"]["sha256"] = hashlib.sha256(b"wrong").hexdigest()
        with self.assertRaisesRegex(ArchiveError, "differ"):
            self.restore()
        self.assertEqual([], list(self.source.iterdir()))


if __name__ == "__main__":
    unittest.main()
