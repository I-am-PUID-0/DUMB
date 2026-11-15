"""Real external blob copies release only verified owned local input."""

from contextlib import closing
from copy import deepcopy
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests import test_infinidysk_v2_space_control as fixtures
from utils import infinidysk_v2_blob_staging as staging
from utils.backup_storage import BackupStorageError
from utils.infinidysk_v2_archive import create_retained_archive
from utils.infinidysk_v2_inventory import tree_fingerprint
from utils.infinidysk_v2_space_control import SpaceReuse


class ExternalBlobInputTests(unittest.TestCase):
    def setUp(self):
        fixtures.SourceSpaceControllerTests.setUp(self)
        self.addCleanup(self.patch_preflight.stop)
        # These guard/ownership tests use real files, hashes and archives.
        # Only physical device separation is supplied by the fixture; the
        # end-to-end namespace suite also exercises a real second filesystem.
        self.external = self.root / "external-input"
        self.external.mkdir()
        device = patch.object(staging, "require_external_filesystem")
        device.start()
        self.addCleanup(device.stop)
        self.storage.save_profile(
            {
                "id": "external",
                "name": "External input",
                "path": str(self.external),
                "enabled": True,
            }
        )
        selected = self.storage.bound_identity("external")
        self.plan["backup_storage"].update(selected)
        self.plan["options"]["offload_legacy_blobs"] = True
        self.manager._assert_job_owner = Mock()
        self.manager.executor = SimpleNamespace(
            _execution_ids=lambda: (os.geteuid(), os.getegid())
        )

        def progress(job, stage, message=None, **fields):
            job.update(stage=stage, message=message, **fields)
            self.manager._write(self.manager._job_path(job["job_id"]), job)

        self.manager._progress = progress
        self.storage_patch = patch.object(
            staging, "get_backup_storage", return_value=self.storage
        )
        self.storage_patch.start()
        self.addCleanup(self.storage_patch.stop)
        source_preparer = getattr(self, "_source_preparer", None)
        if source_preparer is not None:
            source_preparer(self.data)
        fingerprints = {
            str(path): tree_fingerprint(path, scratch_root=self.scratch)
            for path in (self.data, self.runtime)
        }
        result = create_retained_archive(
            self.plan, self.workspace, self.storage, fingerprints
        )
        self.snapshot.update(
            archive_roots=result["archive_roots"],
            cold_archive=result["receipt"],
            original_fingerprints=fingerprints,
        )
        self.controller = SpaceReuse(self.manager)
        self.controller.prepare(self.job, self.workspace, self.snapshot, Mock())
        self.snapshot["space_recovery"]["native_mutations_authorized"] = True
        self.controller._save(self.job)
        self.staging = staging.ExternalBlobStaging(self.manager)

    def prepare(self, check=None):
        return self.staging.prepare(
            self.job, self.workspace, self.snapshot, check or Mock()
        )

    def test_real_copy_readback_release_and_archive_only_source_restore(self):
        original = (self.candidate / "blobs/metadata").read_bytes()
        output = self.prepare()
        self.assertEqual((output / "metadata").read_bytes(), original)
        self.assertEqual(list((self.candidate / "blobs").iterdir()), [])
        self.assertEqual(self.snapshot["external_blob_staging"]["state"], "released")
        self.assertEqual(self.staging.verify(self.job), output)
        self.controller.restore(self.job, Mock())
        fixtures.SourceSpaceControllerTests.assert_original_restored(self)
        self.staging.cleanup(self.job)
        self.assertEqual(self.snapshot["external_blob_staging"]["state"], "cleaned")
        self.assertFalse(output.parent.exists())
        self.assertTrue(self.snapshot["cold_archive"]["verified"])

    def test_changed_copy_is_refused_before_any_local_release(self):
        original = self.staging._verify_tree

        def damaged(job, root, database, **options):
            if options["stage"]:
                (root / "metadata").write_bytes(b"changed staged bytes")
            return original(job, root, database, **options)

        with patch.object(self.staging, "_verify_tree", side_effect=damaged):
            with self.assertRaisesRegex(ValueError, "changed|verify"):
                self.prepare()
        self.assertEqual(
            (self.candidate / "blobs/metadata").read_bytes(), b"retained original blob"
        )

    def test_archive_corruption_after_staging_prevents_every_local_unlink(self):
        original_verify = staging.verify_retained_archive
        original_unlink = os.unlink
        archive = next(self.external.rglob("*.tar"))
        checks, released = 0, []

        def verify(*args, **kwargs):
            nonlocal checks
            checks += 1
            if checks == 2:
                with archive.open("r+b") as handle:
                    handle.write(b"damaged retained archive")
            return original_verify(*args, **kwargs)

        def unlink(path, *args, **kwargs):
            if path == "metadata" and kwargs.get("dir_fd") is not None:
                released.append(path)
            return original_unlink(path, *args, **kwargs)

        with (
            patch.object(staging, "verify_retained_archive", side_effect=verify),
            patch.object(staging.os, "unlink", side_effect=unlink),
        ):
            with self.assertRaises((ValueError, BackupStorageError)):
                self.prepare()
        self.assertEqual(checks, 2)
        self.assertEqual(released, [])
        self.assertEqual(self.snapshot["external_blob_staging"]["state"], "verified")
        self.assertEqual(
            (self.candidate / "blobs/metadata").read_bytes(), b"retained original blob"
        )

    def test_cleanup_replacement_after_full_verification_is_never_removed(self):
        output = self.prepare()
        original = self.staging._verify_tree

        def replace_after_check(job, root, database, **options):
            result = original(job, root, database, **options)
            path = root / "metadata"
            path.unlink()
            path.write_bytes(b"unowned replacement")
            return result

        with patch.object(
            self.staging, "_verify_tree", side_effect=replace_after_check
        ):
            with self.assertRaisesRegex(ValueError, "replaced before cleanup"):
                self.staging.cleanup(self.job)
        self.assertEqual((output / "metadata").read_bytes(), b"unowned replacement")

    def test_staging_root_or_parent_replacement_never_redirects_a_copy(self):
        namespace = self.external / ".dumb-backups" / "external" / staging.NAMESPACE
        namespace.mkdir(mode=0o700)
        protected = self.root / "protected"
        protected.mkdir()
        (protected / "sentinel").write_bytes(b"preserve")
        (namespace / self.job["job_id"]).symlink_to(protected, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "occupied"):
            self.prepare()
        self.assertEqual((protected / "sentinel").read_bytes(), b"preserve")
        self.assertEqual(list(protected.iterdir()), [protected / "sentinel"])
        self.assertTrue((self.candidate / "blobs/metadata").is_file())

    def test_zero_progress_write_fails_without_releasing_local_input(self):
        with patch.object(staging.os, "write", return_value=0):
            with self.assertRaisesRegex(OSError, "could not write"):
                self.prepare()
        self.assertEqual(
            (self.candidate / "blobs/metadata").read_bytes(), b"retained original blob"
        )
        self.assertEqual(self.snapshot["external_blob_staging"]["state"], "copying")
        self.controller.restore(self.job, Mock())
        fixtures.SourceSpaceControllerTests.assert_original_restored(self)

    def test_cancellation_during_copy_keeps_all_local_input(self):
        def cancel():
            record = self.snapshot.get("external_blob_staging") or {}
            if record.get("state") == "copying":
                raise ValueError("fixture cancellation")

        with self.assertRaisesRegex(ValueError, "cancellation"):
            self.prepare(cancel)
        self.assertEqual(
            (self.candidate / "blobs/metadata").read_bytes(), b"retained original blob"
        )
        self.controller.restore(self.job, Mock())
        fixtures.SourceSpaceControllerTests.assert_original_restored(self)

    def test_missing_or_changed_external_input_does_not_block_archive_restore(self):
        output = self.prepare()
        (output / "metadata").unlink()
        with self.assertRaises(ValueError):
            self.staging.verify(self.job)
        self.controller.restore(self.job, Mock())
        fixtures.SourceSpaceControllerTests.assert_original_restored(self)

    def test_manifest_replacement_is_never_used_to_release_or_delete_input(self):
        output = self.prepare()
        manifest = Path(self.snapshot["external_blob_staging"]["manifest"])
        original = manifest.read_bytes()
        manifest.unlink()
        manifest.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            self.staging.verify(self.job)
        self.assertTrue((output / "metadata").is_file())

    def test_link_input_is_refused_without_following_it(self):
        blobs = self.root / "unsupported"
        blobs.mkdir()
        sentinel = self.root / "sentinel"
        sentinel.write_bytes(b"preserve external bytes")
        (blobs / "alias").symlink_to(sentinel)
        with self.assertRaisesRegex(ValueError, "direct files"):
            staging.blob_summary(blobs)
        self.assertEqual(sentinel.read_bytes(), b"preserve external bytes")

    def test_owner_loss_during_release_keeps_archive_and_requires_recovery(self):
        original_unlink = os.unlink
        released = False

        def unlink(path, *args, **kwargs):
            nonlocal released
            result = original_unlink(path, *args, **kwargs)
            if path == "metadata" and kwargs.get("dir_fd") is not None:
                released = True
            return result

        def check():
            if released:
                raise ValueError("owned release interrupted")

        with patch.object(staging.os, "unlink", side_effect=unlink):
            with self.assertRaisesRegex(ValueError, "interrupted"):
                self.prepare(check)
        self.assertEqual(self.snapshot["external_blob_staging"]["state"], "releasing")
        self.assertTrue(self.snapshot["cold_archive"]["verified"])
        self.controller.restore(self.job, Mock())
        fixtures.SourceSpaceControllerTests.assert_original_restored(self)


class ExternalSparseBlobTests(unittest.TestCase):
    def test_sparse_input_credits_only_allocated_blocks_and_copies_full_content(self):
        fixture = ExternalBlobInputTests(methodName="runTest")

        def sparse_source(data):
            with (data / "blobs/metadata").open("wb") as handle:
                handle.seek(8 * 1024**2 - 1)
                handle.write(b"x")

        fixture._source_preparer = sparse_source
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.candidate / "blobs/metadata"
        expected = source.read_bytes()
        measured = staging.blob_summary(source.parent)
        allocated = min(source.stat().st_size, source.stat().st_blocks * 512)
        self.assertEqual(measured["logical_bytes"], len(expected))
        self.assertEqual(measured["allocated_bytes"], allocated)
        output = fixture.prepare()
        self.assertEqual((output / "metadata").read_bytes(), expected)
        self.assertLessEqual(
            fixture.snapshot["external_blob_staging"]["released_bytes"], allocated
        )
        fixture.controller.restore(fixture.job, Mock())
        self.assertEqual((fixture.data / "blobs/metadata").read_bytes(), expected)
        fixture.staging.cleanup(fixture.job)


class ExternalBlobCapacityTests(unittest.TestCase):
    def setUp(self):
        from utils import infinidysk_v2_migration as migration

        self.migration = migration
        temporary = tempfile.TemporaryDirectory(prefix="dumb-input-capacity-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state, self.source, self.archive = (
            self.root / "state",
            self.root / "v1",
            self.root / "archive",
        )
        for path in (self.state, self.source / "blobs", self.archive):
            path.mkdir(parents=True)
        self.manager = object.__new__(migration.InfiniDyskV2MigrationManager)
        self.manager.state_root = self.state
        self.plan = {
            "source": {"data_dir": str(self.source)},
            "target": {"data_dir": str(self.root / "v2")},
            "backup_storage": {"artifact_root": str(self.archive)},
        }
        self.gib = 1024**3
        self.summary = {
            "entries": 10,
            "logical_bytes": 20 * self.gib,
            "allocated_bytes": 8 * self.gib,
        }

    def report(self, *, free=10, headroom=12):
        original_stat = Path.stat

        def devices(path, *args, **kwargs):
            value = original_stat(path, *args, **kwargs)
            if path == self.archive:
                fields = list(value)
                fields[2] += 1
                return os.stat_result(fields)
            return value

        def usage(path):
            external = path == self.archive
            return SimpleNamespace(free=(100 if external else free) * self.gib)

        with (
            patch.object(staging, "blob_summary", return_value=self.summary),
            patch.object(Path, "stat", devices),
            patch.object(self.migration.shutil, "disk_usage", side_effect=usage),
        ):
            return self.manager._external_blob_capacity(
                self.plan,
                [
                    (self.state, 2 * self.gib, "local_verification"),
                    (self.root, (headroom + 1) * self.gib, "candidate"),
                    (self.archive, 30 * self.gib, "cold_archive"),
                ],
                {"entries": 11, "logical_bytes": 21 * self.gib, "root_count": 1},
                database_bytes=self.gib,
                native_headroom=headroom * self.gib,
            )

    def test_projected_release_is_separate_from_observed_free_and_stable_filesystems(
        self,
    ):
        report = self.report()
        self.assertTrue(report["release_is_projected"])
        self.assertEqual(report["status"], "sufficient")
        self.assertEqual(
            [phase["phase"] for phase in report["phases"]],
            ["before_blob_release", "after_blob_release"],
        )
        before, after = report["phases"]
        local_before = next(
            row for row in before["filesystems"] if "local_verification" in row["roles"]
        )
        local_after = after["filesystems"][0]
        self.assertEqual(local_before["filesystem_id"], local_after["filesystem_id"])
        self.assertNotEqual(local_before["id"], local_after["id"])
        self.assertEqual(local_before["free_bytes"], 10 * self.gib)
        self.assertEqual(local_after["free_bytes"], 10 * self.gib)
        self.assertEqual(local_before["projected_released_bytes"], 0)
        self.assertEqual(local_after["projected_released_bytes"], 8 * self.gib)
        self.assertEqual(local_after["projected_available_bytes"], 18 * self.gib)
        self.assertIn(
            "external_blob_staging",
            next(
                row for row in before["filesystems"] if "cold_archive" in row["roles"]
            )["roles"],
        )

    def test_logical_blob_size_is_not_treated_as_allocated_reclaim_credit(self):
        with self.assertRaises(self.migration.InfiniDyskV2CapacityError) as caught:
            self.report(headroom=20)
        after = caught.exception.capacity["phases"][1]["filesystems"][0]
        self.assertEqual(after["projected_released_bytes"], 8 * self.gib)
        self.assertGreater(after["shortfall_bytes"], 0)
        self.assertEqual(after["free_bytes"], 10 * self.gib)

    def test_initial_postgres_and_archive_work_must_fit_before_any_release(self):
        self.summary["allocated_bytes"] = 20 * self.gib
        with self.assertRaises(self.migration.InfiniDyskV2CapacityError) as caught:
            self.report(free=1)
        before, after = caught.exception.capacity["phases"]
        self.assertEqual(before["status"], "insufficient")
        self.assertEqual(after["status"], "sufficient")
        self.assertEqual(before["filesystems"][0]["projected_released_bytes"], 0)

    def test_after_release_recheck_uses_current_free_without_adding_credit_again(self):
        self.manager._progress = Mock()
        job = {
            "plan": self.plan,
            "snapshot": {
                "source_logical_bytes": 20 * self.gib,
                "external_blob_staging": {"released_bytes": 8 * self.gib},
            },
        }
        with patch.object(
            self.migration.shutil,
            "disk_usage",
            return_value=SimpleNamespace(free=10 * self.gib),
        ):
            with self.assertRaises(self.migration.InfiniDyskV2CapacityError) as caught:
                self.manager._external_capacity_after_release(job)
        row = caught.exception.capacity["filesystems"][0]
        self.assertEqual(row["free_bytes"], 10 * self.gib)
        self.assertEqual(row["projected_released_bytes"], 0)
        self.assertEqual(row["projected_available_bytes"], 10 * self.gib)
        self.manager._progress.assert_not_called()


class ExternalBlobPreviewTests(unittest.TestCase):
    def setUp(self):
        from tests.test_infinidysk_v2_migration import InfiniDyskV2MigrationTests

        self.fixture = InfiniDyskV2MigrationTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.archive_profile()
        self.fixture.executor.supports_external_blob_staging = True
        self.manager = self.fixture.manager

    def test_option_is_strict_requires_reuse_and_adds_independent_acknowledgement(self):
        from utils.infinidysk_v2_migration import InfiniDyskV2MigrationError

        with self.assertRaisesRegex(InfiniDyskV2MigrationError, "boolean"):
            self.manager.preview({"offload_legacy_blobs": "true"})
        options = {
            "mode": "migrate",
            "backup_destination_id": "nas",
            "offload_legacy_blobs": True,
        }
        with patch.object(
            self.manager, "_copy_capacity", return_value={"status": "sufficient"}
        ):
            blocked = self.manager.preview(options)
            self.assertFalse(blocked["ready"])
            self.assertTrue(any("reuse" in value for value in blocked["blockers"]))
            with patch("utils.infinidysk_v2_space_control.preflight_reuse"):
                preview = self.manager.preview(
                    {**options, "reclaim_source_space": True}
                )
        self.assertTrue(preview["ready"], preview["blockers"])
        self.assertTrue(preview["source_storage"]["offload_legacy_blobs"])
        self.assertIn("external_blob_staging", preview["required_acknowledgements"])
        request = {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": dict.fromkeys(
                preview["required_acknowledgements"], True
            ),
        }
        request["acknowledgements"]["external_blob_staging"] = False
        with self.assertRaisesRegex(InfiniDyskV2MigrationError, "Acknowledge"):
            self.manager._request_plan(request)
        self.assertIsNone(self.manager._thread)

    def test_old_preview_shape_and_versioned_feature_gating_remain_explicit(self):
        from utils.infinidysk_v2_support import versioned_capabilities

        preview = self.manager.preview({"mode": "migrate"})
        self.assertNotIn("offload_legacy_blobs", preview["source_storage"])
        self.assertNotIn("external_blob_staging", preview["required_acknowledgements"])
        self.assertIs(
            versioned_capabilities("2.24.0")["infinidysk_v2_external_blob_staging"],
            True,
        )
        self.assertIs(
            versioned_capabilities("2.23.0")["infinidysk_v2_external_blob_staging"],
            False,
        )


if __name__ == "__main__":
    unittest.main()
