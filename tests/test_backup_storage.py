from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from utils.backup_storage import (
    BackupStorage,
    BackupStorageError,
    get_backup_storage,
    is_backup_reference,
    parse_backup_reference,
    paths_overlap,
    require_local_path,
    selected_backup_destination,
)


class BackupStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / "archive"
        self.destination.mkdir()
        self.config = SimpleNamespace(
            config={"backup_storage": {"default_destination_id": "", "profiles": {}}},
            file_path=str(self.root / "dumb.json"),
            save_config=Mock(),
        )
        self.storage = BackupStorage(self.config, state_root=self.root / "control")

    def register(self):
        return self.storage.save_profile(
            {
                "id": "nas",
                "name": "Cold archives",
                "path": str(self.destination),
                "enabled": True,
            }
        )

    @contextmanager
    def fixed_external_modes(self, *, directories=None, files=None):
        # Emulate a mount's fixed dir_mode/file_mode, scoped to this disposable
        # destination. Local controller files retain real POSIX private modes.
        original_open, original_mkdir = os.open, os.mkdir

        def external(path, dir_fd=None):
            path = Path(path)
            if not path.is_absolute() and dir_fd is not None:
                path = Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / path
            return path == self.destination or self.destination in path.parents

        def open_file(path, flags, mode=0o777, *, dir_fd=None):
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
            if files is not None and flags & os.O_CREAT and external(path, dir_fd):
                os.fchmod(descriptor, files)
            return descriptor

        def mkdir(path, mode=0o777, *, dir_fd=None):
            original_mkdir(path, mode, dir_fd=dir_fd)
            if directories is not None and external(path, dir_fd):
                os.chmod(path, directories, dir_fd=dir_fd, follow_symlinks=False)

        with (
            patch("utils.backup_storage.os.open", open_file),
            patch("utils.backup_storage.os.mkdir", mkdir),
        ):
            yield

    def shared_request(self, allowed=True):
        return {
            "id": "nas",
            "name": "Cold archives",
            "path": str(self.destination),
            "enabled": True,
            "allow_shared_permissions": allowed,
        }

    def test_fixed_shared_directory_failure_can_retry_same_root_without_deletion(self):
        with self.fixed_external_modes(directories=0o777, files=0o666):
            with self.assertRaisesRegex(BackupStorageError, "0777.*explicitly trusted"):
                self.storage.save_profile(self.shared_request(False))
            owned = self.destination / ".dumb-backups"
            original_inode = owned.stat().st_ino
            preview = self.storage.preview(self.shared_request(False))
            self.assertFalse(preview["ready"])
            self.assertEqual(preview["observed_permissions"]["directory_mode"], "0777")
            shared_preview = self.storage.preview(self.shared_request())
            self.assertTrue(shared_preview["ready"])
            self.assertTrue(shared_preview["warnings"])
            saved = self.storage.save_profile(self.shared_request())["profile"]
            self.assertEqual(owned.stat().st_ino, original_inode)
            self.assertEqual(
                saved["observed_permissions"],
                {"directory_mode": "0777", "file_mode": "0666"},
            )
            self.assertTrue(saved["warnings"])
            self.assertTrue(saved["allow_shared_permissions"])
            receipt = self.storage.write_json(
                "nas",
                "symlink-example",
                {"secret": "fixture-only"},
                metadata={"kind": "symlink_manifest", "retention_scope": "scheduled"},
            )
            self.assertTrue(receipt["allow_shared_permissions"])
            self.assertEqual(
                self.storage.read_json(receipt["reference"]), {"secret": "fixture-only"}
            )
            self.assertEqual(
                self.storage.list_artifacts("nas", "symlink-example"), [receipt]
            )
            self.storage.delete_artifact(receipt["reference"])
            self.assertEqual(self.storage.list_artifacts("nas", "symlink-example"), [])
            self.assertEqual(owned.stat().st_mode & 0o777, 0o777)

    def test_fixed_marker_mode_failure_has_private_retry_authority(self):
        with self.fixed_external_modes(files=0o644):
            with self.assertRaisesRegex(BackupStorageError, "0644.*explicitly trusted"):
                self.storage.save_profile(self.shared_request(False))
            marker = self.destination / ".dumb-backups" / "nas" / ".destination.json"
            before = marker.read_bytes(), marker.stat().st_ino
            pending = self.storage._state("pending", "nas.json")
            self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
            # A new controller object has only durable private pending authority.
            self.storage = BackupStorage(self.config, state_root=self.root / "control")
            saved = self.storage.save_profile(self.shared_request())["profile"]
            self.assertTrue(saved["ready"])
            self.assertEqual((marker.read_bytes(), marker.stat().st_ino), before)
            self.assertEqual(saved["observed_permissions"]["file_mode"], "0644")

    def test_interrupted_marker_retry_survives_mount_id_change_and_clears_pending(self):
        with self.fixed_external_modes(files=0o644):
            with self.assertRaises(BackupStorageError):
                self.storage.save_profile(self.shared_request(False))
            original = self.storage._inspect

            def rebooted(path):
                return {**original(path), "mount_id": "replacement-runtime-id"}

            with patch.object(self.storage, "_inspect", rebooted):
                self.storage.save_profile(self.shared_request())
                self.assertTrue(
                    self.storage.bound_identity("nas")["allow_shared_permissions"]
                )
            self.assertIsNone(
                self.storage._read(self.storage._state("pending", "nas.json"))
            )
            # Authoritative completed binding does not retain ephemeral mount IDs.
            with patch.object(self.storage, "_inspect", rebooted):
                self.assertTrue(
                    self.storage.save_profile(self.shared_request())["profile"]["ready"]
                )

    def test_corrupt_pending_registration_never_mints_marker_authority(self):
        path = self.storage._state("pending", "nas.json")
        for payload in ([], {"marker": "not-an-identity", "inspected": {}}):
            self.storage._write(path, payload)
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(BackupStorageError, "proof is invalid"),
            ):
                self.storage.save_profile(self.shared_request())
        self.assertFalse(
            (self.destination / ".dumb-backups" / "nas" / ".destination.json").exists()
        )

    def test_fifo_marker_is_rejected_without_blocking_preview_or_registration(self):
        owned = self.destination / ".dumb-backups" / "nas"
        owned.mkdir(parents=True)
        os.mkfifo(owned / ".destination.json", 0o600)
        original = os.open

        def opened(path, flags, mode=0o777, *, dir_fd=None):
            if str(path) == ".destination.json":
                self.assertTrue(flags & os.O_NONBLOCK)
            return original(path, flags, mode, dir_fd=dir_fd)

        with patch("utils.backup_storage.os.open", opened):
            preview = self.storage.preview(self.shared_request())
            self.assertFalse(preview["ready"])
            with self.assertRaisesRegex(BackupStorageError, "single-link regular"):
                self.storage.save_profile(self.shared_request())

    def test_shared_consent_does_not_adopt_an_arbitrary_marker(self):
        owned = self.destination / ".dumb-backups" / "nas"
        owned.mkdir(parents=True)
        (owned / ".destination.json").write_text(json.dumps({"marker": "a" * 64}))
        with self.assertRaisesRegex(BackupStorageError, "unowned"):
            self.storage.save_profile(self.shared_request())
        self.assertEqual(self.config.config["backup_storage"]["profiles"], {})

    def test_pending_registration_cannot_be_reused_on_replaced_root(self):
        with self.fixed_external_modes(files=0o644):
            with self.assertRaises(BackupStorageError):
                self.storage.save_profile(self.shared_request(False))
        self.destination.rename(self.root / "original")
        self.destination.mkdir()
        with self.assertRaisesRegex(BackupStorageError, "different mounted directory"):
            self.storage.save_profile(self.shared_request())
        self.assertFalse(
            (self.destination / ".dumb-backups" / "nas" / ".destination.json").exists()
        )

    def test_shared_policy_is_bound_and_old_missing_flag_stays_strict(self):
        self.register()
        before = self.storage.bound_identity("nas")
        record_path = self.storage._binding_path("nas")
        record = self.storage._read(record_path)
        record["binding"].pop("allow_shared_permissions")
        record["public"].pop("allow_shared_permissions")
        self.storage._write(record_path, record)
        self.config.config["backup_storage"]["profiles"]["nas"].pop(
            "allow_shared_permissions"
        )
        self.assertTrue(self.storage.profiles()["destinations"][0]["ready"])
        legacy = self.storage.bound_identity("nas")
        self.assertFalse(legacy["allow_shared_permissions"])
        self.assertEqual(
            legacy["binding_fingerprint"],
            self.storage.bound_identity("nas")["binding_fingerprint"],
        )
        self.config.config["backup_storage"]["profiles"]["nas"][
            "allow_shared_permissions"
        ] = True
        self.assertFalse(self.storage.profiles()["destinations"][0]["ready"])
        with self.assertRaisesRegex(BackupStorageError, "policy changed"):
            self.storage.write_json("nas", "symlink-example", {})
        self.storage.save_profile(self.shared_request())
        after = self.storage.bound_identity("nas")
        self.assertTrue(after["allow_shared_permissions"])
        self.assertNotEqual(before["binding_fingerprint"], after["binding_fingerprint"])

    def test_shared_external_policy_never_relaxes_local_control_or_link_checks(self):
        self.storage.save_profile(self.shared_request())
        receipt = self.storage.write_json("nas", "symlink-example", {})
        artifact = (
            self.destination
            / ".dumb-backups"
            / "nas"
            / "symlink-example"
            / (receipt["artifact_id"] + ".json")
        )
        os.link(artifact, artifact.with_name("foreign-hardlink"))
        with self.assertRaisesRegex(BackupStorageError, "single-link"):
            self.storage.read_json(receipt["reference"])
        artifact.with_name("foreign-hardlink").unlink()
        artifact.rename(artifact.with_name("actual"))
        artifact.symlink_to("actual")
        with self.assertRaises(OSError):
            self.storage.read_json(receipt["reference"])
        # The private catalog is not an external archive and remains mandatory.
        self.storage.state_root.chmod(0o755)
        with self.assertRaisesRegex(BackupStorageError, "private mode"):
            self.storage.profiles()

    def test_fifo_artifact_is_rejected_before_any_stream_read(self):
        self.storage.save_profile(self.shared_request())
        receipt = self.storage.write_json("nas", "symlink-example", {})
        artifact = (
            self.destination
            / ".dumb-backups"
            / "nas"
            / "symlink-example"
            / (receipt["artifact_id"] + ".json")
        )
        artifact.unlink()
        os.mkfifo(artifact, 0o666)
        original = os.open

        def opened(path, flags, mode=0o777, *, dir_fd=None):
            if str(path) == artifact.name:
                self.assertTrue(flags & os.O_NONBLOCK)
            return original(path, flags, mode, dir_fd=dir_fd)

        with patch("utils.backup_storage.os.open", opened):
            with self.assertRaisesRegex(BackupStorageError, "single-link regular"):
                self.storage.read_json(receipt["reference"])

    def test_shared_consent_does_not_allow_missing_or_tampered_owned_markers(self):
        self.storage.save_profile(self.shared_request())
        marker = self.destination / ".dumb-backups" / "nas" / ".destination.json"
        marker.write_text(json.dumps({"marker": "b" * 64}))
        with self.assertRaisesRegex(BackupStorageError, "marker changed"):
            self.storage.write_json("nas", "symlink-example", {})
        marker.unlink()
        with self.assertRaisesRegex(BackupStorageError, "identity is missing"):
            self.storage.save_profile(self.shared_request())

    def test_shared_consent_requires_actual_boolean_in_core(self):
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaises(BackupStorageError):
                self.storage.save_profile(self.shared_request(value))

    def test_existing_root_registration_json_roundtrip_and_private_receipt(self):
        profile = self.register()["profile"]
        self.assertTrue(profile["registered"])
        payload = {"manifest_type": "symlink_backup", "entries": ["example"]}
        receipt = self.storage.write_json(
            "nas",
            "symlink-example",
            payload,
            metadata={"kind": "symlink_manifest", "private": "not-public"},
        )
        self.assertTrue(is_backup_reference(receipt["reference"]))
        self.assertEqual(receipt["artifact_kind"], "symlink_manifest")
        self.assertNotIn("private", receipt)
        self.assertNotIn(str(self.destination), json.dumps(receipt))
        self.assertEqual(
            payload,
            self.storage.read_json(
                receipt["reference"], expected_kind="symlink_manifest"
            ),
        )
        self.assertEqual(
            [receipt], self.storage.list_artifacts("nas", "symlink-example")
        )
        with self.storage.open_artifact(receipt["reference"]) as handle:
            self.assertEqual(json.loads(handle.read()), payload)
        self.assertEqual(
            0o600,
            (
                self.destination
                / ".dumb-backups"
                / "nas"
                / "symlink-example"
                / (receipt["artifact_id"] + ".json")
            )
            .stat()
            .st_mode
            & 0o777,
        )

    def test_selected_destination_is_explicit_and_default_is_opt_in(self):
        self.assertIsNone(
            selected_backup_destination({"symlink_backup_path": "/custom/legacy.json"})
        )
        self.assertIsNone(selected_backup_destination({"backup_destination_id": ""}))
        self.assertEqual(
            "@default",
            selected_backup_destination({"backup_destination_id": "@default"}),
        )
        self.register()
        self.storage.set_default("nas")
        receipt = self.storage.write_json("@default", "symlink-example", {})
        self.assertEqual(receipt["backup_destination_id"], "nas")

    def test_catalog_does_not_contact_registered_network_destinations(self):
        self.register()
        with patch.object(
            self.storage, "_inspect", side_effect=AssertionError("no network probe")
        ):
            profiles = self.storage.profiles()
        self.assertEqual(profiles["destinations"][0]["id"], "nas")
        self.assertTrue(profiles["destinations"][0]["registered"])

    def test_missing_selected_root_is_never_created(self):
        self.destination.rmdir()
        with self.assertRaises(BackupStorageError):
            self.register()
        self.assertFalse(self.destination.exists())

    def test_replaced_root_never_receives_fallback_files(self):
        self.register()
        previous = self.root / "previous-mount"
        self.destination.rename(previous)
        self.destination.mkdir()
        with self.assertRaisesRegex(BackupStorageError, "missing or replaced"):
            self.storage.write_json("nas", "symlink-example", {})
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertTrue(
            (previous / ".dumb-backups" / "nas" / ".destination.json").is_file()
        )

    def test_root_change_between_validation_and_namespace_creation_cannot_fallback(
        self,
    ):
        self.register()
        original_validate = self.storage._validate

        def replaced(profile):
            result = original_validate(profile)
            self.destination.rename(self.root / "previous-mount")
            self.destination.mkdir()
            return result

        with patch.object(self.storage, "_validate", replaced):
            with self.assertRaises(BackupStorageError):
                self.storage.write_json("nas", "symlink-example", {})
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertFalse(any(self.storage._active.values()))

    def test_symbolic_destination_or_changed_marker_refuses(self):
        alias = self.root / "alias"
        alias.symlink_to(self.destination)
        result = self.storage.preview({"id": "alias", "path": str(alias)})
        self.assertFalse(result["ready"])
        self.register()
        marker = self.destination / ".dumb-backups" / "nas" / ".destination.json"
        marker.write_text(json.dumps({"marker": "0" * 64}))
        with self.assertRaisesRegex(BackupStorageError, "marker changed"):
            self.storage.write_json("nas", "symlink-example", {})

    def test_modified_artifact_and_wrong_kind_are_refused(self):
        self.register()
        receipt = self.storage.write_json(
            "nas",
            "symlink-example",
            {"keep": "original"},
            metadata={"kind": "symlink_manifest"},
        )
        with self.assertRaisesRegex(BackupStorageError, "kind"):
            self.storage.read_json(receipt["reference"], expected_kind="other")
        path = (
            self.destination
            / ".dumb-backups"
            / "nas"
            / "symlink-example"
            / (receipt["artifact_id"] + ".json")
        )
        path.write_text('{"changed":true}')
        with self.assertRaisesRegex(BackupStorageError, "did not verify"):
            self.storage.read_json(receipt["reference"])

    def test_registered_namespace_retention_only_deletes_owned_artifact(self):
        self.register()
        receipt = self.storage.write_json("nas", "symlink-example", {})
        namespace = self.destination / ".dumb-backups" / "nas" / "symlink-example"
        unrelated = namespace / "keep.json"
        unrelated.write_text("unrelated")
        self.storage.delete_artifact(receipt["reference"])
        self.assertEqual(unrelated.read_text(), "unrelated")
        self.assertEqual(self.storage.list_artifacts("nas", "symlink-example"), [])

    def test_catalog_filename_traversal_and_invalid_references_are_refused(self):
        self.register()
        receipt = self.storage.write_json("nas", "symlink-example", {})
        profile, namespace, artifact = parse_backup_reference(receipt["reference"])
        path = self.storage._catalog(profile, namespace, artifact)
        data = self.storage._read(path)
        data["filename"] = "../../outside"
        self.storage._write(path, data)
        with self.assertRaises(BackupStorageError):
            self.storage.read_json(receipt["reference"])
        for reference in (
            "https://example.invalid/item",
            "dumb-backup://nas/../secret",
            "dumb-backup://nas/symlink-example/" + "a" * 32 + "?path=outside",
        ):
            with self.subTest(reference=reference):
                with self.assertRaises(BackupStorageError):
                    parse_backup_reference(reference)

    def test_local_database_paths_reject_network_mounts(self):
        with patch(
            "utils.backup_storage.mount_records",
            return_value=[
                {
                    "path": "/",
                    "mount_id": "1",
                    "filesystem": "nfs4",
                    "signature": "example",
                }
            ],
        ):
            with self.assertRaisesRegex(BackupStorageError, "local filesystem"):
                require_local_path(self.root)

    def test_closed_file_producer_failure_leaves_no_published_artifact(self):
        self.register()

        def failing(handle):
            handle.write(b"partial")
            raise OSError("fixture failure")

        with self.assertRaises(OSError):
            self.storage.write_file("nas", "major-example", failing)
        self.assertEqual(self.storage.list_artifacts("nas", "major-example"), [])
        self.assertEqual(
            list(
                (self.destination / ".dumb-backups" / "nas" / "major-example").iterdir()
            ),
            [],
        )
        self.assertFalse(any(self.storage._active.values()))

    def test_cached_service_is_per_injected_configuration_without_default_import(self):
        self.assertIs(get_backup_storage(self.config), get_backup_storage(self.config))
        other = SimpleNamespace(config={}, file_path=str(self.root / "other.json"))
        self.assertIsNot(get_backup_storage(self.config), get_backup_storage(other))

    def test_all_task_owned_directory_descriptors_are_closed(self):
        self.register()
        receipt = self.storage.write_json("nas", "symlink-example", {})
        self.storage.read_json(receipt["reference"])
        owned = []
        for path in Path("/proc/self/fd").iterdir():
            try:
                target = os.readlink(path)
            except FileNotFoundError:
                continue
            if target.startswith(str(self.root)):
                owned.append(target)
        self.assertEqual(owned, [])

    def test_blocked_registration_does_not_hold_status_lock_or_allow_another_writer(
        self,
    ):
        entered, release = threading.Event(), threading.Event()
        original = self.storage._inspect
        errors = []

        def inspect(path):
            entered.set()
            if not release.wait(timeout=3):
                raise TimeoutError("fixture release was not signalled")
            return original(path)

        def register():
            try:
                self.register()
            except Exception as error:
                errors.append(error)

        with patch.object(self.storage, "_inspect", inspect):
            worker = threading.Thread(target=register)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=1))
                self.assertTrue(self.storage._lock.acquire(timeout=0.2))
                self.storage._lock.release()
                self.assertEqual(self.storage.profiles()["destinations"], [])
                with self.assertRaisesRegex(BackupStorageError, "being registered"):
                    self.storage.write_json("nas", "symlink-example", {})
            finally:
                release.set()
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_writer_lane_is_reserved_before_network_validation(self):
        self.register()
        original = self.storage._validate

        def validate(profile):
            self.assertEqual(self.storage._active["nas"], 1)
            with self.assertRaisesRegex(BackupStorageError, "active backup"):
                self.register()
            return original(profile)

        with patch.object(self.storage, "_validate", validate):
            self.storage.write_json("nas", "symlink-example", {})
        self.assertFalse(any(self.storage._active.values()))

    def test_raw_profile_config_edit_invalidates_cached_ready_without_network_io(self):
        self.register()
        self.config.config["backup_storage"]["profiles"]["nas"]["path"] = str(
            self.root / "other"
        )
        with patch.object(
            self.storage, "_inspect", side_effect=AssertionError("no network probe")
        ):
            catalog = self.storage.profiles()
        self.assertFalse(catalog["destinations"][0]["ready"])
        self.assertTrue(catalog["destinations"][0]["blockers"])

    def test_stale_job_owner_cannot_publish_closed_artifact(self):
        self.register()
        active = True

        def owner_check():
            if not active:
                raise RuntimeError("job ownership changed")

        def produce(handle):
            nonlocal active
            handle.write(b"closed bytes")
            active = False

        with self.assertRaisesRegex(RuntimeError, "ownership changed"):
            self.storage.write_file(
                "nas", "major-example", produce, owner_check=owner_check
            )
        self.assertEqual(self.storage.list_artifacts("nas", "major-example"), [])
        self.assertEqual(
            list(
                (self.destination / ".dumb-backups" / "nas" / "major-example").iterdir()
            ),
            [],
        )
        self.assertFalse(any(self.storage._active.values()))

    def test_physical_overlap_handles_alias_identity_and_keeps_same_filesystem_siblings(
        self,
    ):
        first, second = self.root / "source", self.root / "backup"
        first.mkdir()
        second.mkdir()
        self.assertFalse(paths_overlap(first, second))
        self.assertTrue(paths_overlap(first, first / "future" / "child"))
        with patch(
            "utils.backup_storage._path_ancestry",
            side_effect=[
                ((91, 11), ((91, 1), (91, 11))),
                ((91, 11), ((91, 1), (91, 7), (91, 11))),
            ],
        ):
            self.assertTrue(paths_overlap(first, second))
        with patch(
            "utils.backup_storage._path_ancestry",
            side_effect=[
                ((91, 11), ((91, 1), (91, 11))),
                (None, ((91, 1), (91, 11))),
            ],
        ):
            self.assertTrue(paths_overlap(first, second / "not-yet-created"))

    def test_filesystem_coordinates_detect_bind_alias_inside_protected_subtree(self):
        records = [
            {"path": "/", "device": "0:1", "root": "/"},
            {"path": "/data", "device": "8:1", "root": "/host/data"},
            {
                "path": "/mnt/archive",
                "device": "8:1",
                "root": "/host/data/infinidysk/blobs",
            },
        ]
        with (
            patch(
                "utils.backup_storage._path_ancestry", return_value=(None, ((1, 1),))
            ),
            patch("utils.backup_storage.mount_records", return_value=records),
        ):
            self.assertTrue(paths_overlap("/data/infinidysk", "/mnt/archive"))
            self.assertTrue(
                paths_overlap("/mnt/archive", "/data/infinidysk/blobs/future")
            )
            self.assertFalse(paths_overlap("/data/other", "/mnt/archive"))

    def test_physical_containment_refuses_links_without_leaking_descriptors(self):
        source, alias = self.root / "source", self.root / "alias"
        source.mkdir()
        alias.symlink_to(source, target_is_directory=True)
        for _ in range(3):
            with self.assertRaisesRegex(BackupStorageError, "without following"):
                paths_overlap(source, alias)
        owned = []
        for path in Path("/proc/self/fd").iterdir():
            try:
                target = os.readlink(path)
            except FileNotFoundError:
                continue
            if target.startswith(str(self.root)):
                owned.append(target)
        self.assertEqual(owned, [])
