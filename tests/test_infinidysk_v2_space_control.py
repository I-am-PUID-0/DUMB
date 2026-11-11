"""Archive-backed source reuse survives interruption without adopting data."""

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from utils.backup_storage import BackupStorage, BackupStorageError
from utils.infinidysk_v2_archive import create_retained_archive
from utils.infinidysk_v2_inventory import tree_fingerprint
from utils import infinidysk_v2_space_control as control_module
from utils.infinidysk_v2_space_control import SpaceReuse, identity


class SimulatedInterruption(RuntimeError):
    pass


class SourceSpaceControllerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dumb-space-control-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "v1-data"
        self.runtime = self.data / "backend"
        self.scratch = self.root / ("c" * 32)
        self.archive = self.root / "mounted-archives"
        self.target = self.root / "v2-data"
        for path in (self.data, self.runtime, self.scratch, self.archive):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.data / "blobs").mkdir(mode=0o750)
        (self.data / "blobs" / "metadata").write_bytes(b"retained original blob")
        (self.data / "relative").symlink_to("blobs/metadata")
        (self.data / "db.sqlite").write_bytes(b"retained original sqlite bytes")
        (self.runtime / "backend.dll").write_bytes(b"retained nested runtime")
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
                "name": "External archive",
                "path": str(self.archive),
                "enabled": True,
            }
        )
        selected = self.storage.bound_identity("nas")
        self.plan = {
            "mode": "migrate",
            "options": {"reclaim_source_space": True},
            "source": {
                "data_dir": str(self.data),
                "runtime_dir": str(self.runtime),
                "data_identity": identity(self.data),
                "runtime_identity": {"directory": identity(self.runtime)},
            },
            "target": {
                "data_dir": str(self.target),
                "mount_path": str(self.root / "delivery"),
            },
            "backup_storage": {
                "mode": "cold_archive",
                "destination_id": "nas",
                "artifact_root": selected["artifact_root"],
                "binding_fingerprint": selected["binding_fingerprint"],
            },
        }
        config_backup = self.scratch / "config.json"
        config_backup.write_text('{"example":true}')
        config_backup.chmod(0o600)
        self.workspace = {
            "root": str(self.scratch),
            "config_backup": str(config_backup),
            "candidate_data": str(
                self.root / (".v2-data.dumb-v2-" + self.scratch.name)
            ),
        }
        self.candidate = Path(self.workspace["candidate_data"])
        fingerprints = {
            str(path): tree_fingerprint(path, scratch_root=self.scratch)
            for path in (self.data, self.runtime)
        }
        result = create_retained_archive(
            self.plan, self.workspace, self.storage, fingerprints
        )
        self.snapshot = {
            "source_snapshot": str(self.scratch),
            "archive_roots": result["archive_roots"],
            "cold_archive": result["receipt"],
            "original_fingerprints": fingerprints,
        }
        self.job = {
            "job_id": self.scratch.name,
            "plan": self.plan,
            "snapshot": self.snapshot,
        }
        self.disk = self.scratch / "job.json"
        self.states = []

        def write(path, value):
            self.states.append(copy.deepcopy(value))
            Path(path).write_text(json.dumps(value))
            Path(path).chmod(0o600)

        self.manager = SimpleNamespace(
            config_manager=self.config,
            _write=write,
            _job_path=lambda job_id: self.disk,
            _workspace=lambda job: self.workspace,
            _assert_stopped=Mock(),
        )
        self.controller = SpaceReuse(self.manager)
        self.patch_storage = patch.object(
            control_module, "get_backup_storage", return_value=self.storage
        )
        self.patch_storage.start()
        self.addCleanup(self.patch_storage.stop)
        # Only the requirement for a second filesystem is simulated. Archive
        # bytes, descriptor checks, complete hashes and restores remain real.
        self.real_preflight = control_module.preflight_reuse
        self.patch_preflight = patch.object(control_module, "preflight_reuse")
        self.patch_preflight.start()
        self.addCleanup(self.patch_preflight.stop)
        self.unmounted = Mock()

    def prepare(self, *, authorize_native=True):
        result = self.controller.prepare(
            self.job, self.workspace, self.snapshot, Mock()
        )
        if authorize_native:
            # Simulate the manager's independent full candidate verification
            # before any native startup or PostgreSQL staging mutation.
            self.assertEqual(
                self.snapshot["original_fingerprints"][str(self.data)],
                tree_fingerprint(
                    self.candidate, scratch_root=self.scratch, validate_links=True
                ),
            )
            self.snapshot["space_recovery"]["native_mutations_authorized"] = True
            self.controller._save(self.job)
        return result

    def reload(self):
        self.job = json.loads(self.disk.read_text())
        self.snapshot = self.job["snapshot"]
        self.plan = self.job["plan"]

    def assert_original_restored(self):
        self.assertEqual("restored", self.snapshot["space_recovery"]["state"])
        self.assertFalse(self.candidate.exists())
        self.assertEqual(
            b"retained original blob", (self.data / "relative").read_bytes()
        )
        self.assertEqual(
            b"retained nested runtime", (self.runtime / "backend.dll").read_bytes()
        )
        for source, expected in self.snapshot["original_fingerprints"].items():
            self.assertEqual(
                expected, tree_fingerprint(source, scratch_root=self.scratch)
            )

    def test_real_prepare_and_restore_reuses_original_inode_and_recovers_nested_runtime(
        self,
    ):
        original = identity(self.data)
        self.assertEqual(original, self.prepare())
        self.assertFalse(self.data.exists())
        self.assertEqual(original, identity(self.candidate))
        self.assertEqual(
            "prepared", self.states[0]["snapshot"]["space_recovery"]["state"]
        )
        self.assertTrue(self.states[0]["snapshot"]["cold_archive"]["verified"])
        (self.candidate / "blobs" / "metadata").write_bytes(b"v2 mutation")
        self.controller.restore(self.job, self.unmounted)
        self.assert_original_restored()
        self.assertGreaterEqual(self.manager._assert_stopped.call_count, 3)
        self.assertTrue(self.unmounted.called)

    def test_restart_after_prepared_checkpoint_preserves_unmoved_original(self):
        original = identity(self.data)

        def interrupt():
            raise SimulatedInterruption("after prepared checkpoint")

        with self.assertRaises(SimulatedInterruption):
            self.controller.prepare(self.job, self.workspace, self.snapshot, interrupt)
        self.reload()
        self.assertEqual("prepared", self.snapshot["space_recovery"]["state"])
        self.controller.restore(self.job, self.unmounted)
        self.assert_original_restored()
        self.assertEqual(original, identity(self.data))

    def test_restart_after_source_rename_uses_prepared_durable_receipt(self):
        save = self.controller._save

        def interrupt(job):
            if job["snapshot"]["space_recovery"]["state"] == "reused":
                raise SimulatedInterruption("rename completed before checkpoint")
            save(job)

        with patch.object(self.controller, "_save", side_effect=interrupt):
            with self.assertRaises(SimulatedInterruption):
                self.prepare()
        self.assertFalse(self.data.exists())
        self.reload()
        self.assertEqual("prepared", self.snapshot["space_recovery"]["state"])
        self.controller.restore(self.job, self.unmounted)
        self.assert_original_restored()

    def test_restart_after_discard_recovers_from_durable_discarding_checkpoint(self):
        self.prepare()
        save = self.controller._save

        def interrupt(job):
            if job["snapshot"]["space_recovery"]["state"] == "restoring":
                raise SimulatedInterruption("discard completed before checkpoint")
            save(job)

        with patch.object(self.controller, "_save", side_effect=interrupt):
            with self.assertRaises(SimulatedInterruption):
                self.controller.restore(self.job, self.unmounted)
        self.assertFalse(self.candidate.exists())
        self.assertFalse(self.data.exists())
        self.reload()
        self.assertEqual("discarding", self.snapshot["space_recovery"]["state"])
        self.controller.restore(self.job, self.unmounted)
        self.assert_original_restored()

    def test_partial_restore_is_not_published_and_owned_retry_restores_completely(self):
        self.prepare()

        def interrupted(snapshot, plan, storage, **options):
            partial = Path(options["destination"])
            (partial / "interrupted").write_bytes(b"incomplete extraction")
            self.assertFalse(self.data.exists())
            raise SimulatedInterruption("during source extraction")

        with patch.object(
            control_module, "restore_archive_tree", side_effect=interrupted
        ):
            with self.assertRaises(SimulatedInterruption):
                self.controller.restore(self.job, self.unmounted)
        self.assertFalse(self.data.exists())
        self.reload()
        self.assertEqual(
            identity(self.candidate),
            self.snapshot["space_recovery"]["restore_identity"],
        )
        self.controller.restore(self.job, self.unmounted)
        self.assert_original_restored()
        self.assertFalse((self.data / "interrupted").exists())

    def test_archive_mount_outage_retains_sole_local_candidate(self):
        self.prepare()
        original = identity(self.candidate)
        self.archive.rename(self.root / "unavailable-archive-mount")
        with self.assertRaises(BackupStorageError):
            self.controller.restore(self.job, self.unmounted)
        self.assertEqual(original, identity(self.candidate))
        self.assertEqual(
            b"retained original blob", (self.candidate / "relative").read_bytes()
        )
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.data.exists())

    def test_promoted_v2_target_is_recovered_through_owned_candidate_path(self):
        self.prepare()
        self.candidate.rename(self.target)
        (self.target / "infinidysk.sqlite").write_bytes(b"native v2 database")
        self.controller.restore(self.job, self.unmounted)
        self.assert_original_restored()
        self.assertFalse(self.target.exists())
        self.assertFalse((self.data / "infinidysk.sqlite").exists())

    def test_running_managed_process_or_live_native_group_blocks_disposal(self):
        self.prepare()
        original = identity(self.candidate)
        self.manager._assert_stopped.side_effect = ValueError(
            "managed source still running"
        )
        with self.assertRaisesRegex(ValueError, "running"):
            self.controller.restore(self.job, self.unmounted)
        self.manager._assert_stopped.side_effect = None
        self.job["execution"] = {"native_process_group": 424242}
        execution = SimpleNamespace(
            _native_group_has_live_members=Mock(return_value=True)
        )
        with patch.dict(sys.modules, {"utils.infinidysk_v2_execution": execution}):
            with self.assertRaisesRegex(ValueError, "still alive"):
                self.controller.restore(self.job, self.unmounted)
        self.assertEqual(original, identity(self.candidate))
        self.assertFalse(self.data.exists())

    def test_foreign_original_or_candidate_is_never_adopted_or_deleted(self):
        self.prepare()
        self.data.mkdir(mode=0o700)
        (self.data / "foreign").write_bytes(b"protected source occupant")
        with self.assertRaisesRegex(ValueError, "occupied"):
            self.controller.restore(self.job, self.unmounted)
        self.assertEqual(
            b"protected source occupant", (self.data / "foreign").read_bytes()
        )
        (self.data / "foreign").unlink()
        self.data.rmdir()
        self.candidate.rename(self.root / "retained-candidate")
        self.candidate.mkdir(mode=0o700)
        (self.candidate / "foreign").write_bytes(b"protected candidate occupant")
        with self.assertRaisesRegex(ValueError, "missing or changed"):
            self.controller.restore(self.job, self.unmounted)
        self.assertEqual(
            b"protected candidate occupant", (self.candidate / "foreign").read_bytes()
        )

    def test_source_drift_during_archive_readback_retains_unarchived_local_changes(
        self,
    ):
        verify = self.controller.verify

        def source_changes(job):
            proof = verify(job)
            (self.data / "blobs" / "metadata").write_bytes(
                b"unexpected unarchived writes"
            )
            return proof

        with patch.object(self.controller, "verify", side_effect=source_changes):
            self.prepare(authorize_native=False)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.controller.restore(self.job, self.unmounted)
        self.assertEqual(
            b"unexpected unarchived writes", (self.candidate / "relative").read_bytes()
        )
        self.assertFalse(self.data.exists())

    def test_unmodified_pre_native_candidate_recovers_without_mutation_authorization(
        self,
    ):
        self.prepare(authorize_native=False)
        self.controller.restore(self.job, self.unmounted)
        self.assert_original_restored()

    def test_completed_source_recovery_does_not_rewind_later_legitimate_v1_writes(self):
        self.prepare()
        self.controller.restore(self.job, self.unmounted)
        recovered = identity(self.data)
        (self.data / "db.sqlite").write_bytes(b"legitimate writes after v1 restart")
        self.controller.restore(self.job, self.unmounted)
        self.assertEqual(recovered, identity(self.data))
        self.assertEqual(
            b"legitimate writes after v1 restart",
            (self.data / "db.sqlite").read_bytes(),
        )

    def test_unknown_native_writer_payload_blocks_disposal_without_group_proof(self):
        self.prepare()
        original = identity(self.candidate)
        (self.scratch / "candidate-payload.json").write_text('{"data_dir":"example"}')
        with self.assertRaisesRegex(ValueError, "writer|process|native|ownership"):
            self.controller.restore(self.job, self.unmounted)
        self.assertEqual(original, identity(self.candidate))

    def test_real_preflight_refuses_archive_on_same_filesystem(self):
        with self.assertRaisesRegex(ValueError, "different filesystem"):
            self.real_preflight(self.plan)
        self.assertTrue(self.data.exists())
        self.assertFalse(self.candidate.exists())

    def test_real_preflight_refuses_source_mount_nested_mount_and_cross_mount_target(
        self,
    ):
        def source_mount(path):
            return {"mount_id": "source" if Path(path) == self.data else "parent"}

        with patch.object(control_module, "_mount", side_effect=source_mount):
            with self.assertRaisesRegex(ValueError, "mount point"):
                self.real_preflight(self.plan)
        with patch.object(
            control_module, "_mount", return_value={"mount_id": "parent"}
        ):
            with patch.object(
                control_module,
                "mount_records",
                return_value=[{"path": str(self.data / "blobs")}],
            ):
                with self.assertRaisesRegex(ValueError, "mounted filesystem"):
                    self.real_preflight(self.plan)
        other_mount = self.root / "other-local-mount"
        other_mount.mkdir(mode=0o700)
        plan = copy.deepcopy(self.plan)
        plan["target"]["data_dir"] = str(other_mount / "v2-data")

        def target_mount(path):
            return {"mount_id": "other" if Path(path) == other_mount else "source"}

        with patch.object(control_module, "_mount", side_effect=target_mount):
            with self.assertRaisesRegex(ValueError, "same local mount"):
                self.real_preflight(plan)
        self.assertTrue(self.data.exists())

    def test_real_preflight_refuses_hardlinks_before_source_reuse(self):
        from utils.infinidysk_v2_inventory import InventoryError

        os.link(self.data / "blobs" / "metadata", self.data / "shared-hardlink")
        with self.assertRaisesRegex(InventoryError, "linked"):
            self.real_preflight(self.plan)
        self.assertEqual(
            b"retained original blob", (self.data / "shared-hardlink").read_bytes()
        )

    def test_new_candidate_created_during_archive_readback_is_never_replaced(self):
        verify = self.controller.verify

        def occupy_after_readback(job):
            proof = verify(job)
            self.candidate.mkdir(mode=0o700)
            return proof

        with patch.object(self.controller, "verify", side_effect=occupy_after_readback):
            with self.assertRaises(FileExistsError):
                self.prepare()
        self.assertTrue(self.data.exists())
        self.assertEqual([], list(self.candidate.iterdir()))
        self.assertEqual(
            b"retained original blob", (self.data / "relative").read_bytes()
        )

    def test_kernel_no_replace_rename_preserves_existing_destination(self):
        self.candidate.mkdir(mode=0o700)
        (self.candidate / "protected").write_bytes(b"existing unrelated data")
        original_source = identity(self.data)
        original_destination = identity(self.candidate)
        with self.assertRaises(FileExistsError):
            control_module.rename_exclusive(
                self.data,
                self.candidate,
                original_source,
                identity(self.data.parent),
                identity(self.candidate.parent),
            )
        self.assertEqual(original_source, identity(self.data))
        self.assertEqual(original_destination, identity(self.candidate))
        self.assertEqual(
            b"existing unrelated data", (self.candidate / "protected").read_bytes()
        )

    def test_insufficient_restore_capacity_keeps_candidate_and_retry_recovers_source(
        self,
    ):
        self.prepare()
        original = identity(self.candidate)
        with patch.object(
            control_module.shutil, "disk_usage", return_value=SimpleNamespace(free=0)
        ):
            with self.assertRaisesRegex(ValueError, "space.*insufficient"):
                self.controller.restore(self.job, self.unmounted)
        self.assertEqual(original, identity(self.candidate))
        self.assertEqual(
            b"retained original blob", (self.candidate / "relative").read_bytes()
        )
        self.assertFalse(self.data.exists())
        self.assertEqual("reused", self.snapshot["space_recovery"]["state"])
        self.controller.restore(self.job, self.unmounted)
        self.assert_original_restored()

    def test_archive_binding_drift_blocks_prepare_before_any_source_rename(self):
        self.plan["backup_storage"]["binding_fingerprint"] = "changed"
        with self.assertRaisesRegex(ValueError, "destination changed"):
            self.prepare()
        self.assertTrue(self.data.exists())
        self.assertFalse(self.candidate.exists())


if __name__ == "__main__":
    unittest.main()
