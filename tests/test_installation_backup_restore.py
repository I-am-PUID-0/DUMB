"""Real filesystem restore transactions, interruption and ownership boundaries."""

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from utils.installation_backup_archive import InstallationArchiveError
from utils.installation_backup_restore import apply, prepare, rollback


class InstallationRestoreTransactionsTests(unittest.TestCase):
    def fixture(self, *, cross_filesystem=False):
        source_context = tempfile.TemporaryDirectory(prefix="installation-source-")
        self.addCleanup(source_context.cleanup)
        if cross_filesystem:
            if not Path("/dev/shm").is_dir() or not os.access("/dev/shm", os.W_OK):
                self.skipTest("A writable second local filesystem is unavailable.")
            target_context = tempfile.TemporaryDirectory(
                prefix="installation-target-", dir="/dev/shm"
            )
        else:
            target_context = tempfile.TemporaryDirectory(prefix="installation-target-")
        self.addCleanup(target_context.cleanup)
        source, target = Path(source_context.name), Path(target_context.name)
        if cross_filesystem and source.stat().st_dev == target.stat().st_dev:
            self.skipTest("The available scratch roots share a filesystem.")
        (source / "config.json").write_text("restored configuration")
        (source / "new.db").write_bytes(b"restored database")
        (target / "config.json").write_text("original configuration")
        (target / "old.db").write_bytes(b"original database")
        records = []

        def persist(row):
            # Reopen the serialized journal in recovery, as after a controller exit.
            records.append(json.loads(json.dumps(row)))

        root = {"id": "root_000", "path": str(target), "kind": "directory"}
        return source, target, records, persist, root

    def assert_original(self, target):
        self.assertEqual((target / "config.json").read_text(), "original configuration")
        self.assertEqual((target / "old.db").read_bytes(), b"original database")
        self.assertFalse((target / "new.db").exists())

    def test_cross_filesystem_restore_and_repeated_rollback(self):
        source, target, records, persist, root = self.fixture(cross_filesystem=True)
        row = prepare(root, source, "a" * 32, [], persist)
        apply(row, persist)
        self.assertEqual((target / "config.json").read_text(), "restored configuration")
        self.assertEqual((target / "new.db").read_bytes(), b"restored database")
        self.assertFalse((target / "old.db").exists())
        recovered = records[-1]
        rollback(recovered, persist)
        rollback(records[-1], persist)
        self.assert_original(target)

    def test_reopened_journal_restores_original_at_every_directory_rename_boundary(
        self,
    ):
        for fault in range(8):
            with self.subTest(rename_boundary=fault):
                source, target, records, persist, root = self.fixture()
                row = prepare(root, source, "b" * 32, [], persist)
                calls = []

                def checkpoint(phase, name):
                    calls.append((phase, name))
                    if len(calls) - 1 == fault:
                        raise InterruptedError("injected transaction interruption")

                with self.assertRaises(InterruptedError):
                    apply(row, persist, checkpoint)
                rollback(records[-1], persist)
                rollback(records[-1], persist)
                self.assert_original(target)

    def test_reopened_file_journal_preserves_original_before_and_after_each_rename(
        self,
    ):
        for fault in range(4):
            with self.subTest(rename_boundary=fault):
                source, target, records, persist, _root = self.fixture()
                path = target / "config.json"
                root = {"id": "root_000", "path": str(path), "kind": "file"}
                row = prepare(root, source / "config.json", "c" * 32, [], persist)
                calls = []

                def checkpoint(phase, name):
                    calls.append((phase, name))
                    if len(calls) - 1 == fault:
                        raise InterruptedError("injected file transaction interruption")

                with self.assertRaises(InterruptedError):
                    apply(row, persist, checkpoint)
                rollback(records[-1], persist)
                rollback(records[-1], persist)
                self.assertEqual(path.read_text(), "original configuration")

    def test_root_permissions_and_timestamps_follow_restore_and_rollback(self):
        source, target, records, persist, root = self.fixture()
        os.chmod(source, 0o750)
        os.chmod(target, 0o700)
        original_time, archived_time = (
            1_700_000_000_000_000_000,
            1_710_000_000_000_000_000,
        )
        os.utime(source, ns=(archived_time, archived_time))
        os.utime(target, ns=(original_time, original_time))
        row = prepare(root, source, "d" * 32, [], persist)
        apply(row, persist)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o750)
        self.assertEqual(target.stat().st_mtime_ns, archived_time)
        rollback(records[-1], persist)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertEqual(target.stat().st_mtime_ns, original_time)
        self.assert_original(target)

    def test_new_unrecognized_target_entry_blocks_before_originals_move(self):
        source, target, _records, persist, root = self.fixture()
        row = prepare(root, source, "e" * 32, [], persist)
        (target / "foreign.txt").write_text("retain unrelated state")
        with self.assertRaises(InstallationArchiveError):
            apply(row, persist)
        self.assert_original(target)
        self.assertEqual((target / "foreign.txt").read_text(), "retain unrelated state")

    @unittest.skipUnless(
        os.geteuid() == 0, "Foreign ownership needs a privileged controller."
    )
    def test_privileged_restore_preserves_application_root_and_file_ownership(self):
        source, target, records, persist, root = self.fixture()
        for entry in [*source.iterdir(), source]:
            os.chown(entry, 1, 1)
        row = prepare(root, source, "2" * 32, [], persist)
        apply(row, persist)
        self.assertEqual((target.stat().st_uid, target.stat().st_gid), (1, 1))
        data = (target / "new.db").stat()
        self.assertEqual((data.st_uid, data.st_gid), (1, 1))
        rollback(records[-1], persist)
        self.assertEqual((target.stat().st_uid, target.stat().st_gid), (0, 0))
        self.assert_original(target)

    def test_replaced_rollback_holder_is_never_adopted(self):
        source, target, _records, persist, root = self.fixture()
        row = prepare(root, source, "f" * 32, [], persist)
        holder = Path(row["holder"])
        holder.rename(target / "retained-original-holder")
        holder.mkdir(mode=0o700)
        with self.assertRaises(InstallationArchiveError):
            apply(row, persist)
        self.assert_original(target)

    def test_recovery_control_subtree_is_retained_through_restore(self):
        source, target, records, persist, root = self.fixture()
        control = target / "recovery-control"
        control.mkdir(mode=0o700)
        (control / "operation.json").write_text("current controller recovery")
        row = prepare(root, source, "1" * 32, [str(control)], persist)
        apply(row, persist)
        self.assertEqual(
            (control / "operation.json").read_text(), "current controller recovery"
        )
        rollback(records[-1], persist)
        self.assert_original(target)
        self.assertEqual(
            (control / "operation.json").read_text(), "current controller recovery"
        )

    def test_atomic_controller_file_is_always_valid_at_every_directory_boundary(self):
        for fault in range(8):
            with self.subTest(boundary=fault):
                source, target, records, persist, root = self.fixture()
                (source / "config.json").write_text('{"version":"restored"}')
                (target / "config.json").write_text('{"version":"original"}')
                root["atomic_names"] = ["config.json"]
                row = prepare(root, source, "3" * 32, [], persist)
                calls = []

                def checkpoint(phase, name):
                    value = json.loads((target / "config.json").read_text())
                    self.assertIn(value["version"], {"original", "restored"})
                    calls.append((phase, name))
                    if len(calls) - 1 == fault:
                        raise InterruptedError("atomic controller interruption")

                with self.assertRaises(InterruptedError):
                    apply(row, persist, checkpoint)
                rollback(records[-1], persist)
                rollback(records[-1], persist)
                self.assertEqual(
                    json.loads((target / "config.json").read_text()),
                    {"version": "original"},
                )
                self.assertEqual((target / "old.db").read_bytes(), b"original database")
                self.assertFalse((target / "new.db").exists())

    def test_atomic_external_auth_file_never_has_an_absent_boot_window(self):
        for fault in range(4):
            with self.subTest(boundary=fault):
                source, target, records, persist, _root = self.fixture()
                (source / "config.json").write_text('{"users":["restored"]}')
                live = target / "config.json"
                live.write_text('{"users":["original"]}')
                root = {
                    "id": "root_000",
                    "path": str(live),
                    "kind": "file",
                    "atomic_restore": True,
                }
                row = prepare(root, source / "config.json", "4" * 32, [], persist)
                calls = []

                def checkpoint(phase, name):
                    self.assertIn(
                        json.loads(live.read_text())["users"],
                        [["original"], ["restored"]],
                    )
                    calls.append((phase, name))
                    if len(calls) - 1 == fault:
                        raise InterruptedError("atomic auth interruption")

                with self.assertRaises(InterruptedError):
                    apply(row, persist, checkpoint)
                rollback(records[-1], persist)
                rollback(records[-1], persist)
                self.assertEqual(json.loads(live.read_text()), {"users": ["original"]})

    def test_interrupted_atomic_rollback_retries_only_its_owned_partial_copy(self):
        source, target, records, normal_persist, root = self.fixture()
        root["atomic_names"] = ["config.json"]
        row = prepare(root, source, "5" * 32, [], normal_persist)
        apply(row, normal_persist)

        def interrupted_persist(value):
            normal_persist(value)
            record = value.get("atomic_files", {}).get("config.json", {})
            if record.get("discarded") and not record.get("discarded_ready"):
                raise InterruptedError("controller exited during owned retention copy")

        with self.assertRaises(InterruptedError):
            rollback(records[-1], interrupted_persist)
        self.assertEqual((target / "config.json").read_text(), "restored configuration")
        rollback(records[-1], normal_persist)
        rollback(records[-1], normal_persist)
        self.assert_original(target)

    def test_changed_atomic_original_copy_is_preserved_and_refused(self):
        source, target, records, persist, root = self.fixture()
        root["atomic_names"] = ["config.json"]
        row = prepare(root, source, "6" * 32, [], persist)
        apply(row, persist)
        original_copy = Path(row["holder"]) / "previous" / "config.json"
        original_copy.write_text("unexpected retained contents")
        with self.assertRaises(InstallationArchiveError):
            rollback(records[-1], persist)
        self.assertEqual(original_copy.read_text(), "unexpected retained contents")
        self.assertEqual((target / "config.json").read_text(), "restored configuration")

    def test_atomic_staged_content_change_blocks_before_any_original_moves(self):
        for file_root in [False, True]:
            with self.subTest(file_root=file_root):
                source, target, _records, persist, root = self.fixture()
                if file_root:
                    root = {
                        "id": "root_000",
                        "path": str(target / "config.json"),
                        "kind": "file",
                        "atomic_restore": True,
                    }
                    staged = source / "config.json"
                else:
                    root["atomic_names"] = ["config.json"]
                    staged = source
                row = prepare(root, staged, "7" * 32, [], persist)
                replacement = Path(row["holder"]) / "staged"
                if not file_root:
                    replacement /= "config.json"
                previous_inode = replacement.stat().st_ino
                replacement.write_text("unverified in-place bytes")
                self.assertEqual(replacement.stat().st_ino, previous_inode)
                with self.assertRaises(InstallationArchiveError):
                    apply(row, persist)
                self.assert_original(target)


if __name__ == "__main__":
    unittest.main()
