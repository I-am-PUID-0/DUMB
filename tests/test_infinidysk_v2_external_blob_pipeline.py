"""External input uses real files and a real archive through recovery."""

import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from utils.infinidysk_v2_inventory import tree_fingerprint


class ExternalBlobPipelineTests(unittest.TestCase):
    def setUp(self):
        from tests.test_infinidysk_v2_low_space_pipeline import LowSpacePipelineTests
        from utils import infinidysk_v2_blob_staging as staging

        self.pipeline = LowSpacePipelineTests(methodName="runTest")
        self.pipeline.setUp()
        self.external_temporary = None
        self.addCleanup(self.finish_pipeline)
        self.p = self.pipeline
        self.p.start_patch(
            patch.object(staging, "get_backup_storage", return_value=self.p.storage)
        )
        # The canonical disposable lab can supply a second filesystem at an
        # ordinary backup path. Do not relax the real registry's /dev guard.
        external = os.environ.get("DUMB_TEST_EXTERNAL_STORAGE")
        lab = os.environ.get("DUMB_FIXTURE_LAB_ROOT")
        if not external or not lab:
            self.skipTest("a disposable separate storage mount was not supplied")
        external_root, lab_root = Path(external), Path(lab)
        if (
            not external_root.is_relative_to(lab_root)
            or (lab_root / "fixture.marker").read_text().strip()
            != "DUMB_DISPOSABLE_INFINIDYSK_V2"
        ):
            self.skipTest("the separate storage mount is not a disposable fixture")
        temporary = tempfile.TemporaryDirectory(
            prefix="dumb-blob-pipeline-", dir=external_root
        )
        self.external_temporary = temporary
        self.p.archive = Path(temporary.name)
        if self.p.archive.stat().st_dev == self.p.source.stat().st_dev:
            self.skipTest("the fixture needs two distinct filesystems")
        self.p.storage.save_profile(
            {
                "id": "external",
                "name": "Migration input fixture",
                "path": str(self.p.archive),
                "enabled": True,
            }
        )
        self.p.start_patch(
            patch(
                "utils.infinidysk_v2_migration.shutil.disk_usage",
                return_value=SimpleNamespace(free=16 * 1024**3),
            )
        )
        nested = self.p.source / "blobs" / "aa" / "bb"
        nested.mkdir(parents=True)
        self.relative = Path("aa/bb/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self.contents = b"reserved legacy metadata fixture\n" * 200
        (self.p.source / "blobs" / self.relative).write_bytes(self.contents)
        self.original = tree_fingerprint(
            self.p.source, scratch_root=self.p.fixture.root
        )
        self.observed_staging = []
        self.before_native = None
        self.p.start_patch(
            patch(
                "utils.infinidysk_v2_execution.NativeCandidate",
                side_effect=self.native,
            )
        )
        self.p.preview = self.preview

    def finish_pipeline(self):
        try:
            self.pipeline.doCleanups()
        finally:
            if self.external_temporary is not None:
                self.external_temporary.cleanup()

    def preview(self):
        preview = self.p.manager.preview(
            {
                "mode": "migrate",
                "mount_backend": "rclone",
                "import_strategy": "preserve",
                "backup_destination_id": "external",
                "reclaim_source_space": True,
                "offload_legacy_blobs": True,
            }
        )
        self.assertTrue(preview["ready"], preview["blockers"])
        self.assertTrue(preview["source_storage"]["offload_legacy_blobs"])
        self.assertIn("external_blob_staging", preview["required_acknowledgements"])
        return preview, {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": dict.fromkeys(
                preview["required_acknowledgements"], True
            ),
        }

    def native(self, service, candidate, private, *args, **options):
        job = self.p.manager._read(self.p.manager._job_path(Path(private).name))
        record = job["snapshot"]["external_blob_staging"]
        stage = Path(record["directory"]) / "blobs"
        self.assertEqual(record["state"], "released")
        self.assertEqual((stage / self.relative).read_bytes(), self.contents)
        self.assertEqual(list((Path(candidate) / "blobs").iterdir()), [])
        self.assertTrue(options.get("external_blobs"))
        self.observed_staging.append(stage)
        if self.before_native:
            self.before_native(stage)
        return self.p.native(service, candidate, private, *args, **options)

    def assert_original_restored(self):
        self.assertEqual(
            self.original,
            tree_fingerprint(self.p.source, scratch_root=self.p.fixture.root),
        )
        self.assertEqual(
            (self.p.source / "blobs" / self.relative).read_bytes(), self.contents
        )
        self.assertTrue(list(self.p.archive.rglob("*.tar")))
        self.assertEqual("1", self.p.config.config["infinidysk"]["runtime_generation"])

    def unlink_failure(self, error):
        native_unlink = os.unlink
        fired = []

        def unlink(path, *args, **kwargs):
            parent = kwargs.get("dir_fd")
            if parent is not None and not fired:
                resolved = Path(os.readlink(f"/proc/self/fd/{parent}"))
                if (
                    self.p.fixture.root in resolved.parents
                    and "blobs" in resolved.parts
                    and ".dumb-v2-" in str(resolved)
                ):
                    native_unlink(path, *args, **kwargs)
                    fired.append(True)
                    raise error
            return native_unlink(path, *args, **kwargs)

        return (
            patch("utils.infinidysk_v2_blob_staging.os.unlink", side_effect=unlink),
            fired,
        )

    def test_rehearsal_cutover_and_asynchronous_rollback_restore_real_archive(self):
        _, request = self.preview()
        rehearsed = self.p.wait(self.p.manager.rehearse(request))
        self.assertEqual("rehearsed", rehearsed["status"], rehearsed)
        self.assert_original_restored()
        completed = self.p.wait(
            self.p.manager.start({**request, "rehearsal_job_id": rehearsed["job_id"]})
        )
        self.assertEqual("completed", completed["status"], completed)
        self.assertFalse(self.p.source.exists())
        response = self.p.manager.rollback(
            {
                "job_id": completed["job_id"],
                "confirmation": completed["rollback_confirmation_text"],
                "acknowledge_discard_v2_writes": True,
            }
        )
        restored = self.p.wait(response)
        self.assertEqual("rolled_back", restored["status"], restored)
        self.assert_original_restored()
        self.assertEqual(2, len(self.observed_staging))

    def test_failure_after_first_local_unlink_restores_complete_original(self):
        _, request = self.preview()
        injected, fired = self.unlink_failure(
            OSError("interrupted local release fixture")
        )
        with injected:
            result = self.p.wait(self.p.manager.rehearse(request))
        self.assertTrue(fired)
        self.assertEqual("failed_restored", result["status"], result)
        self.assertFalse(self.observed_staging)
        self.assert_original_restored()

    def test_controller_restart_after_partial_release_keeps_archive_recovery(self):
        from utils.infinidysk_v2_migration import InfiniDyskV2MigrationManager

        _, request = self.preview()
        injected, fired = self.unlink_failure(
            SystemExit("controller interruption fixture")
        )
        with injected:
            result = self.p.wait(self.p.manager.rehearse(request))
        self.assertTrue(fired)
        self.assertFalse(self.p.source.exists())
        manager = InfiniDyskV2MigrationManager(
            process_handler=self.p.handler,
            updater=self.p.fixture.updater,
            config_manager=self.p.config,
            state_root=self.p.manager.state_root,
            executor=self.p.executor,
        )
        self.p.fixture.managers.append(manager)
        self.p.manager = manager
        retained = manager.get_job(result["job_id"])["job"]
        self.assertTrue(retained["can_rollback"], retained)
        restored = self.p.wait(
            manager.rollback(
                {
                    "job_id": retained["job_id"],
                    "confirmation": retained["rollback_confirmation_text"],
                }
            )
        )
        self.assertEqual("rolled_back", restored["status"], restored)
        self.assert_original_restored()

    def test_missing_expanded_input_does_not_prevent_archive_based_recovery(self):
        def lose_input(stage):
            # This is exclusively created disposable test storage. The archive
            # beside it remains intact and is the authoritative recovery source.
            for root, directories, _files in os.walk(stage):
                os.chmod(root, 0o700)
                for name in directories:
                    os.chmod(Path(root) / name, 0o700)
            shutil.rmtree(stage)
            raise ConnectionError("expanded input disappeared")

        self.before_native = lose_input
        _, request = self.preview()
        result = self.p.wait(self.p.manager.rehearse(request))
        self.assertEqual("failed_restored", result["status"], result)
        self.assert_original_restored()

    def test_cancelling_import_restores_v1_instead_of_leaving_missing_blobs(self):
        self.p.test_cancelled_native_import_restores_reclaimed_v1_from_verified_archive()
        self.assert_original_restored()


if __name__ == "__main__":
    unittest.main()
