"""Mounted-storage recovery and registration edits use only disposable fixtures."""

import copy
import json
import os
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

from tests import test_backup_storage as fixtures
from utils.backup_storage import BackupStorage, BackupStorageError


class BackupStorageManagementTests(unittest.TestCase):
    setUp = fixtures.BackupStorageTests.setUp
    register = fixtures.BackupStorageTests.register
    shared_request = fixtures.BackupStorageTests.shared_request
    fixed_external_modes = fixtures.BackupStorageTests.fixed_external_modes

    def replacement_inspector(self, storage, *, device=True, mount=True, **changes):
        original = storage._inspect

        def inspect(path):
            value = original(path)
            value["root_identity"] = dict(value["root_identity"])
            if device:
                value["root_identity"]["device"] += 1
            if mount:
                value["mount_id"] = "new-controller-mount"
            for key, item in changes.items():
                if key in {"inode", "mode"}:
                    value["root_identity"][key] = item
                else:
                    value[key] = item
            return value

        return inspect

    def fresh_controller(self):
        return BackupStorage(self.config, state_root=self.root / "control")

    def recovery_record(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in path.parents:
            if directory == self.root:
                break
            directory.chmod(0o700)
        self.storage._write(path, value)
        return path

    def test_device_change_across_controllers_preserves_identity_receipts_and_bytes(
        self,
    ):
        self.register()
        fingerprint = self.storage.bound_identity("nas")["binding_fingerprint"]
        artifact = self.storage.write_json("nas", "example", {"retained": True})
        binding_path = self.storage._binding_path("nas")
        before = binding_path.read_bytes()
        storage = self.fresh_controller()
        inspector = self.replacement_inspector(storage)
        self.config.save_config.reset_mock()
        with (
            patch.object(storage, "_inspect", inspector),
            patch.object(
                storage, "_write", side_effect=AssertionError("No registry rewrite")
            ),
        ):
            self.assertEqual(
                storage.bound_identity("nas")["binding_fingerprint"], fingerprint
            )
            self.assertEqual(
                storage.read_json(artifact["reference"]), {"retained": True}
            )
            checked = storage.check_profile("nas")["profile"]
            self.assertTrue(checked["ready"])
            self.assertTrue(checked["live_checked"])
            self.assertFalse(checked["revalidation_required"])
        self.config.save_config.assert_not_called()
        self.assertEqual(binding_path.read_bytes(), before)

    def test_idle_runtime_replacement_is_blocked_until_reviewed(self):
        self.register()
        original = self.storage.bound_identity("nas")["binding_fingerprint"]
        before = self.storage._binding_path("nas").read_bytes()
        with patch.object(
            self.storage, "_inspect", self.replacement_inspector(self.storage)
        ):
            with self.assertRaisesRegex(
                BackupStorageError, "changed since this controller"
            ):
                self.storage.bound_identity("nas")
            with self.assertRaisesRegex(BackupStorageError, "revalidate"):
                self.storage.save_profile(self.shared_request(False))
            checked = self.storage.check_profile("nas")["profile"]
            self.assertFalse(checked["ready"])
            self.assertTrue(checked["revalidation_required"])
            self.assertEqual(self.storage._revalidation_reviews, {})
            preview = self.storage.preview_revalidation("nas")
            self.assertTrue(preview["ready"])
            self.assertTrue(preview["revalidation_required"])
            self.assertIn("device_number", preview["changes"])
            self.assertIn("mount_id", preview["changes"])
            self.assertNotIn("marker", json.dumps(preview))
            result = self.storage.revalidate_profile(
                "nas", {"token": preview["token"], "confirm": True}
            )
            self.assertTrue(result["profile"]["ready"])
            self.assertEqual(
                self.storage.bound_identity("nas")["binding_fingerprint"], original
            )
        self.assertEqual(self.storage._binding_path("nas").read_bytes(), before)

    def test_fresh_controller_still_rejects_durable_replacement_and_bad_marker(self):
        self.register()
        identity = self.storage._inspect(self.destination)["root_identity"]
        for change in (
            {"inode": identity["inode"] + 1},
            {"mode": stat.S_IFDIR | 0o700},
            {"mount_signature": "different"},
            {"filesystem_type": "different"},
        ):
            storage = self.fresh_controller()
            with (
                self.subTest(change=change),
                patch.object(
                    storage, "_inspect", self.replacement_inspector(storage, **change)
                ),
            ):
                with self.assertRaisesRegex(BackupStorageError, "missing or replaced"):
                    storage.bound_identity("nas")
                self.assertFalse(storage.preview_revalidation("nas")["ready"])
        marker = self.destination / ".dumb-backups" / "nas" / ".destination.json"
        marker.write_text(json.dumps({"marker": "0" * 64}))
        storage = self.fresh_controller()
        with patch.object(storage, "_inspect", self.replacement_inspector(storage)):
            with self.assertRaisesRegex(BackupStorageError, "marker changed"):
                storage.bound_identity("nas")
            self.assertFalse(storage.preview_revalidation("nas")["ready"])

    def test_resave_and_label_policy_edits_keep_original_binding_after_recreation(self):
        self.register()
        fingerprint = self.storage.bound_identity("nas")["binding_fingerprint"]
        original = self.storage._read(self.storage._binding_path("nas"))["binding"]
        artifact = self.storage.write_json("nas", "example", {"old": "archive"})
        storage = self.fresh_controller()
        with patch.object(storage, "_inspect", self.replacement_inspector(storage)):
            storage.save_profile(self.shared_request(False))
            for allowed in (False, True, False):
                request = {**self.shared_request(allowed), "name": "Reviewed label"}
                storage.update_profile("nas", request)
                self.assertEqual(
                    storage.bound_identity("nas")["binding_fingerprint"], fingerprint
                )
                self.assertEqual(
                    storage._read(storage._binding_path("nas"))["binding"], original
                )
                self.assertEqual(
                    storage.read_json(artifact["reference"]), {"old": "archive"}
                )
                self.assertEqual(
                    storage.bound_identity("nas")["allow_shared_permissions"], allowed
                )

    def test_label_edit_is_local_during_an_outage(self):
        self.register()
        original = self.storage._read(self.storage._binding_path("nas"))["binding"]
        with patch.object(
            self.storage,
            "_inspect",
            side_effect=AssertionError("Do not contact mounted storage"),
        ):
            result = self.storage.update_profile(
                "nas", {**self.shared_request(False), "name": "New label"}
            )
        self.assertEqual(result["profile"]["name"], "New label")
        self.assertEqual(
            self.storage._read(self.storage._binding_path("nas"))["binding"], original
        )

    def test_paths_and_identifiers_are_immutable_in_put_and_post(self):
        self.register()
        original = copy.deepcopy(self.config.config)
        for request in (
            {**self.shared_request(False), "id": "other"},
            {**self.shared_request(False), "path": str(self.root / "other")},
        ):
            with self.assertRaisesRegex(BackupStorageError, "cannot be changed"):
                self.storage.update_profile("nas", request)
        for enabled in (True, False):
            with self.assertRaisesRegex(BackupStorageError, "new identifier"):
                self.storage.save_profile(
                    {
                        **self.shared_request(False),
                        "path": str(self.root / "other"),
                        "enabled": enabled,
                    }
                )
        self.assertEqual(self.config.config, original)
        self.assertFalse((self.root / "other").exists())

    def test_offline_removal_preserves_receipts_marker_and_reserved_identifier(self):
        self.register()
        artifact = self.storage.write_json("nas", "example", {"history": True})
        fingerprint = self.storage.bound_identity("nas")["binding_fingerprint"]
        retained = {
            path: path.read_bytes() for path in self.storage.state_root.rglob("*.json")
        }
        marker = self.destination / ".dumb-backups" / "nas" / ".destination.json"
        marker_bytes = marker.read_bytes()
        with patch.object(
            self.storage,
            "_inspect",
            side_effect=AssertionError("No network dependency"),
        ):
            result = self.storage.remove_profile("nas")
        self.assertTrue(result["archives_preserved"])
        self.assertTrue(result["receipts_preserved"])
        self.assertEqual(self.config.config["backup_storage"]["profiles"], {})
        self.assertEqual({path: path.read_bytes() for path in retained}, retained)
        self.assertEqual(marker.read_bytes(), marker_bytes)
        with self.assertRaises(BackupStorageError):
            self.storage.save_profile(
                {**self.shared_request(False), "path": str(self.root / "different")}
            )
        self.register()
        self.assertEqual(
            self.storage.bound_identity("nas")["binding_fingerprint"], fingerprint
        )
        self.assertEqual(
            self.storage.read_json(artifact["reference"]), {"history": True}
        )

    def test_removal_and_disabling_refuse_default_consumer_and_active_lease(self):
        self.register()
        self.storage.set_default("nas")
        for operation in (
            lambda: self.storage.remove_profile("nas"),
            lambda: self.storage.update_profile(
                "nas", {**self.shared_request(False), "enabled": False}
            ),
        ):
            with self.assertRaisesRegex(BackupStorageError, "global default"):
                operation()
        self.storage.set_default("")
        self.config.config["consumer"] = {"backup_destination_id": "nas"}
        with self.assertRaisesRegex(BackupStorageError, "configured"):
            self.storage.remove_profile("nas")
        self.config.config.pop("consumer")
        preview = self.storage.preview_revalidation("nas")
        with self.storage._lease("nas", "example", create=True):
            with self.assertRaisesRegex(BackupStorageError, "active"):
                self.storage.remove_profile("nas")
            with self.assertRaisesRegex(BackupStorageError, "active"):
                self.storage.update_profile(
                    "nas", {**self.shared_request(False), "name": "Edit"}
                )
            with self.assertRaisesRegex(BackupStorageError, "active"):
                self.storage.revalidate_profile(
                    "nas", {"token": preview["token"], "confirm": True}
                )

    def test_schedule_and_retained_restore_block_but_historical_job_does_not(self):
        self.register()
        schedule = self.recovery_record(
            "installation-backups/schedule.json",
            {"enabled": True, "destination_id": "nas"},
        )
        self.assertIn(
            "installation_schedule",
            {item["kind"] for item in self.storage._profile_usage("nas")},
        )
        schedule.unlink()
        job = self.recovery_record(
            "installation-backups/jobs/" + "a" * 32 + ".json",
            {
                "job_id": "a" * 32,
                "job_type": "installation_restore",
                "status": "completed",
                "can_rollback": True,
                "plan": {
                    "source": {"reference": "dumb-backup://nas/example/" + "b" * 32}
                },
            },
        )
        with self.assertRaisesRegex(BackupStorageError, "retained restore"):
            self.storage.remove_profile("nas")
        self.storage._write(
            job,
            {
                "job_id": "a" * 32,
                "job_type": "installation_restore",
                "status": "completed",
                "can_rollback": False,
                "restart_required": False,
                "rollback_journal": [{"historical": True}],
                "plan": {
                    "source": {"reference": "dumb-backup://nas/example/" + "b" * 32}
                },
            },
        )
        self.assertTrue(self.storage.remove_profile("nas")["removed"])

    def test_major_retained_rollback_and_resumable_cleanup_block_removal(self):
        self.register()
        job = {
            "job_id": "b" * 32,
            "job_type": "infinidysk_v1_to_v2",
            "status": "completed",
            "mode": "cutover",
            "snapshot": {
                "cold_archive": {"reference": "dumb-backup://nas/example/" + "a" * 32}
            },
            "plan": {"options": {}, "backup_storage": {"destination_id": "nas"}},
        }
        path = self.recovery_record(
            "migrations/infinidysk-v2/jobs/" + "b" * 32 + ".json", job
        )
        for fields in (
            {},
            {
                "rollback_retired": True,
                "cleanup": {"state": "failed", "resumable": True},
            },
        ):
            self.storage._write(path, job | fields)
            with self.assertRaisesRegex(BackupStorageError, "retained rollback"):
                self.storage.remove_profile("nas")
        self.storage._write(
            path,
            job
            | {
                "rollback_retired": True,
                "cleanup": {"state": "completed", "resumable": False},
            },
        )
        self.assertTrue(self.storage.remove_profile("nas")["removed"])

    def test_conflicting_migration_state_cannot_hide_an_active_owner(self):
        self.register()
        self.recovery_record(
            "migrations/infinidysk-v2/jobs/" + "a" * 32 + ".json",
            {
                "job_id": "a" * 32,
                "job_type": "infinidysk_v1_to_v2",
                "status": "importing",
                "state": "completed",
                "mode": "cutover",
                "snapshot": None,
                "plan": {"options": {}, "backup_storage": {"destination_id": "nas"}},
            },
        )
        with self.assertRaisesRegex(BackupStorageError, "recovery references"):
            self.storage.remove_profile("nas")

    def test_unsafe_local_recovery_inventory_blocks_removal_without_network_io(self):
        self.register()
        parent = self.root / "installation-backups"
        parent.mkdir(mode=0o700)
        (parent / "jobs").symlink_to(self.root, target_is_directory=True)
        with patch.object(
            self.storage, "_inspect", side_effect=AssertionError("No network IO")
        ):
            with self.assertRaisesRegex(BackupStorageError, "recovery references"):
                self.storage.remove_profile("nas")
        self.assertIn("nas", self.config.config["backup_storage"]["profiles"])

    def test_normal_migration_wrappers_accept_application_owned_config_ancestry(self):
        application_uid = os.geteuid() + 1000
        self.config.config.update(puid=application_uid, pgid=os.getegid())
        self.root.chmod(0o770)
        self.register()
        previews = self.root / "migrations" / "infinidysk-v2" / "previews"
        # Mirror the controller's leaf-only mkdir/chmod contract. The generic
        # parents use normal mkdir(parents=True) permissions, not private0700.
        previews.mkdir(parents=True, mode=0o700)
        (self.root / "migrations").chmod(0o755)
        previews.parent.chmod(0o755)
        preview_path = previews / "review.json"
        self.storage._write(
            preview_path,
            {"expires_at": 0, "plan": {"options": {}, "backup_storage": None}},
        )
        original_fstat = os.fstat

        def application_owner(descriptor):
            value = original_fstat(descriptor)
            path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            if path in {self.root, self.root / "migrations"}:
                fields = list(value)
                fields[4] = application_uid
                return os.stat_result(fields)
            return value

        with patch("utils.backup_storage.os.fstat", application_owner):
            row = self.storage.profiles()["destinations"][0]
            self.assertFalse(row["usage"])
            self.assertTrue(self.storage.check_profile("nas")["profile"]["ready"])
            # An absent jobs leaf is valid; its ordinary wrappers do not create a
            # permanent false in-use condition for a removable registration.
            self.assertTrue(self.storage.remove_profile("nas")["removed"])
        self.assertFalse((previews.parent / "jobs").exists())
        self.assertEqual(stat.S_IMODE(previews.parent.stat().st_mode), 0o755)
        self.assertTrue(preview_path.is_file())

    def test_private_migration_leaf_and_safe_writer_guards_remain_required(self):
        self.register()
        previews = self.root / "migrations" / "infinidysk-v2" / "previews"
        previews.mkdir(parents=True, mode=0o700)
        (self.root / "migrations").chmod(0o755)
        previews.parent.chmod(0o755)
        for path, mode in ((previews, 0o755), (previews.parent, 0o777)):
            with self.subTest(path=path, mode=mode):
                before = stat.S_IMODE(path.stat().st_mode)
                path.chmod(mode)
                row = self.storage.profiles()["destinations"][0]
                self.assertIn(
                    "recovery_unavailable", {item["kind"] for item in row["usage"]}
                )
                with self.assertRaisesRegex(BackupStorageError, "recovery references"):
                    self.storage.remove_profile("nas")
                path.chmod(before)

    def test_malformed_recovery_job_blocks_only_management_not_catalog(self):
        self.register()
        self.recovery_record(
            "installation-backups/jobs/" + "a" * 32 + ".json",
            {
                "job_id": "a" * 32,
                "job_type": "installation_restore",
                "status": "completed",
                "plan": {},
            },
        )
        catalog = self.storage.profiles()
        self.assertEqual(catalog["destinations"][0]["id"], "nas")
        self.assertIn(
            "recovery_unavailable",
            {row["kind"] for row in catalog["destinations"][0]["usage"]},
        )
        with self.assertRaisesRegex(BackupStorageError, "recovery references"):
            self.storage.remove_profile("nas")
        self.assertTrue(self.storage.check_profile("nas")["profile"]["ready"])

    def test_malformed_preview_and_configured_reference_keep_catalog_visible(self):
        self.register()
        for plan in (
            {"options": ["malformed"], "backup_storage": {"destination_id": "nas"}},
            {"options": {}, "backup_storage": ["malformed"]},
            {"options": {}, "backup_storage": {"destination_id": 123}},
            {
                "options": {"backup_destination_id": ["malformed"]},
                "backup_storage": None,
            },
        ):
            with self.subTest(plan=plan):
                path = self.recovery_record(
                    "migrations/infinidysk-v2/previews/review.json",
                    {"expires_at": 9999999999, "plan": plan},
                )
                row = self.storage.profiles()["destinations"][0]
                self.assertEqual(row["id"], "nas")
                self.assertIn(
                    "recovery_unavailable", {item["kind"] for item in row["usage"]}
                )
                self.assertTrue(self.storage.check_profile("nas")["profile"]["ready"])
                with self.assertRaisesRegex(BackupStorageError, "recovery references"):
                    self.storage.remove_profile("nas")
                path.unlink()
        self.config.config["fixture_note"] = "dumb-backup://malformed"
        row = self.storage.profiles()["destinations"][0]
        self.assertEqual(row["id"], "nas")
        self.assertIn(
            "configured_reference_unavailable", {item["kind"] for item in row["usage"]}
        )
        with self.assertRaisesRegex(BackupStorageError, "configured backup reference"):
            self.storage.remove_profile("nas")
        self.assertTrue(self.storage.check_profile("nas")["profile"]["ready"])

    def test_nonfinite_or_invalid_preview_expiry_is_a_local_usage_blocker(self):
        self.register()
        for expires in (True, float("nan"), float("inf"), -float("inf"), -1, 10**1000):
            with self.subTest(expires=expires):
                self.recovery_record(
                    "migrations/infinidysk-v2/previews/review.json",
                    {
                        "expires_at": expires,
                        "plan": {"options": {}, "backup_storage": None},
                    },
                )
                row = self.storage.profiles()["destinations"][0]
                self.assertEqual(row["id"], "nas")
                self.assertIn(
                    "recovery_unavailable", {item["kind"] for item in row["usage"]}
                )
                with self.assertRaisesRegex(BackupStorageError, "recovery references"):
                    self.storage.remove_profile("nas")

    def test_permission_narrowing_cannot_strand_a_retained_recovery(self):
        self.storage.save_profile(self.shared_request(True))
        self.recovery_record(
            "installation-backups/jobs/" + "a" * 32 + ".json",
            {
                "job_id": "a" * 32,
                "job_type": "installation_restore",
                "status": "completed",
                "can_rollback": True,
                "plan": {
                    "source": {"reference": "dumb-backup://nas/example/" + "b" * 32}
                },
            },
        )
        with self.assertRaisesRegex(BackupStorageError, "retained restore"):
            self.storage.update_profile("nas", self.shared_request(False))
        self.assertTrue(self.storage.bound_identity("nas")["allow_shared_permissions"])

    def test_review_is_bound_to_current_config_mount_and_expiry(self):
        self.register()
        preview = self.storage.preview_revalidation("nas")
        self.config.config["backup_storage"]["profiles"]["nas"]["name"] = "Changed"
        with self.assertRaisesRegex(BackupStorageError, "configuration changed"):
            self.storage.revalidate_profile(
                "nas", {"token": preview["token"], "confirm": True}
            )
        preview = self.storage.preview_revalidation("nas")
        self.storage._revalidation_reviews[preview["token"]]["expires_at"] = 0
        with self.assertRaisesRegex(BackupStorageError, "expired"):
            self.storage.revalidate_profile(
                "nas", {"token": preview["token"], "confirm": True}
            )
        preview = self.storage.preview_revalidation("nas")
        with patch.object(
            self.storage, "_inspect", self.replacement_inspector(self.storage)
        ):
            with self.assertRaisesRegex(
                BackupStorageError, "storage or configuration changed"
            ):
                self.storage.revalidate_profile(
                    "nas", {"token": preview["token"], "confirm": True}
                )

    def test_disabled_destination_can_review_remount_then_reenable(self):
        self.register()
        self.storage.update_profile(
            "nas", {**self.shared_request(False), "enabled": False}
        )
        with patch.object(
            self.storage, "_inspect", self.replacement_inspector(self.storage)
        ):
            with self.assertRaisesRegex(BackupStorageError, "revalidate"):
                self.storage.update_profile("nas", self.shared_request(False))
            checked = self.storage.check_profile("nas")["profile"]
            self.assertTrue(checked["revalidation_required"])
            preview = self.storage.preview_revalidation("nas")
            self.assertTrue(preview["ready"])
            self.storage.revalidate_profile(
                "nas", {"token": preview["token"], "confirm": True}
            )
            self.storage.update_profile("nas", self.shared_request(False))
            self.assertTrue(self.storage.check_profile("nas")["profile"]["ready"])

    def test_explicit_unused_remove_allows_owned_reregistration_after_remount(self):
        self.register()
        fingerprint = self.storage.bound_identity("nas")["binding_fingerprint"]
        artifact = self.storage.write_json("nas", "example", {"old": True})
        self.storage.remove_profile("nas")
        with patch.object(
            self.storage, "_inspect", self.replacement_inspector(self.storage)
        ):
            self.register()
            self.assertEqual(
                self.storage.bound_identity("nas")["binding_fingerprint"], fingerprint
            )
            self.assertEqual(
                self.storage.read_json(artifact["reference"]), {"old": True}
            )

    def test_existing_owned_namespace_is_never_recreated_during_reregistration(self):
        self.register()
        owned = self.destination / ".dumb-backups" / "nas"
        (owned / ".destination.json").unlink()
        owned.rmdir()
        with self.assertRaisesRegex(BackupStorageError, "missing"):
            self.storage.save_profile(self.shared_request(False))
        self.assertFalse(owned.exists())

    def test_catalog_uses_only_local_cached_checks(self):
        self.register()
        with patch.object(
            self.storage,
            "_inspect",
            side_effect=AssertionError("Catalog must not probe NAS"),
        ):
            row = self.storage.profiles()["destinations"][0]
        self.assertFalse(row["live_checked"])
        self.assertEqual(row["validation_state"], "not_checked")


if __name__ == "__main__":
    unittest.main()
