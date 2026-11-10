"""Consumer contracts for explicitly selected registered symlink backups."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from utils.backup_storage import BackupStorageError


def _helpers():
    name = "_dumb_symlink_backup_storage_helpers"
    config = types.ModuleType("utils.config_loader")
    config.CONFIG_MANAGER = types.SimpleNamespace(get=lambda *_args, **_kwargs: {})
    logger = types.ModuleType("utils.global_logger")
    logger.logger = Mock()
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parents[1] / "utils" / "symlink_repair.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {name: module, "utils.config_loader": config, "utils.global_logger": logger},
    ):
        spec.loader.exec_module(module)
    return module


helpers = _helpers()
NAMESPACE = helpers.symlink_backup_namespace("InfiniDysk")
REFERENCE = "dumb-backup://nas/" + NAMESPACE + "/" + "a" * 32


def receipt(reference=REFERENCE, created_at=1):
    return {
        "artifact_id": reference.rsplit("/", 1)[1],
        "reference": reference,
        "backup_destination_id": "nas",
        "namespace": NAMESPACE,
        "created_at": created_at,
        "size_bytes": 12,
        "sha256": "b" * 64,
        "verified": True,
        "artifact_kind": "symlink_manifest",
        "retention_scope": "scheduled",
    }


class SymlinkBackupStorageTests(unittest.TestCase):
    def test_legacy_custom_destination_is_private_verified_and_not_redirected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary, "links")
            root.mkdir()
            (root / "movie").symlink_to("/reserved-example/missing")
            destination = Path(temporary, "chosen", "snapshot.json")
            with patch.object(helpers, "get_backup_storage") as storage:
                report = helpers.backup_symlink_manifest([str(root)], str(destination))
            payload = destination.read_bytes()
            self.assertEqual(report["backup_manifest"], str(destination))
            self.assertEqual(report["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertTrue(report["verified"])
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(
                json.loads(payload)["entries"][0]["link_path"], str(root / "movie")
            )
            storage.assert_not_called()

    def test_explicit_profile_writes_managed_reference_without_legacy_fallback(self):
        storage = Mock()
        storage.write_json.return_value = receipt()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary, "links")
            root.mkdir()
            (root / "movie").symlink_to("/reserved-example/missing")
            legacy = Path(temporary, "legacy", "snapshot.json")
            with patch.object(helpers, "get_backup_storage", return_value=storage):
                report = helpers.backup_symlink_manifest(
                    [str(root)],
                    str(legacy),
                    backup_destination_id="nas",
                    process_name="InfiniDysk",
                )
            self.assertFalse(legacy.parent.exists())
            self.assertEqual(report["backup_manifest"], REFERENCE)
            self.assertEqual(report["storage_receipt"], receipt())
            selected, namespace, payload = storage.write_json.call_args.args
            self.assertEqual((selected, namespace), ("nas", NAMESPACE))
            self.assertEqual(payload["manifest_type"], "symlink_snapshot")
            self.assertEqual(
                payload["entries"][0]["target"], "/reserved-example/missing"
            )

    def test_missing_profile_mount_fails_without_creating_legacy_directory(self):
        storage = Mock()
        storage.write_json.side_effect = ValueError(
            "Selected backup mount is unavailable"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary, "links")
            root.mkdir()
            legacy = Path(temporary, "legacy", "snapshot.json")
            with patch.object(helpers, "get_backup_storage", return_value=storage):
                with self.assertRaisesRegex(ValueError, "mount is unavailable"):
                    helpers.backup_symlink_manifest(
                        [str(root)], str(legacy), backup_destination_id="nas"
                    )
            self.assertFalse(legacy.parent.exists())

    def test_registered_restore_reads_verified_catalog_artifact_then_restores_links(
        self,
    ):
        storage = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            link = Path(temporary, "restored", "movie")
            storage.read_json.return_value = {
                "manifest_type": "symlink_snapshot",
                "entries": [
                    {"link_path": str(link), "target": "/reserved-example/missing"}
                ],
            }
            with patch.object(helpers, "get_backup_storage", return_value=storage):
                preview = helpers.preview_symlink_manifest_restore(REFERENCE)
                self.assertFalse(os.path.lexists(link))
                report = helpers.restore_symlink_manifest(REFERENCE, dry_run=False)
            self.assertEqual(preview["projected_restored"], 1)
            self.assertEqual(report["restored"], 1)
            self.assertEqual(os.readlink(link), "/reserved-example/missing")
            storage.read_json.assert_called_with(
                REFERENCE, expected_kind="symlink_manifest"
            )

    def test_managed_reader_uses_catalog_only_and_preserves_exact_bindings(self):
        storage = Mock()
        storage.bound_identity.return_value = {"destination_id": "nas"}
        manifest = {
            "manifest_type": "symlink_snapshot",
            "process_name": "InfiniDysk",
            "entries": [],
        }
        storage.read_json.return_value = manifest
        with (
            patch.object(helpers, "get_backup_storage", return_value=storage),
            patch.object(
                helpers,
                "read_symlink_manifest",
                side_effect=AssertionError("legacy dispatch"),
            ),
            patch.object(
                helpers.os.path, "exists", side_effect=AssertionError("raw exists")
            ),
            patch("builtins.open", side_effect=AssertionError("raw open")),
        ):
            result = helpers.read_managed_symlink_manifest(
                REFERENCE, process_name="InfiniDysk", backup_destination_id="nas"
            )
        self.assertIs(result, manifest)
        storage.bound_identity.assert_called_once_with("nas")
        storage.read_json.assert_called_once_with(
            REFERENCE, expected_kind="symlink_manifest"
        )

    def test_managed_reader_rejects_paths_and_noncanonical_ids_before_storage(self):
        invalid = [
            "/reserved-example/custom/snapshot.json",
            REFERENCE.replace("//nas/", "//../"),
            REFERENCE.replace(NAMESPACE, "../" + NAMESPACE),
            REFERENCE[:-32] + "not-an-artifact",
            REFERENCE[:-32] + "A" * 32,
            REFERENCE + "?path=/reserved-example/snapshot.json",
            REFERENCE + "#fragment",
            REFERENCE + "/",
            REFERENCE.replace("//nas/", "//nas//"),
            REFERENCE.replace("//nas/", "//nas%2fother/"),
        ]
        with (
            patch.object(helpers, "get_backup_storage") as storage,
            patch.object(
                helpers.os.path, "exists", side_effect=AssertionError("raw exists")
            ),
            patch("builtins.open", side_effect=AssertionError("raw open")),
        ):
            for reference in invalid:
                with self.subTest(reference=reference):
                    with self.assertRaises((BackupStorageError, ValueError)):
                        helpers.read_managed_symlink_manifest(reference)
        storage.assert_not_called()

    def test_reference_listing_never_enters_legacy_reader(self):
        storage = Mock()
        storage.read_json.return_value = {
            "manifest_type": "symlink_snapshot",
            "process_name": "InfiniDysk",
            "entries": [],
        }
        storage.list_artifacts.return_value = [receipt()]
        with (
            patch.object(helpers, "get_backup_storage", return_value=storage),
            patch.object(
                helpers,
                "read_symlink_manifest",
                side_effect=AssertionError("legacy dispatch"),
            ),
            patch.object(
                helpers.os.path, "exists", side_effect=AssertionError("raw exists")
            ),
            patch("builtins.open", side_effect=AssertionError("raw open")),
        ):
            result = helpers.list_symlink_backup_manifests(
                process_name="InfiniDysk", reference=REFERENCE
            )
        self.assertEqual(result["files"][0]["reference"], REFERENCE)
        storage.read_json.assert_called_once_with(
            REFERENCE, expected_kind="symlink_manifest"
        )
        storage.list_artifacts.assert_called_once_with("nas", NAMESPACE)

    def test_legacy_custom_absolute_manifest_still_reads_without_storage(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary, "custom", "snapshot.json")
            source.parent.mkdir()
            manifest = {"manifest_type": "symlink_snapshot", "entries": []}
            source.write_text(json.dumps(manifest), encoding="utf-8")
            with patch.object(
                helpers,
                "get_backup_storage",
                side_effect=AssertionError("managed storage"),
            ):
                self.assertEqual(helpers.read_symlink_manifest(str(source)), manifest)
                preview = helpers.preview_symlink_manifest_restore(str(source))
            self.assertEqual(preview["projected_restored"], 0)

    def test_hash_or_binding_failure_cannot_start_restore(self):
        storage = Mock()
        storage.read_json.side_effect = ValueError("Backup content hash mismatch")
        with patch.object(helpers, "get_backup_storage", return_value=storage):
            with patch.object(helpers.os, "symlink") as create:
                with self.assertRaisesRegex(ValueError, "hash mismatch"):
                    helpers.restore_symlink_manifest(REFERENCE, dry_run=False)
                create.assert_not_called()

    def test_non_symlink_artifact_namespace_is_rejected_before_read(self):
        storage = Mock()
        wrong = REFERENCE.replace(NAMESPACE, "major-migration")
        with patch.object(helpers, "get_backup_storage", return_value=storage):
            with self.assertRaisesRegex(ValueError, "does not identify a symlink"):
                helpers.read_symlink_manifest(wrong)
        storage.read_json.assert_not_called()

    def test_registered_listing_uses_only_catalog_namespace_not_filesystem_browse(self):
        storage = Mock()
        storage.list_artifacts.return_value = [receipt()]
        with patch.object(helpers, "get_backup_storage", return_value=storage):
            with patch.object(
                helpers.os, "listdir", side_effect=AssertionError("raw browse")
            ):
                result = helpers.list_symlink_backup_manifests("nas", "InfiniDysk")
        storage.list_artifacts.assert_called_once_with("nas", NAMESPACE)
        self.assertEqual(result["files"][0]["path"], REFERENCE)
        self.assertEqual(result["files"][0]["sha256"], "b" * 64)
        self.assertEqual(result["count"], 1)

    def test_listing_by_reference_validates_it_before_listing_its_namespace(self):
        storage = Mock()
        storage.read_json.side_effect = ValueError("Unregistered backup")
        with patch.object(helpers, "get_backup_storage", return_value=storage):
            with self.assertRaisesRegex(ValueError, "Unregistered"):
                helpers.list_symlink_backup_manifests(reference=REFERENCE)
        storage.list_artifacts.assert_not_called()

    def test_retention_keeps_just_written_reference_when_timestamps_tie(self):
        storage = Mock()
        other = REFERENCE[:-32] + "f" * 32
        storage.list_artifacts.return_value = [receipt(other), receipt()]
        with patch.object(helpers, "get_backup_storage", return_value=storage):
            report = helpers.prune_symlink_backup_manifests(
                "nas", "InfiniDysk", 1, latest_reference=REFERENCE
            )
        self.assertEqual(report["pruned"], 1)
        storage.delete_artifact.assert_called_once_with(other)
        storage.list_artifacts.assert_called_once_with("nas", NAMESPACE)

    def test_retention_rejects_mismatched_profile_or_missing_latest_receipt(self):
        storage = Mock()
        with patch.object(helpers, "get_backup_storage", return_value=storage):
            with self.assertRaisesRegex(ValueError, "does not match"):
                helpers.prune_symlink_backup_manifests(
                    "other", "InfiniDysk", 1, latest_reference=REFERENCE
                )
            storage.list_artifacts.assert_not_called()
            other = REFERENCE[:-32] + "f" * 32
            storage.list_artifacts.return_value = [receipt(other)]
            with self.assertRaisesRegex(ValueError, "receipt is missing"):
                helpers.prune_symlink_backup_manifests(
                    "nas", "InfiniDysk", 1, latest_reference=REFERENCE
                )
        storage.delete_artifact.assert_not_called()

    def test_disabled_retention_does_not_touch_registered_storage(self):
        with patch.object(helpers, "get_backup_storage") as storage:
            report = helpers.prune_symlink_backup_manifests("nas", "InfiniDysk", 0)
        self.assertEqual(report["pruned"], 0)
        storage.assert_not_called()

    def test_distinct_process_names_cannot_share_retention_after_slug_normalization(
        self,
    ):
        self.assertNotEqual(
            helpers.symlink_backup_namespace("Backend 1"),
            helpers.symlink_backup_namespace("Backend-1"),
        )
        self.assertNotEqual(
            helpers.symlink_backup_namespace("Backend"),
            helpers.symlink_backup_namespace("backend"),
        )
        self.assertNotEqual(
            helpers.symlink_backup_namespace("X" * 200 + "a"),
            helpers.symlink_backup_namespace("X" * 200 + "b"),
        )

    def test_namespace_cannot_escape_or_exceed_provider_bound(self):
        namespace = helpers.symlink_backup_namespace("../" + "A" * 200)
        self.assertLessEqual(len(namespace), 80)
        self.assertRegex(namespace, r"^symlink-[a-z0-9_-]+$")


class RegisteredSymlinkRoundTripTests(unittest.TestCase):
    def setUp(self):
        from utils.backup_storage import BackupStorage

        temporary = tempfile.TemporaryDirectory(prefix="dumb-symlink-storage-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "mounted-backup"
        self.destination.mkdir(mode=0o700)
        self.config = types.SimpleNamespace(
            file_path=str(self.root / "config.json"),
            config={"backup_storage": {"profiles": {}, "default_destination_id": ""}},
            save_config=Mock(),
        )
        self.storage = BackupStorage(self.config, state_root=self.root / "catalog")
        self.storage.save_profile(
            {"id": "nas", "path": str(self.destination), "enabled": True}
        )
        self.links = self.root / "links"
        self.links.mkdir()
        (self.links / "movie").symlink_to("/reserved-example/missing")
        item = patch.object(helpers, "get_backup_storage", return_value=self.storage)
        item.start()
        self.addCleanup(item.stop)

    def write(self, process_name, *, scheduled=False):
        return helpers.backup_symlink_manifest(
            [str(self.links)],
            backup_destination_id="nas",
            process_name=process_name,
            scheduled=scheduled,
        )

    def test_actual_registered_write_read_and_listing_preserve_process_kind_and_hash(
        self,
    ):
        report = self.write("InfiniDysk")
        reference = report["backup_manifest"]
        self.assertEqual(report["storage_receipt"]["artifact_kind"], "symlink_manifest")
        payload = helpers.read_symlink_manifest(
            reference, process_name="InfiniDysk", backup_destination_id="nas"
        )
        self.assertEqual(payload["process_name"], "InfiniDysk")
        self.assertEqual(payload["entries"][0]["link_path"], str(self.links / "movie"))
        listing = helpers.list_symlink_backup_manifests("nas", "InfiniDysk")
        self.assertEqual([row["path"] for row in listing["files"]], [reference])
        self.assertEqual(listing["files"][0]["sha256"], report["sha256"])

    def test_actual_retention_isolated_for_colliding_readable_process_slugs(self):
        first = self.write("Backend 1", scheduled=True)
        newest = self.write("Backend 1", scheduled=True)
        other = self.write("Backend-1", scheduled=True)
        manual = self.write("Backend 1")
        foreign_file = self.destination / "operator-backup.json"
        foreign_file.write_text("retained")
        result = helpers.prune_symlink_backup_manifests(
            "nas", "Backend 1", 1, latest_reference=newest["backup_manifest"]
        )
        self.assertEqual(result["pruned"], 1, result)
        self.assertNotEqual(first["backup_manifest"], newest["backup_manifest"])
        self.assertEqual(
            helpers.list_symlink_backup_manifests("nas", "Backend 1")["count"], 2
        )
        self.assertEqual(
            helpers.list_symlink_backup_manifests(
                "nas", "Backend 1", scheduled_only=True
            )["count"],
            1,
        )
        self.assertEqual(
            helpers.read_symlink_manifest(manual["backup_manifest"])["retention_scope"],
            "manual",
        )
        self.assertEqual(
            helpers.list_symlink_backup_manifests("nas", "Backend-1")["count"], 1
        )
        self.assertEqual(
            helpers.read_symlink_manifest(other["backup_manifest"])["process_name"],
            "Backend-1",
        )
        self.assertEqual(foreign_file.read_text(), "retained")

    def test_default_switch_before_retention_cannot_prune_the_new_default(self):
        self.storage.set_default("nas")
        self.write("InfiniDysk", scheduled=True)
        latest = helpers.backup_symlink_manifest(
            [str(self.links)],
            backup_destination_id="@default",
            process_name="InfiniDysk",
            scheduled=True,
        )
        second = self.root / "other-backup"
        second.mkdir(mode=0o700)
        self.storage.save_profile(
            {"id": "other", "path": str(second), "enabled": True, "make_default": True}
        )
        for _ in range(2):
            helpers.backup_symlink_manifest(
                [str(self.links)],
                backup_destination_id="other",
                process_name="InfiniDysk",
                scheduled=True,
            )
        result = helpers.prune_symlink_backup_manifests(
            "@default", "InfiniDysk", 1, latest_reference=latest["backup_manifest"]
        )
        self.assertEqual(result["pruned"], 1)
        self.assertEqual(
            helpers.list_symlink_backup_manifests("nas", "InfiniDysk")["count"], 1
        )
        self.assertEqual(
            helpers.list_symlink_backup_manifests("other", "InfiniDysk")["count"], 2
        )

    def test_actual_replaced_destination_never_creates_local_fallback(self):
        self.write("InfiniDysk")
        retained = self.root / "detached-backup"
        self.destination.rename(retained)
        self.destination.mkdir(mode=0o700)
        fallback = self.root / "legacy-fallback" / "snapshot.json"
        with self.assertRaisesRegex(BackupStorageError, "missing or replaced"):
            helpers.backup_symlink_manifest(
                [str(self.links)],
                str(fallback),
                backup_destination_id="nas",
                process_name="InfiniDysk",
            )
        self.assertFalse(fallback.parent.exists())
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertTrue((retained / ".dumb-backups").is_dir())

    def test_actual_process_and_explicit_old_profile_must_match_registered_reference(
        self,
    ):
        reference = self.write("InfiniDysk")["backup_manifest"]
        second = self.root / "other-backup"
        second.mkdir(mode=0o700)
        self.storage.save_profile(
            {"id": "other", "path": str(second), "enabled": True, "make_default": True}
        )
        with self.assertRaisesRegex(ValueError, "different service process"):
            helpers.read_symlink_manifest(reference, process_name="infinidysk")
        with self.assertRaisesRegex(ValueError, "different selected destination"):
            helpers.read_symlink_manifest(reference, backup_destination_id="@default")
        restored = helpers.read_symlink_manifest(
            reference, process_name="InfiniDysk", backup_destination_id="nas"
        )
        self.assertEqual(restored["process_name"], "InfiniDysk")

    def test_actual_catalog_kind_required_even_inside_owned_symlink_namespace(self):
        namespace = helpers.symlink_backup_namespace("InfiniDysk")
        fake = self.storage.write_json(
            "nas", namespace, {"manifest_type": "symlink_snapshot", "entries": []}
        )
        with self.assertRaisesRegex(BackupStorageError, "kind"):
            helpers.read_symlink_manifest(fake["reference"])
        self.assertEqual(
            helpers.list_symlink_backup_manifests("nas", "InfiniDysk")["count"], 0
        )


if __name__ == "__main__":
    unittest.main()
