import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from utils import infinidysk_v2_inventory as inventory


def legacy_fingerprint(root):
    """Independent reference for the hash already stored by released migration jobs."""
    rows = []
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in sorted(folders + files):
            item = Path(directory) / name
            value = item.lstat()
            row = {
                "path": str(item.relative_to(root)),
                "mode": value.st_mode,
                "mtime_ns": value.st_mtime_ns,
            }
            if not stat.S_ISDIR(value.st_mode):
                row["size"] = value.st_size
            if stat.S_ISLNK(value.st_mode):
                row["link"] = os.readlink(item)
            elif stat.S_ISREG(value.st_mode):
                row["sha256"] = hashlib.sha256(item.read_bytes()).hexdigest()
            rows.append(row)
    encoded = json.dumps(
        sorted(rows, key=lambda row: row["path"]),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class InfiniDyskV2InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()

    def assert_resources_closed(self):
        self.assertEqual(list(self.scratch.iterdir()), [])
        owned = []
        for descriptor in Path("/proc/self/fd").iterdir():
            try:
                target = os.readlink(descriptor)
            except FileNotFoundError:
                continue
            if target.startswith(str(self.root)):
                owned.append(target)
        self.assertEqual(owned, [])

    def fingerprint(self, **options):
        return inventory.tree_fingerprint(
            self.source, scratch_root=self.scratch, **options
        )

    def test_exact_legacy_hash_for_global_path_order_links_and_unix_names(self):
        (self.source / "a").mkdir()
        (self.source / "a" / "z").write_bytes(b"nested")
        for name in ("a-", "a.txt", "é", "\ue000", "😀", os.fsdecode(b"raw-\xff")):
            (self.source / name).write_bytes(name.encode("utf-8", "surrogatepass"))
        (self.source / "relative").symlink_to("a/z")
        self.assertEqual(legacy_fingerprint(self.source), self.fingerprint())
        self.assert_resources_closed()

    def test_empty_and_missing_sources_keep_legacy_empty_hash(self):
        expected = hashlib.sha256(b"[]").hexdigest()
        self.assertEqual(expected, self.fingerprint())
        self.assertEqual(
            expected,
            inventory.tree_fingerprint(
                self.source / "missing", scratch_root=self.scratch
            ),
        )
        self.assert_resources_closed()

    def test_metadata_summary_preserves_every_path_without_content_or_manifest(self):
        (self.source / "blobs").mkdir()
        (self.source / "blobs" / "one").write_bytes(b"abc")
        (self.source / "node_modules").mkdir()
        (self.source / "node_modules" / "two").write_bytes(b"de")
        (self.source / "relative").symlink_to("blobs/one")
        with (
            patch.object(inventory, "_content_hash") as reading,
            patch.object(inventory.tempfile, "TemporaryDirectory") as manifest,
        ):
            summary = inventory.tree_summary(self.source, validate_links=True)
        self.assertEqual({"entries": 5, "bytes": 5}, summary)
        reading.assert_not_called()
        manifest.assert_not_called()
        self.assert_resources_closed()

    def test_wide_scan_stops_at_first_excess_entry_without_materializing(self):
        for index in range(2):
            (self.source / f"item-{index}").touch()
        consumed = []

        def entries(_descriptor):
            for index in range(100_000):
                consumed.append(index)
                yield SimpleNamespace(name=f"item-{index}")

        with (
            patch.object(inventory, "MAX_TREE_ENTRIES", 2),
            patch.object(inventory.os, "scandir", entries),
        ):
            with self.assertRaisesRegex(
                inventory.InventoryError, "observed at least 3"
            ):
                inventory.tree_summary(self.source)
        self.assertEqual([0, 1, 2], consumed)
        self.assert_resources_closed()

    def test_depth_bound_refuses_before_opening_deeper_directory(self):
        (self.source / "one" / "two" / "three").mkdir(parents=True)
        with patch.object(inventory, "MAX_TREE_DEPTH", 2):
            with self.assertRaisesRegex(inventory.InventoryError, "directory depth"):
                inventory.tree_summary(self.source)
        self.assert_resources_closed()
        with patch.object(inventory, "MAX_TREE_DEPTH", 3):
            self.assertEqual(3, inventory.tree_summary(self.source)["entries"])
        self.assert_resources_closed()

    def test_unreadable_directory_refuses_instead_of_omitting_subtree(self):
        child = self.source / "private"
        child.mkdir()
        (child / "retained").write_bytes(b"original")
        child_inode = child.stat().st_ino
        original_scandir = os.scandir

        def inaccessible(descriptor):
            if os.fstat(descriptor).st_ino == child_inode:
                raise PermissionError("not an empty tree")
            return original_scandir(descriptor)

        with patch.object(inventory.os, "scandir", inaccessible):
            with self.assertRaisesRegex(inventory.InventoryError, "read completely"):
                inventory.tree_summary(self.source)
        self.assert_resources_closed()

    def test_iteration_failure_closes_directory_and_never_returns_partial_digest(self):
        (self.source / "first").touch()

        original_scandir = os.scandir
        source_inode = self.source.stat().st_ino

        def failing(descriptor):
            if os.fstat(descriptor).st_ino != source_inode:
                return original_scandir(descriptor)

            def interrupted():
                yield SimpleNamespace(name="first")
                raise OSError("interrupted enumeration")

            return interrupted()

        with patch.object(inventory.os, "scandir", failing):
            with self.assertRaisesRegex(inventory.InventoryError, "read completely"):
                self.fingerprint()
        self.assert_resources_closed()

    def test_special_file_and_unsafe_link_refusals_close_directory_descriptors(self):
        os.mkfifo(self.source / "special")
        with self.assertRaisesRegex(inventory.InventoryError, "special file"):
            inventory.tree_summary(self.source)
        (self.source / "special").unlink()
        (self.source / "unsafe").symlink_to("../outside")
        with self.assertRaisesRegex(inventory.InventoryError, "symbolic link"):
            inventory.tree_summary(self.source, validate_links=True)
        self.assert_resources_closed()
        with self.assertRaisesRegex(inventory.InventoryError, "symbolic link"):
            self.fingerprint(validate_links=True)
        self.assert_resources_closed()

    def test_directory_swapped_for_link_is_not_followed(self):
        child = self.source / "child"
        child.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "unrelated").write_bytes(b"private")
        original_open = os.open

        def replaced(name, flags, *args, **kwargs):
            if name == "child":
                child.rmdir()
                child.symlink_to(outside)
            return original_open(name, flags, *args, **kwargs)

        with patch.object(inventory.os, "open", replaced):
            with self.assertRaisesRegex(inventory.InventoryError, "read completely"):
                inventory.tree_summary(self.source)
        self.assert_resources_closed()

    def test_file_swapped_for_link_is_not_followed(self):
        item = self.source / "item"
        item.write_bytes(b"original")
        outside = self.root / "outside"
        outside.write_bytes(b"unrelated")
        original_open = os.open

        def replaced(name, flags, *args, **kwargs):
            if name == "item":
                item.unlink()
                item.symlink_to(outside)
            return original_open(name, flags, *args, **kwargs)

        with patch.object(inventory.os, "open", replaced):
            with self.assertRaisesRegex(inventory.InventoryError, "read completely"):
                self.fingerprint()
        self.assert_resources_closed()

    def test_file_mutation_during_content_read_is_refused(self):
        item = self.source / "item"
        item.write_bytes(b"original")
        original_read = os.read
        changed = False

        def changing(descriptor, size):
            nonlocal changed
            value = original_read(descriptor, size)
            if not changed:
                changed = True
                item.write_bytes(b"changed actual bytes")
            return value

        with patch.object(inventory.os, "read", changing):
            with self.assertRaisesRegex(inventory.InventoryError, "changed while"):
                self.fingerprint()
        self.assert_resources_closed()

    def test_manifest_full_refuses_and_removes_scratch_and_open_descriptors(self):
        for index in range(30):
            (self.source / (f"item-{index}-" + "x" * 180)).touch()
        with patch.object(inventory, "MAX_TREE_MANIFEST_BYTES", 8192):
            with self.assertRaisesRegex(inventory.InventoryError, "manifest"):
                self.fingerprint()
        self.assert_resources_closed()

    def test_fingerprint_entry_limit_cleans_private_manifest(self):
        (self.source / "one").touch()
        (self.source / "two").touch()
        with patch.object(inventory, "MAX_TREE_ENTRIES", 1):
            with self.assertRaisesRegex(inventory.InventoryError, "inventory exceeds"):
                self.fingerprint()
        self.assert_resources_closed()

    def test_scratch_cannot_be_created_inside_inventory(self):
        with self.assertRaisesRegex(inventory.InventoryError, "outside"):
            inventory.tree_fingerprint(self.source, scratch_root=self.source)
        self.assertEqual(list(self.source.iterdir()), [])
        self.assert_resources_closed()
