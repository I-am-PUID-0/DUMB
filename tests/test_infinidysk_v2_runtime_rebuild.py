"""A rebuilt container must recover the accepted runtime, never downgrade data."""

from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from utils import infinidysk_v2_releases as releases
from utils import infinidysk_v2_runtime as runtime
from utils import infinidysk_v2_update as update


class NativeRuntimeRebuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.program = self.root / "program"
        self.data = self.root / "data"
        self.data.mkdir()
        self.database = self.data / "native.sqlite"
        self.database.write_bytes(b"retained native database fixture")
        self.configuration = self.root / "config" / "dumb_config.json"
        self.configuration.parent.mkdir()
        self.configuration.write_text('{"preserve": "configured service choices"}')
        self.journals = self.configuration.parent / "updates" / "infinidysk-v2"
        self.service = {
            "enabled": True,
            "runtime_generation": "2",
            "mount_backend": "rclone",
            "config_dir": str(self.root / "legacy"),
            "v2_runtime_dir": str(self.program),
            "v2_data_dir": str(self.data),
            "v2_release_version": "latest",
        }
        self.handler = SimpleNamespace(logger=Mock())
        self.uid = os.geteuid() or 1000
        self.gid = os.getegid() or 1000
        self.accepted = None
        self.plan_changes = {}
        self.metadata_changes = {}
        self.during_staging = None
        self.staged = None

    @staticmethod
    def artifact(path, release, contents):
        path.mkdir()
        (path / "infinidysk").write_bytes(contents)
        (path / "infinidysk").chmod(0o755)
        (path / "LICENSE").write_text("fixture license")
        (path / "version.txt").write_text(release.removeprefix("v"))
        (path / "commit.txt").write_text("a" * 40)
        metadata = {
            "format": releases.FORMAT,
            "release": release,
            "commit": "a" * 40,
            "architecture": releases.architecture(),
            "archive_sha256": "b" * 64,
            "binary_sha256": hashlib.sha256(contents).hexdigest(),
        }
        (path / runtime.MARKER).write_text(json.dumps(metadata))
        return metadata

    def completed_update(self, *, original_selector="latest", prepare=None):
        """Create real lifecycle evidence without starting a process or database."""
        self.service["v2_release_version"] = original_selector
        self.artifact(self.program, runtime.RELEASE, b"initial beta6 program")
        staging = self.root / "update-staging"
        staging.mkdir()
        candidate = staging / "candidate"
        self.accepted = self.artifact(
            candidate, "v2.0.0-beta.10", b"accepted beta10 program"
        )
        return update.perform_native_update(
            self.service,
            candidate,
            self.accepted,
            journal_root=self.journals,
            stop_current=lambda: None,
            prepare_launch=prepare or (lambda: None),
            start_candidate=lambda: None,
            wait_ready=lambda: None,
            save_selection=lambda metadata: None,
            target_selector="latest",
        )

    def lose_container_program(self):
        self.program.rename(self.root / "previous-container-program")

    def journal(self):
        paths = list(self.journals.glob("*.json"))
        self.assertEqual(len(paths), 1)
        return paths[0]

    def resolve(self, service):
        self.assertEqual(service["v2_release_version"], "v2.0.0-beta.10")
        self.assertEqual(self.service["v2_runtime_dir"], service["v2_runtime_dir"])
        return {**self.accepted, **self.plan_changes}

    @contextmanager
    def stage(self, service, plan, *, uid, gid):
        self.assertEqual(service["v2_release_version"], "v2.0.0-beta.10")
        self.assertEqual((uid, gid), (self.uid, self.gid))
        staging = self.root / "restore-staging"
        staging.mkdir()
        self.staged = staging / "candidate"
        metadata = self.artifact(
            self.staged, "v2.0.0-beta.10", b"accepted beta10 program"
        )
        self.assertEqual(plan, {**self.accepted, **self.plan_changes})
        if self.during_staging is not None:
            self.during_staging()
        yield self.staged, {**metadata, **self.metadata_changes}

    def ensure(self, *, resolver=None):
        selection = copy.deepcopy(self.service)
        database = self.database.read_bytes()
        configuration = self.configuration.read_bytes()
        manager = SimpleNamespace(
            file_path=str(self.configuration),
            get=lambda key: {
                "data_root": str(self.root / "default-data"),
                "puid": self.uid,
                "pgid": self.gid,
            }.get(key),
        )
        # Scope the config fixture so full discovery's legacy global doubles do
        # not select real service paths, and restore every imported module after.
        with (
            patch.dict(
                sys.modules,
                {"utils.config_loader": SimpleNamespace(CONFIG_MANAGER=manager)},
            ),
            patch.object(
                runtime, "install_v2_runtime", return_value=(True, None)
            ) as initial_install,
            patch.object(
                runtime, "resolve_v2_release", side_effect=resolver or self.resolve
            ) as resolve,
            patch.object(
                runtime, "stage_v2_runtime_update", side_effect=self.stage
            ) as stage,
        ):
            result = runtime.ensure_active_v2_runtime(
                self.handler, "InfiniDysk", self.service
            )
        self.assertEqual(self.service, selection)
        self.assertEqual(self.database.read_bytes(), database)
        self.assertEqual(self.configuration.read_bytes(), configuration)
        return result, initial_install, resolve, stage

    def assert_refused(self, *, resolver=None):
        result, initial, resolve, stage = self.ensure(resolver=resolver)
        self.assertFalse(result[0])
        self.assertIsInstance(result[1], str)
        self.assertTrue(result[1])
        initial.assert_not_called()
        return resolve, stage

    def test_completed_beta10_receipt_restores_exact_bytes_for_latest_selection(self):
        receipt = self.completed_update()
        journal = self.journal().read_bytes()
        self.lose_container_program()
        result, initial, resolve, stage = self.ensure()
        self.assertEqual(result, (True, None))
        initial.assert_not_called()
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(stage.call_count, 1)
        self.assertEqual(
            self.program.joinpath("infinidysk").read_bytes(), b"accepted beta10 program"
        )
        self.assertEqual(
            runtime.installed_active_v2_version(self.service), self.accepted["release"]
        )
        self.assertTrue(
            update.validate_native_update_launch(self.service, self.journals)
        )
        self.assertEqual(self.journal().read_bytes(), journal)
        self.assertTrue(Path(receipt["previous_runtime"]).is_dir())
        self.assertFalse(self.staged.exists())

    def test_retained_old_pin_cannot_downgrade_already_upgraded_data(self):
        self.completed_update(original_selector=runtime.RELEASE)
        self.lose_container_program()
        result, initial, _resolve, _stage = self.ensure()
        self.assertEqual(result, (True, None))
        initial.assert_not_called()
        self.assertEqual(self.service["v2_release_version"], runtime.RELEASE)
        self.assertEqual(
            runtime.installed_active_v2_version(self.service), "v2.0.0-beta.10"
        )

    def test_verified_empty_runtime_directory_can_receive_accepted_artifact(self):
        self.completed_update()
        self.lose_container_program()
        self.program.mkdir()
        result, initial, _resolve, _stage = self.ensure()
        self.assertEqual(result, (True, None))
        initial.assert_not_called()
        self.assertEqual(
            runtime.installed_active_v2_version(self.service), "v2.0.0-beta.10"
        )

    def test_installed_runtime_needs_neither_network_resolution_nor_initial_install(
        self,
    ):
        self.completed_update()
        result, initial, resolve, stage = self.ensure()
        self.assertEqual(result, (True, None))
        initial.assert_not_called()
        resolve.assert_not_called()
        stage.assert_not_called()

    def test_changed_release_tag_commit_architecture_or_archive_is_refused(self):
        self.completed_update()
        self.lose_container_program()
        journal = self.journal().read_bytes()
        for key, changed in {
            "release": "v2.0.0-beta.11",
            "commit": "c" * 40,
            "architecture": (
                "x64" if self.accepted["architecture"] == "arm64" else "arm64"
            ),
            "archive_sha256": "d" * 64,
        }.items():
            with self.subTest(key=key):
                self.plan_changes = {key: changed}
                resolve, stage = self.assert_refused()
                self.assertEqual(resolve.call_count, 1)
                stage.assert_not_called()
                self.assertFalse(self.program.exists())
                self.assertEqual(self.journal().read_bytes(), journal)

    def test_staged_binary_receipt_must_equal_previously_accepted_metadata(self):
        self.completed_update()
        self.lose_container_program()
        self.metadata_changes = {"binary_sha256": "e" * 64}
        _resolve, stage = self.assert_refused()
        self.assertEqual(stage.call_count, 1)
        self.assertFalse(self.program.exists())
        self.assertTrue(self.staged.is_dir())

    def test_resolution_failure_never_falls_back_to_the_initial_runtime(self):
        self.completed_update()
        self.lose_container_program()
        journal = self.journal().read_bytes()

        def unavailable(_service):
            raise releases.NativeReleaseError("fixture immutable release unavailable")

        _resolve, stage = self.assert_refused(resolver=unavailable)
        stage.assert_not_called()
        self.assertFalse(self.program.exists())
        self.assertEqual(self.journal().read_bytes(), journal)

    def test_nonempty_unverified_destination_is_preserved_without_downloading(self):
        self.completed_update()
        self.lose_container_program()
        self.program.mkdir()
        unknown = self.program / "operator-file"
        unknown.write_bytes(b"preserve these unverified bytes")
        resolve, stage = self.assert_refused()
        resolve.assert_not_called()
        stage.assert_not_called()
        self.assertEqual(unknown.read_bytes(), b"preserve these unverified bytes")

    def test_broken_journal_never_grants_initial_install_authority(self):
        self.completed_update()
        self.lose_container_program()
        journal = self.journal()
        journal.write_text("{interrupted write")
        resolve, stage = self.assert_refused()
        resolve.assert_not_called()
        stage.assert_not_called()
        self.assertEqual(journal.read_text(), "{interrupted write")
        self.assertFalse(self.program.exists())

    def test_interrupted_or_recovery_journal_never_grants_initial_install_authority(
        self,
    ):
        self.completed_update()
        self.lose_container_program()
        journal = self.journal()
        completed = json.loads(journal.read_text())
        for state in ("active", "recovery_required"):
            with self.subTest(state=state):
                journal.write_text(json.dumps({**completed, "state": state}))
                before = journal.read_bytes()
                resolve, stage = self.assert_refused()
                resolve.assert_not_called()
                stage.assert_not_called()
                self.assertEqual(journal.read_bytes(), before)
                self.assertFalse(self.program.exists())

    def test_prelaunch_restored_journal_cannot_reinstall_beta6_after_container_loss(
        self,
    ):
        with self.assertRaises(update.NativeUpdateError):
            self.completed_update(prepare=lambda: False)
        self.assertEqual(json.loads(self.journal().read_text())["state"], "restored")
        self.lose_container_program()
        resolve, stage = self.assert_refused()
        resolve.assert_not_called()
        stage.assert_not_called()
        self.assertFalse(self.program.exists())

    def test_changed_data_scope_does_not_turn_a_receipt_into_initial_install(self):
        self.completed_update()
        self.lose_container_program()
        self.service["v2_data_dir"] = str(self.root / "different-data")
        resolve, stage = self.assert_refused()
        resolve.assert_not_called()
        stage.assert_not_called()
        self.assertFalse(self.program.exists())

    def test_receipt_is_rechecked_after_staging_before_program_publication(self):
        self.completed_update()
        self.lose_container_program()

        def change_receipt():
            journal = self.journal()
            record = json.loads(journal.read_text())
            record["state"] = "recovery_required"
            journal.write_text(json.dumps(record))

        self.during_staging = change_receipt
        self.assert_refused()
        self.assertFalse(self.program.exists())
        self.assertTrue(self.staged.is_dir())
        self.assertEqual(
            json.loads(self.journal().read_text())["state"], "recovery_required"
        )

    def test_new_destination_during_staging_is_preserved_without_publication(self):
        self.completed_update()
        self.lose_container_program()

        def occupy_destination():
            self.program.mkdir()
            self.program.joinpath("operator-file").write_bytes(b"concurrent data")

        self.during_staging = occupy_destination
        self.assert_refused()
        self.assertEqual(
            self.program.joinpath("operator-file").read_bytes(), b"concurrent data"
        )
        self.assertTrue(self.staged.is_dir())

    def test_replaced_empty_destination_is_not_removed(self):
        self.completed_update()
        self.lose_container_program()
        self.program.mkdir()

        def replace_destination():
            self.program.rename(self.root / "retained-empty-destination")
            self.program.mkdir()

        self.during_staging = replace_destination
        self.assert_refused()
        self.assertTrue(self.program.is_dir())
        self.assertTrue(self.root.joinpath("retained-empty-destination").is_dir())
        self.assertTrue(self.staged.is_dir())

    def test_no_journal_preserves_initial_beta6_preparation_for_latest(self):
        result, initial, resolve, stage = self.ensure()
        self.assertEqual(result, (True, None))
        initial.assert_called_once_with(
            self.handler,
            "InfiniDysk",
            {**self.service, "v2_release_version": runtime.RELEASE},
        )
        resolve.assert_not_called()
        stage.assert_not_called()
        self.assertFalse(self.journals.exists())

    def test_no_journal_cannot_install_an_unprepared_newer_release(self):
        self.service["v2_release_version"] = "v2.0.0-beta.10"
        resolve, stage = self.assert_refused()
        resolve.assert_not_called()
        stage.assert_not_called()
        self.assertFalse(self.program.exists())


if __name__ == "__main__":
    unittest.main()
