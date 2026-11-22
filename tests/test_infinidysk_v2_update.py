"""Native runtime publication must not roll an upgraded database backwards."""

import hashlib
from contextvars import copy_context
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from utils.infinidysk_v2_runtime import MARKER
from utils.infinidysk_v2_update import (
    NativeUpdateError,
    NativeUpdateRecoveryRequired,
    native_update_state,
    perform_native_update,
    resume_native_update,
    validate_native_update_launch,
    native_runtime_restore_metadata,
)
from utils.infinidysk_v2_installed_release import (
    read_installed_release,
    record_installed_release,
)


class NativeRuntimeUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / "runtime"
        self.staging = self.root / "staging"
        self.staging.mkdir()
        self.candidate = self.staging / "candidate"
        self.journals = self.root / "updates"
        self.service = {"v2_runtime_dir": str(self.runtime)}
        self.old = self.artifact(self.runtime, "v2.0.0-beta.6", b"old program")
        self.new = self.artifact(self.candidate, "v2.0.0-beta.7", b"new program")
        self.events = []

    @staticmethod
    def artifact(path, release, contents):
        path.mkdir()
        (path / "infinidysk").write_bytes(contents)
        (path / "infinidysk").chmod(0o755)
        (path / "LICENSE").write_text("example license")
        (path / "version.txt").write_text(release.removeprefix("v"))
        (path / "commit.txt").write_text("a" * 40)
        metadata = {
            "format": "official-v2-release-v1",
            "release": release,
            "commit": "a" * 40,
            "architecture": "arm64",
            "archive_sha256": "b" * 64,
            "binary_sha256": hashlib.sha256(contents).hexdigest(),
        }
        (path / MARKER).write_text(json.dumps(metadata))
        return metadata

    def callbacks(self, **overrides):
        def event(name):
            return lambda *args: self.events.append(name)

        values = {
            "stop_current": event("stop"),
            "prepare_launch": event("prepare"),
            "start_candidate": event("start"),
            "wait_ready": event("ready"),
            "save_selection": event("save"),
            "verify_admission": event("admission"),
            "retire_rollback": event("retire"),
        }
        values.update(overrides)
        return values

    def update(self, **overrides):
        was_running = overrides.pop("was_running", True)
        target_selector = overrides.pop("target_selector", None)
        return perform_native_update(
            self.service,
            self.candidate,
            self.new,
            journal_root=self.journals,
            was_running=was_running,
            target_selector=target_selector,
            **self.callbacks(**overrides),
        )

    def journal(self):
        paths = list(self.journals.glob("native-runtime-*.json"))
        self.assertEqual(len(paths), 1)
        return paths[0], json.loads(paths[0].read_text())

    def test_success_retains_old_runtime_and_journals_before_launch(self):
        def start():
            state = native_update_state(self.service, self.journals)
            self.assertTrue(state["launched"])
            self.assertTrue(state["recovery_required"])
            self.events.append("start")

        receipt = self.update(start_candidate=start)
        self.assertEqual(receipt["state"], "completed")
        self.assertEqual(
            self.runtime.joinpath("infinidysk").read_bytes(), b"new program"
        )
        previous = Path(receipt["previous_runtime"])
        self.assertEqual(previous.joinpath("infinidysk").read_bytes(), b"old program")
        self.assertLess(self.events.index("retire"), self.events.index("start"))
        self.assertLess(self.events.index("ready"), self.events.index("save"))
        path, saved = self.journal()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(saved["state"], "completed")
        self.assertFalse(
            native_update_state(self.service, self.journals)["recovery_required"]
        )
        self.assertFalse(self.candidate.exists())

    def test_prelaunch_failure_restores_only_owned_runtime_and_never_starts(self):
        prepare = Mock(side_effect=ValueError("fixture prepare failure"))
        start = Mock()
        with self.assertRaises(NativeUpdateError) as caught:
            self.update(prepare_launch=prepare, start_candidate=start)
        self.assertFalse(caught.exception.launched)
        self.assertFalse(caught.exception.recovery_required)
        self.assertEqual(caught.exception.stage, "prepare_launch")
        self.assertEqual(
            self.runtime.joinpath("infinidysk").read_bytes(), b"old program"
        )
        self.assertEqual(
            self.candidate.joinpath("infinidysk").read_bytes(), b"new program"
        )
        start.assert_not_called()
        self.assertEqual(self.journal()[1]["state"], "restored")

    def test_failure_after_launch_never_replaces_new_runtime_with_old_binary(self):
        for callback in ("start_candidate", "wait_ready", "save_selection"):
            with self.subTest(callback=callback):
                if self.journals.exists():
                    shutil.rmtree(self.journals)
                if not self.candidate.exists():
                    self.artifact(self.candidate, "v2.0.0-beta.7", b"new program")
                failure = Mock(
                    side_effect=ValueError("private failure must not be public")
                )
                with self.assertRaises(NativeUpdateRecoveryRequired) as caught:
                    self.update(**{callback: failure})
                self.assertTrue(caught.exception.launched)
                self.assertTrue(caught.exception.recovery_required)
                self.assertEqual(
                    self.runtime.joinpath("infinidysk").read_bytes(), b"new program"
                )
                self.assertTrue(
                    Path(caught.exception.receipt["previous_runtime"]).is_dir()
                )
                self.assertNotIn("private failure", self.journal()[0].read_text())
                blocked = Mock()
                with self.assertRaises(NativeUpdateRecoveryRequired):
                    self.update(stop_current=blocked)
                blocked.assert_not_called()

    def test_false_callback_results_are_failures(self):
        for result in (False, (False, "fixture readiness failure")):
            with self.subTest(result=result):
                if self.journals.exists():
                    shutil.rmtree(self.journals)
                if not self.candidate.exists():
                    self.artifact(self.candidate, "v2.0.0-beta.7", b"new program")
                with self.assertRaises(NativeUpdateRecoveryRequired):
                    self.update(wait_ready=lambda: result)

    def test_admission_loss_after_stop_preserves_old_runtime(self):
        count = 0

        def admission():
            nonlocal count
            count += 1
            if "stop" in self.events:
                raise ValueError("fixture admission lost")

        start = Mock()
        with self.assertRaises(NativeUpdateError) as caught:
            self.update(verify_admission=admission, start_candidate=start)
        self.assertGreaterEqual(count, 2)
        self.assertFalse(caught.exception.recovery_required)
        self.assertEqual(
            self.runtime.joinpath("infinidysk").read_bytes(), b"old program"
        )
        start.assert_not_called()

    def test_changed_published_runtime_is_retained_for_manual_recovery(self):
        def replace_runtime():
            self.runtime.rename(self.root / "moved-new-runtime")
            self.runtime.mkdir()
            (self.runtime / "foreign").write_bytes(b"preserve")
            raise ValueError("fixture runtime changed")

        with self.assertRaises(NativeUpdateRecoveryRequired) as caught:
            self.update(prepare_launch=replace_runtime)
        self.assertFalse(caught.exception.launched)
        self.assertTrue(caught.exception.recovery_required)
        self.assertEqual((self.runtime / "foreign").read_bytes(), b"preserve")
        self.assertEqual(
            Path(caught.exception.receipt["previous_runtime"])
            .joinpath("infinidysk")
            .read_bytes(),
            b"old program",
        )

    def test_stage_replacement_before_publication_cannot_be_installed(self):
        def replace_stage():
            self.candidate.rename(self.staging / "original")
            self.artifact(self.candidate, "v2.0.0-beta.7", b"replacement")

        with self.assertRaises(NativeUpdateError):
            self.update(stop_current=replace_stage)
        self.assertEqual(
            self.runtime.joinpath("infinidysk").read_bytes(), b"old program"
        )

    def test_modified_candidate_bytes_or_marker_are_rejected_before_stop(self):
        stop = Mock()
        (self.candidate / "infinidysk").write_bytes(b"changed")
        with self.assertRaises(NativeUpdateError):
            self.update(stop_current=stop)
        stop.assert_not_called()
        self.assertEqual(
            self.runtime.joinpath("infinidysk").read_bytes(), b"old program"
        )

    def test_linked_or_foreign_journals_are_never_overwritten(self):
        self.update()
        path, _record = self.journal()
        protected = self.root / "protected"
        protected.write_text("preserve")
        path.unlink()
        path.symlink_to(protected)
        if not self.candidate.exists():
            self.artifact(self.candidate, "v2.0.0-beta.7", b"new program")
        with self.assertRaises(NativeUpdateRecoveryRequired):
            self.update()
        self.assertEqual(protected.read_text(), "preserve")
        state = native_update_state(self.service, self.journals)
        self.assertTrue(state["recovery_required"])

    def test_unknown_interrupted_state_blocks_without_guessing_rollback(self):
        receipt = self.update()
        path, record = self.journal()
        record.update(state="active", stage="candidate_published", launched=False)
        path.write_text(json.dumps(record))
        self.artifact(self.candidate, "v2.0.0-beta.7", b"new program")
        stop = Mock()
        with self.assertRaises(NativeUpdateRecoveryRequired):
            self.update(stop_current=stop)
        stop.assert_not_called()
        self.assertTrue(Path(receipt["previous_runtime"]).exists())

    def test_missing_journal_is_not_a_recovery_block(self):
        self.assertIsNone(native_update_state(self.service, self.journals))

    def test_changed_published_bytes_are_refused_before_launch(self):
        start = Mock()

        def change():
            self.runtime.joinpath("infinidysk").write_bytes(b"changed program")

        with self.assertRaises(NativeUpdateError) as caught:
            self.update(prepare_launch=change, start_candidate=start)
        self.assertFalse(caught.exception.launched)
        self.assertFalse(caught.exception.recovery_required)
        self.assertEqual(
            self.runtime.joinpath("infinidysk").read_bytes(), b"old program"
        )
        start.assert_not_called()

    def test_changed_previous_binary_is_not_declared_safe_to_restart(self):
        def change_previous():
            self.runtime.joinpath("infinidysk").write_bytes(b"changed original")

        with self.assertRaises(NativeUpdateRecoveryRequired) as caught:
            self.update(stop_current=change_previous)
        self.assertFalse(caught.exception.launched)
        self.assertTrue(caught.exception.recovery_required)
        self.assertEqual(
            self.runtime.joinpath("infinidysk").read_bytes(), b"changed original"
        )

    def test_fsync_failure_after_rename_restores_using_actual_owned_identities(self):
        from utils.infinidysk_v2_space_control import rename_exclusive

        def moved_then_failed(source, destination, *args):
            rename_exclusive(source, destination, *args)
            if Path(source) == self.runtime:
                raise OSError("fixture fsync failure after completed first rename")

        with patch(
            "utils.infinidysk_v2_update.rename_exclusive", side_effect=moved_then_failed
        ):
            with self.assertRaises(NativeUpdateError) as caught:
                self.update()
        self.assertFalse(caught.exception.recovery_required)
        self.assertEqual(
            self.runtime.joinpath("infinidysk").read_bytes(), b"old program"
        )

    def test_later_update_keeps_the_previous_terminal_receipt_and_runtime(self):
        first = self.update()
        self.new = self.artifact(self.candidate, "v2.0.0-beta.8", b"newer program")
        second = self.update()
        histories = list(self.journals.glob("*.history"))
        self.assertEqual(len(histories), 1)
        self.assertEqual(json.loads(histories[0].read_text())["id"], first["id"])
        self.assertTrue(Path(first["previous_runtime"]).exists())
        self.assertTrue(Path(second["previous_runtime"]).exists())
        self.assertEqual(
            self.runtime.joinpath("infinidysk").read_bytes(), b"newer program"
        )

    def test_public_journal_never_exposes_original_callback_failure(self):
        with self.assertRaises(NativeUpdateRecoveryRequired):
            self.update(
                wait_ready=Mock(side_effect=ValueError("password=private-example"))
            )
        state = native_update_state(self.service, self.journals)
        self.assertNotIn("private-example", json.dumps(state))
        self.assertNotIn("private-example", self.journal()[0].read_text())

    def failed_after_launch(self):
        with self.assertRaises(NativeUpdateRecoveryRequired) as caught:
            self.update(wait_ready=lambda: False)
        return caught.exception.receipt

    def resume(self, **overrides):
        callbacks = self.callbacks(**overrides)
        callbacks.pop("stop_current")
        callbacks.pop("retire_rollback")
        return resume_native_update(
            self.service, journal_root=self.journals, **callbacks
        )

    def test_explicit_resume_verifies_and_starts_only_the_retained_replacement(self):
        failed = self.failed_after_launch()
        self.events.clear()
        selected = []

        def start():
            self.assertEqual(
                self.runtime.joinpath("infinidysk").read_bytes(), b"new program"
            )
            self.events.append("start")

        result = self.resume(start_candidate=start, save_selection=selected.append)
        self.assertEqual(result["id"], failed["id"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(selected, [self.new])
        self.assertNotIn("stop", self.events)
        self.assertNotIn("retire", self.events)
        self.assertEqual(
            Path(result["previous_runtime"]).joinpath("infinidysk").read_bytes(),
            b"old program",
        )
        self.assertFalse(
            native_update_state(self.service, self.journals)["recovery_required"]
        )

    def test_resume_cannot_accept_replaced_or_modified_new_runtime(self):
        self.failed_after_launch()
        self.runtime.joinpath("infinidysk").write_bytes(b"changed binary")
        start = Mock()
        with self.assertRaises(NativeUpdateRecoveryRequired):
            self.resume(start_candidate=start)
        start.assert_not_called()
        self.assertTrue(
            native_update_state(self.service, self.journals)["recovery_required"]
        )

    def test_resume_cannot_follow_changed_retained_old_runtime(self):
        failed = self.failed_after_launch()
        Path(failed["previous_runtime"]).joinpath("infinidysk").write_bytes(
            b"changed old binary"
        )
        start = Mock()
        with self.assertRaises(NativeUpdateRecoveryRequired):
            self.resume(start_candidate=start)
        start.assert_not_called()

    def test_resume_failure_remains_held_with_the_same_replacement(self):
        failed = self.failed_after_launch()
        with self.assertRaises(NativeUpdateRecoveryRequired) as caught:
            self.resume(wait_ready=lambda: False)
        self.assertEqual(caught.exception.receipt["id"], failed["id"])
        self.assertTrue(caught.exception.launched)
        self.assertEqual(
            self.runtime.joinpath("infinidysk").read_bytes(), b"new program"
        )

    def test_interrupted_prelaunch_record_cannot_be_resumed_as_a_running_update(self):
        self.failed_after_launch()
        path, record = self.journal()
        record["launched"] = False
        path.write_text(json.dumps(record))
        start = Mock()
        with self.assertRaises(NativeUpdateRecoveryRequired):
            self.resume(start_candidate=start)
        start.assert_not_called()

    def test_running_intent_survives_failed_update_and_explicit_resume(self):
        with self.assertRaises(NativeUpdateRecoveryRequired):
            self.update(was_running=False, wait_ready=lambda: False)
        state = native_update_state(self.service, self.journals)
        self.assertIs(state["was_running"], False)
        resumed = self.resume()
        self.assertIs(resumed["was_running"], False)

    def test_missing_or_unverified_running_intent_cannot_be_resumed(self):
        self.failed_after_launch()
        path, record = self.journal()
        record.pop("was_running")
        path.write_text(json.dumps(record))
        start = Mock()
        with self.assertRaises(NativeUpdateRecoveryRequired):
            self.resume(start_candidate=start)
        start.assert_not_called()

    def test_resume_refuses_a_different_data_or_provider_configuration(self):
        self.failed_after_launch()
        start = Mock()
        self.service["v2_data_dir"] = str(self.root / "different-data")
        with self.assertRaises(NativeUpdateRecoveryRequired):
            self.resume(start_candidate=start)
        start.assert_not_called()

    def test_new_journal_binds_standard_controls_separately_from_service_scope(self):
        self.service.update(
            release_version="v2.0.0-beta.6", release_version_enabled=True
        )
        self.failed_after_launch()
        _path, record = self.journal()
        self.assertEqual(record["service_fingerprint_version"], 2)
        self.service.update(
            release_version="v2.0.0-beta.7", release_version_enabled=True
        )
        self.assertEqual(self.resume()["state"], "completed")

    def test_invalid_fingerprint_versions_do_not_authorize_retry(self):
        self.failed_after_launch()
        path, record = self.journal()
        for version in (True, 0, 3, "2"):
            with self.subTest(version=version):
                record["service_fingerprint_version"] = version
                path.write_text(json.dumps(record))
                start = Mock()
                with self.assertRaises(NativeUpdateRecoveryRequired):
                    self.resume(start_candidate=start)
                start.assert_not_called()

    def test_legacy_completed_journal_does_not_block_verified_runtime_or_new_update(
        self,
    ):
        self.service.update(release_version="rc", release_version_enabled=False)
        self.update()
        path, record = self.journal()
        record.pop("service_fingerprint_version")
        record["service_fingerprint"] = hashlib.sha256(
            json.dumps(
                self.service, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        path.write_text(json.dumps(record))
        self.service["release_version"] = "latest"
        self.assertTrue(validate_native_update_launch(self.service, self.journals))
        self.assertFalse(
            native_update_state(self.service, self.journals)["recovery_required"]
        )
        self.new = self.artifact(self.candidate, "v2.0.0-beta.8", b"newer program")
        result = self.update()
        self.assertEqual(result["service_fingerprint_version"], 2)
        self.assertEqual(result["state"], "completed")
        history = list(self.journals.glob("*.history"))
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0].read_text()), record)

    def test_resume_allows_only_a_selector_save_before_final_journal_commit(self):
        self.failed_after_launch()
        self.service["release_version"] = "v2.0.0-beta.7"
        self.service["release_version_enabled"] = True
        result = self.resume()
        self.assertEqual(result["state"], "completed")

    def test_manual_latest_override_survives_failed_update_and_retry(self):
        self.service["release_version"] = "v2.0.0-beta.6"
        self.service["release_version_enabled"] = True
        with self.assertRaises(NativeUpdateRecoveryRequired):
            self.update(target_selector="latest", wait_ready=lambda: False)
        self.assertEqual(
            native_update_state(self.service, self.journals)["target_selector"],
            "latest",
        )
        self.assertEqual(self.service["release_version"], "v2.0.0-beta.6")
        result = self.resume()
        self.assertEqual(result["target_selector"], "latest")

    def test_unrelated_release_selector_edit_cannot_retarget_resume(self):
        self.failed_after_launch()
        self.service["release_version"] = "v2.0.0-beta.8"
        self.service["release_version_enabled"] = True
        start = Mock()
        with self.assertRaises(NativeUpdateRecoveryRequired):
            self.resume(start_candidate=start)
        start.assert_not_called()

    def test_completed_update_allows_only_its_accepted_new_runtime_offline(self):
        result = self.update()
        self.assertTrue(validate_native_update_launch(self.service, self.journals))
        self.runtime.rename(self.root / "accepted-new-runtime")
        Path(result["previous_runtime"]).rename(self.runtime)
        with self.assertRaises(NativeUpdateRecoveryRequired):
            validate_native_update_launch(self.service, self.journals)

    def test_completed_update_rejects_old_bytes_and_self_consistent_marker_in_same_directory(
        self,
    ):
        result = self.update()
        old = Path(result["previous_runtime"])
        for name in ("infinidysk", MARKER, "version.txt", "commit.txt", "LICENSE"):
            shutil.copyfile(old / name, self.runtime / name)
        with self.assertRaises(NativeUpdateRecoveryRequired):
            validate_native_update_launch(self.service, self.journals)

    def test_interrupted_prelaunch_record_blocks_ordinary_cold_boot(self):
        self.failed_after_launch()
        path, record = self.journal()
        record.update(state="active", stage="prepare_launch", launched=False)
        path.write_text(json.dumps(record))
        with self.assertRaises(NativeUpdateRecoveryRequired):
            validate_native_update_launch(self.service, self.journals)

    def test_verified_prelaunch_restoration_allows_only_the_captured_old_runtime(self):
        with self.assertRaises(NativeUpdateError):
            self.update(prepare_launch=lambda: False)
        self.assertTrue(validate_native_update_launch(self.service, self.journals))
        self.runtime.joinpath("infinidysk").write_bytes(b"changed original")
        with self.assertRaises(NativeUpdateRecoveryRequired):
            validate_native_update_launch(self.service, self.journals)

    def test_explicit_update_and_resume_own_only_their_synchronous_launch_callbacks(
        self,
    ):
        guard = lambda: validate_native_update_launch(self.service, self.journals)
        with self.assertRaises(NativeUpdateRecoveryRequired):
            self.update(
                prepare_launch=guard, start_candidate=guard, wait_ready=lambda: False
            )
        with self.assertRaises(NativeUpdateRecoveryRequired):
            guard()

        def start():
            self.assertTrue(guard())
            blocked = []

            def unrelated_thread():
                try:
                    guard()
                except NativeUpdateRecoveryRequired:
                    blocked.append(True)

            worker = threading.Thread(target=unrelated_thread)
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(blocked, [True])

        self.resume(prepare_launch=guard, start_candidate=start, wait_ready=guard)
        self.assertTrue(guard())

    def test_absent_journal_preserves_initial_install_admission(self):
        self.assertTrue(validate_native_update_launch(self.service, self.journals))

    def test_identical_accepted_artifact_can_be_restored_to_a_new_container_inode(self):
        self.update()
        self.runtime.rename(self.root / "previous-container-runtime")
        shutil.copytree(self.root / "previous-container-runtime", self.runtime)
        self.assertTrue(validate_native_update_launch(self.service, self.journals))

    def test_unreadable_journal_never_becomes_absent_launch_authority(self):
        self.update()
        path, _record = self.journal()
        path.write_text("{incomplete")
        with self.assertRaises(NativeUpdateRecoveryRequired):
            validate_native_update_launch(self.service, self.journals)

    def test_copied_async_context_cannot_keep_update_launch_authority_after_callback(
        self,
    ):
        saved = []

        def capture_context():
            saved.append(copy_context())
            self.assertTrue(validate_native_update_launch(self.service, self.journals))

        with self.assertRaises(NativeUpdateRecoveryRequired):
            self.update(prepare_launch=capture_context, wait_ready=lambda: False)
        with self.assertRaises(NativeUpdateRecoveryRequired):
            saved[0].run(validate_native_update_launch, self.service, self.journals)

    def test_completed_metadata_survives_missing_container_program_without_downgrade(
        self,
    ):
        self.update()
        self.runtime.rename(self.root / "old-container-runtime")
        self.assertEqual(
            native_runtime_restore_metadata(self.service, self.journals), self.new
        )
        self.assertFalse(self.runtime.exists())

    def test_legacy_completed_receipt_restores_exact_release_after_env_normalization(
        self,
    ):
        self.service["env"] = {"FRONTEND_BACKEND_API_KEY": "fixture-old-alias"}
        self.update()
        path, _record = self.journal()
        original = path.read_bytes()
        # Simulate the deployed completed journal before installed-state existed.
        for installed in self.journals.glob("installed-release-*.json"):
            installed.unlink()
        self.service["env"] = {"INFINIDYSK__SAB__API_KEY": "fixture-native-key"}
        self.service.update(release_version="latest", release_version_enabled=False)
        self.assertTrue(validate_native_update_launch(self.service, self.journals))
        self.runtime.rename(self.root / "previous-container-program")
        self.assertEqual(
            native_runtime_restore_metadata(self.service, self.journals), self.new
        )
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.journals.glob("installed-release-*.json")), [])

    def test_installed_state_restores_without_any_update_journal(self):
        record_installed_release(self.service, self.old, self.journals)
        self.assertEqual(
            native_runtime_restore_metadata(self.service, self.journals), self.old
        )
        self.assertTrue(validate_native_update_launch(self.service, self.journals))
        self.runtime.rename(self.root / "old-container-program")
        self.assertEqual(
            native_runtime_restore_metadata(self.service, self.journals), self.old
        )
        with self.assertRaises(NativeUpdateRecoveryRequired):
            validate_native_update_launch(self.service, self.journals)

    def test_invalid_update_journal_blocks_even_a_valid_installed_record(self):
        self.update()
        path, _record = self.journal()
        path.write_text("{incomplete")
        self.assertEqual(read_installed_release(self.service, self.journals), self.new)
        for operation in (
            native_runtime_restore_metadata,
            validate_native_update_launch,
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(NativeUpdateRecoveryRequired):
                    operation(self.service, self.journals)
        self.assertEqual(path.read_text(), "{incomplete")

    def test_unfinished_update_dominates_older_normal_state_until_explicit_resume(self):
        record_installed_release(self.service, self.old, self.journals)
        self.failed_after_launch()
        self.assertEqual(read_installed_release(self.service, self.journals), self.old)
        with self.assertRaises(NativeUpdateRecoveryRequired):
            native_runtime_restore_metadata(self.service, self.journals)
        with self.assertRaises(NativeUpdateRecoveryRequired):
            validate_native_update_launch(self.service, self.journals)
        self.resume()
        self.assertEqual(read_installed_release(self.service, self.journals), self.new)
        self.assertEqual(
            native_runtime_restore_metadata(self.service, self.journals), self.new
        )

    def test_restored_runtime_new_inode_requires_normal_state_and_all_previous_bindings(
        self,
    ):
        record_installed_release(self.service, self.old, self.journals)
        with self.assertRaises(NativeUpdateError):
            self.update(prepare_launch=lambda: False)
        self.assertEqual(self.journal()[1]["state"], "restored")
        self.assertEqual(
            native_runtime_restore_metadata(self.service, self.journals), self.old
        )
        self.runtime.rename(self.root / "previous-container-program")
        shutil.copytree(self.root / "previous-container-program", self.runtime)
        self.assertTrue(validate_native_update_launch(self.service, self.journals))
        self.runtime.joinpath("LICENSE").write_text("different metadata")
        with self.assertRaises(NativeUpdateRecoveryRequired):
            validate_native_update_launch(self.service, self.journals)

    def test_restored_legacy_runtime_accepts_only_verified_marker_normalization(self):
        from utils import infinidysk_v2_runtime as runtime
        from utils.infinidysk_v2_releases import architecture

        binary_hash = hashlib.sha256(b"old program").hexdigest()
        self.runtime.joinpath("version.txt").write_text(runtime.RELEASE)
        self.runtime.joinpath("commit.txt").write_text(runtime.COMMIT)
        self.runtime.joinpath(MARKER).write_text(
            json.dumps({"commit": runtime.COMMIT, "binary_sha256": binary_hash})
        )
        accepted = runtime.installed_v2_metadata(self.service)
        self.assertEqual(accepted["release"], runtime.RELEASE)
        self.assertEqual(accepted["archive_sha256"], runtime.ASSETS[architecture()])
        record_installed_release(self.service, accepted, self.journals)
        with self.assertRaises(NativeUpdateError):
            self.update(prepare_launch=lambda: False)
        path, receipt = self.journal()
        before = path.read_bytes()
        self.assertEqual(receipt["state"], "restored")
        self.assertTrue(validate_native_update_launch(self.service, self.journals))
        self.assertEqual(
            native_runtime_restore_metadata(self.service, self.journals), accepted
        )

        self.runtime.rename(self.root / "legacy-container-program")
        shutil.copytree(self.root / "legacy-container-program", self.runtime)
        self.runtime.joinpath(MARKER).write_text(json.dumps(accepted))
        self.assertTrue(validate_native_update_launch(self.service, self.journals))
        for name in ("infinidysk", "version.txt", "commit.txt", "LICENSE", MARKER):
            with self.subTest(changed=name):
                target = self.runtime / name
                original = target.read_bytes()
                target.write_bytes(original + b"changed")
                with self.assertRaises(NativeUpdateRecoveryRequired):
                    validate_native_update_launch(self.service, self.journals)
                target.write_bytes(original)
        self.assertEqual(path.read_bytes(), before)

    def test_normal_state_conflicting_with_completed_journal_refuses_before_update(
        self,
    ):
        self.update()
        installed = next(self.journals.glob("installed-release-*.json"))
        record = json.loads(installed.read_text())
        record["release_metadata"] = self.old
        installed.write_text(json.dumps(record))
        for operation in (
            native_runtime_restore_metadata,
            validate_native_update_launch,
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(NativeUpdateRecoveryRequired):
                    operation(self.service, self.journals)
        stop = Mock()
        with self.assertRaises(NativeUpdateRecoveryRequired):
            self.update(stop_current=stop)
        stop.assert_not_called()

    def test_downgrade_and_same_version_replacement_refuse_before_lifecycle_work(self):
        self.update()
        path, _record = self.journal()
        before = path.read_bytes()
        for legacy in (False, True):
            if legacy:
                for installed in self.journals.glob("installed-release-*.json"):
                    installed.unlink()
            for release in ("v2.0.0-beta.6", "v2.0.0-beta.7"):
                with self.subTest(legacy=legacy, release=release):
                    if self.candidate.exists():
                        shutil.rmtree(self.candidate)
                    self.new = self.artifact(
                        self.candidate, release, b"different verified candidate"
                    )
                    stop, start = Mock(), Mock()
                    with self.assertRaisesRegex(NativeUpdateError, "newer verified"):
                        self.update(stop_current=stop, start_candidate=start)
                    stop.assert_not_called()
                    start.assert_not_called()
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(
                        self.runtime.joinpath("infinidysk").read_bytes(), b"new program"
                    )

    def test_failure_after_installed_state_save_still_requires_update_recovery(self):
        from utils import infinidysk_v2_installed_release as installed

        write = installed.record_installed_release

        def saved_then_failed(*args, **kwargs):
            write(*args, **kwargs)
            raise OSError("fixture crash after normal record save")

        with (
            patch.object(
                installed, "record_installed_release", side_effect=saved_then_failed
            ),
            self.assertRaises(NativeUpdateRecoveryRequired),
        ):
            self.update()
        self.assertEqual(read_installed_release(self.service, self.journals), self.new)
        self.assertEqual(self.journal()[1]["state"], "recovery_required")
        with self.assertRaises(NativeUpdateRecoveryRequired):
            native_runtime_restore_metadata(self.service, self.journals)
        self.resume()
        self.assertEqual(self.journal()[1]["state"], "completed")
        self.assertEqual(
            native_runtime_restore_metadata(self.service, self.journals), self.new
        )

    def test_restore_metadata_refuses_tampered_journal_or_changed_data_scope(self):
        self.update()
        self.service["v2_data_dir"] = str(self.root / "changed-data")
        with self.assertRaises(NativeUpdateRecoveryRequired):
            native_runtime_restore_metadata(self.service, self.journals)
        self.service.pop("v2_data_dir")
        path, _record = self.journal()
        path.write_text("{incomplete")
        with self.assertRaises(NativeUpdateRecoveryRequired):
            native_runtime_restore_metadata(self.service, self.journals)

    def test_restore_metadata_cannot_bypass_unfinished_runtime_update(self):
        self.failed_after_launch()
        with self.assertRaises(NativeUpdateRecoveryRequired):
            native_runtime_restore_metadata(self.service, self.journals)

    def test_restore_metadata_has_no_authority_without_any_update_journal(self):
        self.assertIsNone(native_runtime_restore_metadata(self.service, self.journals))

    def test_owned_launch_context_cannot_retarget_the_candidate_to_different_data(self):
        changed = {**self.service, "v2_data_dir": str(self.root / "different-data")}
        start = Mock()
        with self.assertRaises(NativeUpdateError) as caught:
            self.update(
                prepare_launch=lambda: validate_native_update_launch(
                    changed, self.journals
                ),
                start_candidate=start,
            )
        self.assertFalse(caught.exception.launched)
        start.assert_not_called()

    def test_prelaunch_restored_journal_does_not_authorize_unproven_artifact_reinstall(
        self,
    ):
        with self.assertRaises(NativeUpdateError):
            self.update(prepare_launch=lambda: False)
        with self.assertRaises(NativeUpdateRecoveryRequired):
            native_runtime_restore_metadata(self.service, self.journals)


if __name__ == "__main__":
    unittest.main()
