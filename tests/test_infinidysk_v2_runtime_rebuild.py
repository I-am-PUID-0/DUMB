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
            "release_version": "latest",
            "release_version_enabled": False,
        }
        self.handler = SimpleNamespace(logger=Mock())
        self.uid = os.geteuid() or 1000
        self.gid = os.getegid() or 1000
        self.accepted = None
        self.plan_changes = {}
        self.metadata_changes = {}
        self.during_staging = None
        self.staged = None
        self.fresh = False
        self.candidate_bytes = b"accepted beta10 program"

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
        self.service["release_version"] = original_selector
        self.service["release_version_enabled"] = original_selector != "latest"
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
        paths = list(self.journals.glob("native-runtime-*.json"))
        self.assertEqual(len(paths), 1)
        return paths[0]

    def resolve(self, service):
        if self.fresh:
            self.assertEqual(
                releases.selected_release(service),
                releases.selected_release(self.service),
            )
        else:
            self.assertEqual(service["release_version"], self.accepted["release"])
            self.assertTrue(service["release_version_enabled"])
        self.assertEqual(self.service["v2_runtime_dir"], service["v2_runtime_dir"])
        return {**self.accepted, **self.plan_changes}

    @contextmanager
    def stage(self, service, plan, *, uid, gid):
        if self.fresh:
            self.assertEqual(
                releases.selected_release(service),
                releases.selected_release(self.service),
            )
        else:
            self.assertEqual(service["release_version"], self.accepted["release"])
            self.assertTrue(service["release_version_enabled"])
        self.assertEqual((uid, gid), (self.uid, self.gid))
        staging = self.root / "restore-staging"
        staging.mkdir()
        self.staged = staging / "candidate"
        metadata = self.artifact(
            self.staged, self.accepted["release"], self.candidate_bytes
        )
        self.assertEqual(plan, {**self.accepted, **self.plan_changes})
        if self.during_staging is not None:
            self.during_staging()
        yield self.staged, {**metadata, **self.metadata_changes}

    def ensure(self, *, resolver=None, prepare_candidate=False, replace_verified=False):
        selection = copy.deepcopy(self.service)
        database = self.database.read_bytes() if self.database.exists() else None
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
            if prepare_candidate:
                result = runtime.install_selected_v2_runtime(
                    self.handler,
                    "InfiniDysk",
                    self.service,
                    replace_verified=replace_verified,
                )
            else:
                result = runtime.ensure_active_v2_runtime(
                    self.handler, "InfiniDysk", self.service
                )
        self.assertEqual(self.service, selection)
        self.assertEqual(
            self.database.read_bytes() if self.database.exists() else None, database
        )
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
        self.assertEqual(self.service["release_version"], runtime.RELEASE)
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

    def prepare_fresh_install(self):
        self.fresh = True
        self.database.unlink()
        self.accepted = self.artifact(
            self.root / "official-release-fixture",
            "v2.0.0-beta.10",
            b"accepted beta10 program",
        )

    def test_fresh_latest_installs_selected_release_and_remembers_it(self):
        from utils.infinidysk_v2_installed_release import read_installed_release

        self.prepare_fresh_install()
        result, initial, resolve, stage = self.ensure()
        self.assertEqual(result, (True, None))
        initial.assert_not_called()
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(stage.call_count, 1)
        self.assertEqual(
            read_installed_release(self.service, self.journals), self.accepted
        )
        self.assertEqual(list(self.journals.glob("native-runtime-*.json")), [])

    def test_fresh_exact_release_uses_standard_selector(self):
        self.prepare_fresh_install()
        self.service["release_version"] = "v2.0.0-beta.10"
        self.service["release_version_enabled"] = True
        result, initial, resolve, stage = self.ensure()
        self.assertEqual(result, (True, None))
        initial.assert_not_called()
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(stage.call_count, 1)

    def test_fresh_retry_can_replace_verified_inactive_preparation_program(self):
        self.prepare_fresh_install()
        self.artifact(self.program, runtime.RELEASE, b"previous fresh preparation")
        result, initial, resolve, stage = self.ensure(
            prepare_candidate=True, replace_verified=True
        )
        self.assertEqual(result, (True, None))
        initial.assert_not_called()
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(stage.call_count, 1)
        self.assertEqual(
            self.program.joinpath("infinidysk").read_bytes(), b"accepted beta10 program"
        )
        self.assertEqual(list(self.root.glob(".program.fresh-previous-*")), [])

    def test_existing_program_is_not_replaced_without_fresh_preparation_authority(self):
        self.prepare_fresh_install()
        self.artifact(self.program, runtime.RELEASE, b"previous fresh preparation")
        result, initial, _resolve, stage = self.ensure(prepare_candidate=True)
        self.assertFalse(result[0])
        initial.assert_not_called()
        stage.assert_not_called()
        self.assertEqual(
            self.program.joinpath("infinidysk").read_bytes(),
            b"previous fresh preparation",
        )

    def test_fresh_retry_publish_failure_restores_exact_previous_program(self):
        from utils import infinidysk_v2_space_control as space

        self.prepare_fresh_install()
        self.artifact(self.program, runtime.RELEASE, b"previous fresh preparation")
        rename = space.rename_exclusive

        def fail_publish(source, target, *identities):
            if source == self.staged:
                raise ValueError("fixture fresh publication failure")
            return rename(source, target, *identities)

        with patch.object(space, "rename_exclusive", side_effect=fail_publish):
            result, initial, _resolve, _stage = self.ensure(
                prepare_candidate=True, replace_verified=True
            )
        self.assertFalse(result[0])
        initial.assert_not_called()
        self.assertEqual(
            self.program.joinpath("infinidysk").read_bytes(),
            b"previous fresh preparation",
        )
        self.assertEqual(list(self.root.glob(".program.fresh-previous-*")), [])

    def prepare_legacy_install(self, *, release="v2.0.0-beta.10", migrated=False):
        """Retain old native data but no new installed-release/update record."""
        self.fresh = True
        self.candidate_bytes = ("official " + release + " program").encode()
        self.accepted = self.artifact(
            self.root / "official-release-fixture", release, self.candidate_bytes
        )
        if migrated:
            legacy = Path(self.service["config_dir"])
            legacy.mkdir()
            (legacy / ".dumb_infinidysk_install.json").write_text(
                json.dumps({"release": "v1.7.1"})
            )

    def test_legacy_fresh_v2_data_uses_latest_without_migration_history(self):
        from utils.infinidysk_v2_installed_release import read_installed_release

        self.prepare_legacy_install()
        self.assertFalse(self.journals.exists())
        result, initial, resolve, stage = self.ensure()
        self.assertEqual(result, (True, None))
        initial.assert_not_called()
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(stage.call_count, 1)
        self.assertEqual(
            read_installed_release(self.service, self.journals), self.accepted
        )
        self.assertEqual(list(self.journals.glob("native-runtime-*.json")), [])

    def test_legacy_migrated_v2_data_uses_configured_exact_beta6(self):
        from utils.infinidysk_v2_installed_release import read_installed_release

        self.prepare_legacy_install(release=runtime.RELEASE, migrated=True)
        self.service["release_version"] = runtime.RELEASE
        self.service["release_version_enabled"] = True
        legacy_receipt = (
            Path(self.service["config_dir"]) / ".dumb_infinidysk_install.json"
        )
        before = legacy_receipt.read_bytes()
        result, initial, resolve, stage = self.ensure()
        self.assertEqual(result, (True, None))
        initial.assert_not_called()
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(stage.call_count, 1)
        self.assertEqual(legacy_receipt.read_bytes(), before)
        self.assertEqual(
            read_installed_release(self.service, self.journals), self.accepted
        )
        self.assertEqual(
            runtime.installed_active_v2_version(self.service), runtime.RELEASE
        )

    def test_legacy_existing_data_allows_verified_empty_program_destination(self):
        self.prepare_legacy_install()
        self.program.mkdir()
        self.assertEqual(self.ensure()[0], (True, None))
        self.assertEqual(
            self.program.joinpath("infinidysk").read_bytes(), self.candidate_bytes
        )

    def test_legacy_existing_data_preserves_unverified_program_without_download(self):
        self.prepare_legacy_install()
        self.program.mkdir()
        unknown = self.program / "operator-file"
        unknown.write_bytes(b"preserve unverified program directory")
        resolve, stage = self.assert_refused()
        resolve.assert_not_called()
        stage.assert_not_called()
        self.assertEqual(unknown.read_bytes(), b"preserve unverified program directory")
        self.assertFalse(self.journals.exists())

    def test_legacy_rebuild_records_release_then_restores_exact_bytes_next_time(self):
        from utils.infinidysk_v2_installed_release import read_installed_release

        self.prepare_legacy_install()
        self.assertEqual(self.ensure()[0], (True, None))
        accepted = read_installed_release(self.service, self.journals)
        self.fresh = False
        self.lose_container_program()
        self.root.joinpath("restore-staging").rmdir()
        # The compatibility bootstrap runs once. A subsequent stale pin must
        # not override the now-persistent accepted native release.
        self.service["release_version"] = runtime.RELEASE
        self.service["release_version_enabled"] = True
        self.assertEqual(self.ensure()[0], (True, None))
        self.assertEqual(read_installed_release(self.service, self.journals), accepted)
        self.assertEqual(
            runtime.installed_active_v2_version(self.service), "v2.0.0-beta.10"
        )
        self.assertEqual(
            self.program.joinpath("infinidysk").read_bytes(), self.candidate_bytes
        )

    def test_legacy_record_after_bootstrap_cannot_be_bypassed_when_invalid(self):
        self.prepare_legacy_install()
        self.assertEqual(self.ensure()[0], (True, None))
        self.fresh = False
        self.lose_container_program()
        record = next(self.journals.glob("installed-release-*.json"))
        record.write_text("{interrupted installed-release write")
        resolve, stage = self.assert_refused()
        resolve.assert_not_called()
        stage.assert_not_called()
        self.assertEqual(record.read_text(), "{interrupted installed-release write")
        self.assertFalse(self.program.exists())

    def test_legacy_release_download_failure_preserves_existing_data(self):
        self.prepare_legacy_install()

        def unavailable(_service):
            raise releases.NativeReleaseError("fixture official release unavailable")

        resolve, stage = self.assert_refused(resolver=unavailable)
        self.assertEqual(resolve.call_count, 1)
        stage.assert_not_called()
        self.assertFalse(self.program.exists())
        self.assertFalse(self.journals.exists())

    def test_legacy_record_write_failure_blocks_startup_until_record_can_be_saved(self):
        from utils.infinidysk_v2_installed_release import read_installed_release

        self.prepare_legacy_install()
        with patch(
            "utils.infinidysk_v2_installed_release.record_installed_release",
            side_effect=OSError("fixture installed-release write unavailable"),
        ):
            result, initial, resolve, stage = self.ensure()
        self.assertFalse(result[0])
        initial.assert_not_called()
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(stage.call_count, 1)
        self.assertIsNone(read_installed_release(self.service, self.journals))
        self.assertEqual(
            self.program.joinpath("infinidysk").read_bytes(), self.candidate_bytes
        )
        result, initial, resolve, stage = self.ensure()
        self.assertEqual(result, (True, None))
        initial.assert_not_called()
        resolve.assert_not_called()
        stage.assert_not_called()
        self.assertEqual(
            read_installed_release(self.service, self.journals), self.accepted
        )

    def test_completed_update_survives_normal_key_and_config_changes(self):
        self.service["env"] = {"FRONTEND_BACKEND_API_KEY": "old-fixture-key"}
        self.completed_update()
        self.lose_container_program()
        self.service["env"] = {"INFINIDYSK__SAB__API_KEY": "new-fixture-key"}
        self.service["log_level"] = "DEBUG"
        result, initial, _resolve, _stage = self.ensure()
        self.assertEqual(result, (True, None))
        initial.assert_not_called()

    def test_legacy_completed_receipt_can_bootstrap_normal_record_after_key_change(
        self,
    ):
        from utils.infinidysk_v2_installed_release import read_installed_release

        self.completed_update()
        for path in self.journals.glob("installed-release-*.json"):
            path.unlink()
        self.lose_container_program()
        self.service["env"] = {"INFINIDYSK__SAB__API_KEY": "fixture-new-key"}
        self.assertEqual(self.ensure()[0], (True, None))
        self.assertEqual(
            read_installed_release(self.service, self.journals), self.accepted
        )

    def test_custom_default_data_root_survives_start_and_container_replacement(self):
        from utils.infinidysk_v2_installed_release import read_installed_release

        self.prepare_fresh_install()
        self.service.pop("v2_data_dir")
        self.assertEqual(self.ensure()[0], (True, None))
        self.assertEqual(
            read_installed_release(
                self.service,
                self.journals,
                data_root=str(self.root / "default-data"),
            ),
            self.accepted,
        )
        self.assertEqual(self.ensure()[0], (True, None))
        self.fresh = False
        self.lose_container_program()
        self.root.joinpath("restore-staging").rmdir()
        self.assertEqual(self.ensure()[0], (True, None))

    def test_fresh_v2_rebuild_has_no_dependency_on_migration_or_update_journal(self):
        self.prepare_fresh_install()
        self.assertTrue(self.ensure()[0][0])
        self.fresh = False
        self.lose_container_program()
        self.root.joinpath("restore-staging").rmdir()
        self.database.write_bytes(b"native writes after first startup")
        self.service["env"] = {"INFINIDYSK__SAB__API_KEY": "fixture-rotation"}
        # Even a newly selected older pin cannot downgrade the existing database
        # during ordinary startup. A release change requires the update flow.
        self.service["release_version"] = runtime.RELEASE
        self.service["release_version_enabled"] = True
        self.assertEqual(self.ensure()[0], (True, None))
        self.assertEqual(list(self.journals.glob("native-runtime-*.json")), [])


if __name__ == "__main__":
    unittest.main()
