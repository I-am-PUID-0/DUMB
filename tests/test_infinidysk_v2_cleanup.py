"""Private filesystem fixtures for explicit, resumable v1 retirement."""

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote

from utils import infinidysk_v2_cleanup as cleanup_module
from utils.backup_storage import BackupStorage, parse_backup_reference
from utils.infinidysk_v2_archive import create_retained_archive
from utils.infinidysk_v2_cleanup import InfiniDyskV2Cleanup
from utils.infinidysk_v2_migration import (
    InfiniDyskV2MigrationError,
    InfiniDyskV2MigrationManager,
    _digest,
    _identity,
    _source_fingerprint,
)


class V1CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dumb-v1-cleanup-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data, self.runtime = self.root / "v1-data", self.root / "v1-runtime"
        self.v2data, self.v2runtime = self.root / "v2-data", self.root / "v2-runtime"
        for path in (self.data, self.runtime, self.v2data, self.v2runtime):
            path.mkdir()
        (self.data / "db.sqlite").write_bytes(b"retained database fixture")
        (self.data / "content").mkdir()
        (self.data / "content" / "one").write_bytes(b"retained blob")
        (self.data / "link").symlink_to("content/one")
        (self.runtime / "app.dll").write_bytes(b"legacy runtime")
        (self.v2data / "live.sqlite").write_bytes(b"current v2 data")
        self.config = SimpleNamespace(
            config={
                "data_root": str(self.root),
                "infinidysk": {
                    "runtime_generation": "2",
                    "process_name": "InfiniDysk",
                    "config_dir": str(self.runtime),
                    "env": {"CONFIG_PATH": str(self.data)},
                    "v2_data_dir": str(self.v2data),
                    "v2_runtime_dir": str(self.v2runtime),
                    "mount_path": str(self.root / "delivery"),
                    "mount_backend": "fuse",
                },
            },
            save_config=Mock(),
        )
        self.updater = SimpleNamespace(updating=threading.Lock())
        self.manager = InfiniDyskV2MigrationManager(
            config_manager=self.config,
            state_root=self.root / "state",
            updater=self.updater,
        )
        self.manager._process_identity = Mock(return_value={"pid": 1234})
        self.controller = InfiniDyskV2Cleanup(self.manager)
        self.manager._cleanup = self.controller
        self.job_id = "a" * 32
        backup = self.manager._directory(
            self.manager.state_root / "backups" / self.job_id
        )
        shutil.copytree(self.data, backup / "data", symlinks=True)
        shutil.copytree(self.runtime, backup / "runtime", symlinks=True)
        fingerprints = {
            str(path): _source_fingerprint(path, scratch_root=backup)
            for path in (self.data, self.runtime)
        }
        plan = {
            "mode": "migrate",
            "source": {
                "process_name": "InfiniDysk",
                "data_dir": str(self.data),
                "runtime_dir": str(self.runtime),
                "data_identity": _identity(self.data),
                "runtime_identity": {"directory": _identity(self.runtime)},
                "aliases": [],
            },
            "target": {
                "data_dir": str(self.v2data),
                "runtime_dir": str(self.v2runtime),
                "mount_path": str(self.root / "delivery"),
            },
            "owned_config_paths": [["infinidysk"]],
        }
        self.job = {
            "job_id": self.job_id,
            "job_type": "infinidysk_v1_to_v2",
            "protocol": 1,
            "status": "completed",
            "mode": "cutover",
            "plan": plan,
            "plan_fingerprint": "b" * 64,
            "result": {
                "validated": True,
                "execution_verified": True,
                "plan_fingerprint": "b" * 64,
            },
            "accepted_import": {
                "validated": True,
                "plan_fingerprint": "b" * 64,
                "snapshot_fingerprint": _digest(fingerprints),
            },
            "activation_fingerprint": _digest(
                self.manager._owned(self.config.config, plan)
            ),
            "snapshot": {
                "source_snapshot": str(backup),
                "data_snapshot": str(backup / "data"),
                "runtime_snapshot": str(backup / "runtime"),
                "original_fingerprints": fingerprints,
                "snapshot_fingerprint": _digest(fingerprints),
            },
        }
        self.manager._write(self.manager._job_path(self.job_id), self.job)
        for name, value in (
            ("native_api_request", {"status": 200}),
            ("delivery_mount_ready", True),
        ):
            active = patch.object(cleanup_module, name, return_value=value)
            active.start()
            self.addCleanup(active.stop)

    def _preview(self):
        return self.controller.preview({"job_id": self.job_id})

    def _request(self, preview=None):
        preview = preview or self._preview()
        self.assertTrue(preview["ready"], preview)
        return {
            "job_id": self.job_id,
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": dict.fromkeys(cleanup_module.ACKS, True),
        }

    def _run(self, request=None):
        self.controller.start(request or self._request())
        self.controller._thread.join(timeout=10)
        self.assertFalse(self.controller._thread.is_alive())
        self.assertFalse(self.updater.updating.locked())
        return self.manager._read(self.manager._job_path(self.job_id))

    def test_preview_is_read_only_and_complete_cleanup_keeps_backups_and_v2(self):
        before = _source_fingerprint(
            self.data, scratch_root=Path(self.job["snapshot"]["source_snapshot"])
        )
        preview = self._preview()
        self.assertTrue(preview["ready"], preview)
        self.assertEqual(2, len(preview["targets"]))
        self.assertTrue(all(row["kept"] for row in preview["kept_backups"]))
        self.assertEqual(
            before,
            _source_fingerprint(
                self.data, scratch_root=Path(self.job["snapshot"]["source_snapshot"])
            ),
        )
        job = self._run(self._request(preview))
        self.assertEqual("completed", job["status"])
        self.assertEqual("completed", job["cleanup"]["state"])
        self.assertTrue(job["rollback_retired"])
        self.assertFalse(self.manager._public_job(job)["can_rollback"])
        with self.assertRaises(InfiniDyskV2MigrationError):
            self.manager.rollback(
                {
                    "job_id": self.job_id,
                    "confirmation": f"ROLLBACK INFINIDYSK V2 {self.job_id}",
                }
            )
        self.assertFalse(self.data.exists())
        self.assertFalse(self.runtime.exists())
        self.assertEqual(b"current v2 data", (self.v2data / "live.sqlite").read_bytes())
        self.assertTrue(Path(self.job["snapshot"]["data_snapshot"]).is_dir())
        self.assertTrue(Path(job["cleanup"]["manifest_path"]).is_file())

    def test_acknowledgements_expiration_and_changed_config_block_before_mutation(self):
        preview = self._preview()
        request = self._request(preview)
        invalid = copy.deepcopy(request)
        invalid["acknowledgements"]["playback"] = "true"
        with self.assertRaises(InfiniDyskV2MigrationError):
            self.controller.start(invalid)
        with patch.object(
            cleanup_module.time, "time", return_value=preview["expires_at"] + 1
        ):
            with self.assertRaises(InfiniDyskV2MigrationError):
                self.controller.start(request)
        self.config.config["unrelated"] = "changed after review"
        with self.assertRaises(InfiniDyskV2MigrationError):
            self.controller.start(request)
        self.assertTrue((self.data / "db.sqlite").exists())
        self.assertNotIn(
            "rollback_retired", self.manager._read(self.manager._job_path(self.job_id))
        )

    def test_unhealthy_native_shared_paths_and_source_mounts_block_preview(self):
        with patch.object(
            cleanup_module, "native_api_request", return_value={"status": 503}
        ):
            self.assertFalse(self._preview()["ready"])
        self.config.config["another_service"] = {
            "library_path": str(self.data / "content")
        }
        self.assertFalse(self._preview()["ready"])
        self.config.config.pop("another_service")
        with patch.object(
            cleanup_module,
            "mount_records",
            return_value=[{"path": str(self.data / "content")}],
        ):
            self.assertFalse(self._preview()["ready"])
        self.assertTrue((self.data / "content" / "one").exists())

    def test_changed_original_and_corrupt_backup_fail_before_retirement(self):
        (self.data / "db.sqlite").write_bytes(b"changed original")
        job = self._run()
        self.assertEqual("failed", job["cleanup"]["state"])
        self.assertFalse(job.get("rollback_retired", False))
        self.assertTrue((self.runtime / "app.dll").exists())

    def test_shared_reference_with_spaces_or_command_assignment_is_blocked(self):
        moved = self.root / "old library"
        self.data.rename(moved)
        old_path = str(self.data)
        self.data = moved
        self.job["plan"]["source"]["data_dir"] = str(moved)
        fingerprints = self.job["snapshot"]["original_fingerprints"]
        fingerprints[str(moved)] = fingerprints.pop(old_path)
        self.job["snapshot"]["snapshot_fingerprint"] = _digest(fingerprints)
        self.job["accepted_import"]["snapshot_fingerprint"] = _digest(fingerprints)
        self.config.config["infinidysk"]["env"]["CONFIG_PATH"] = str(moved)
        self.manager._write(self.manager._job_path(self.job_id), self.job)
        for reference in (str(moved / "content"), f'--library="{moved}/content"'):
            self.config.config["other_service"] = {"path": reference}
            preview = self._preview()
            self.assertFalse(preview["ready"], reference)
            self.assertTrue((moved / "db.sqlite").exists())

    def test_corrupt_retained_backup_never_retires_or_deletes_v1(self):
        (Path(self.job["snapshot"]["data_snapshot"]) / "db.sqlite").write_bytes(
            b"bad backup"
        )
        job = self._run()
        self.assertEqual("failed", job["cleanup"]["state"])
        self.assertFalse(job.get("rollback_retired", False))
        self.assertTrue((self.data / "db.sqlite").exists())

    def _cold_backup(self):
        destination = self.root / "cold-storage"
        destination.mkdir(mode=0o700)
        storage = BackupStorage(self.config, state_root=self.root / "storage-control")
        storage.save_profile(
            {
                "id": "nas",
                "name": "Cold archives",
                "path": str(destination),
                "enabled": True,
            }
        )
        backup_root = Path(self.job["snapshot"]["source_snapshot"])
        config_backup = backup_root / "dumb-config.json"
        config_backup.write_text('{"example":true}')
        config_backup.chmod(0o600)
        self.job["plan"]["backup_storage"] = {"destination_id": "nas"}
        archive = create_retained_archive(
            self.job["plan"],
            {"root": str(backup_root), "config_backup": str(config_backup)},
            storage,
            self.job["snapshot"]["original_fingerprints"],
        )
        self.job["snapshot"].update(
            cold_archive=archive["receipt"], archive_roots=archive["archive_roots"]
        )
        self.manager._write(self.manager._job_path(self.job_id), self.job)
        active = patch("utils.backup_storage.get_backup_storage", return_value=storage)
        active.start()
        self.addCleanup(active.stop)
        return destination, archive["receipt"]

    def test_cleanup_fully_verifies_and_retains_the_actual_cold_archive(self):
        destination, receipt = self._cold_backup()
        job = self._run()
        self.assertEqual("completed", job["cleanup"]["state"])
        self.assertTrue(job["rollback_retired"])
        self.assertEqual(
            [receipt["artifact_id"] + ".tar"],
            [path.name for path in destination.rglob("*.tar")],
        )
        self.assertFalse(self.data.exists())
        self.assertTrue((self.v2data / "live.sqlite").exists())

    def test_corrupt_cold_archive_stops_before_v1_retirement(self):
        destination, receipt = self._cold_backup()
        profile, namespace, artifact = parse_backup_reference(receipt["reference"])
        path = destination / ".dumb-backups" / profile / namespace / f"{artifact}.tar"
        with path.open("r+b") as handle:
            handle.seek(1024)
            handle.write(b"changed archive")
        job = self._run()
        self.assertEqual("failed", job["cleanup"]["state"])
        self.assertFalse(job.get("rollback_retired", False))
        self.assertTrue((self.data / "db.sqlite").exists())

    def test_unavailable_archive_destination_is_a_blocked_preview(self):
        self._cold_backup()
        self.config.config["backup_storage"]["profiles"]["nas"]["enabled"] = False
        preview = self._preview()
        self.assertFalse(preview["ready"])
        self.assertTrue(
            any("destination" in message for message in preview["blockers"])
        )
        self.assertTrue((self.data / "db.sqlite").exists())

    def _legacy_postgres_backup(self):
        parent = Path(self.job["snapshot"]["source_snapshot"]) / "postgres-source"
        parent.mkdir(mode=0o700)
        dump, staged = parent / "v1-postgres.dump", parent / "source.sqlite"
        dump.write_bytes(b"verified PostgreSQL backup fixture")
        staged.write_bytes(b"closed native import snapshot fixture")
        dump.chmod(0o600)
        staged.chmod(0o600)
        self.job["plan"]["source"]["provider"] = "postgres"
        self.job["snapshot"]["postgres_snapshot"] = {
            "source_read_only": True,
            "verified_restore": True,
            "source_binding": {"binding_fingerprint": "c" * 64},
            "restore_proof": {"verified": True, "source_binding": "c" * 64},
            "dump_path": str(dump),
            "staged_sqlite": str(staged),
            "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
            "staged_sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
        }
        self.manager._write(self.manager._job_path(self.job_id), self.job)
        return dump, staged

    def test_legacy_postgres_backup_is_verified_and_kept_during_retirement(self):
        dump, staged = self._legacy_postgres_backup()
        job = self._run()
        self.assertEqual("completed", job["cleanup"]["state"])
        self.assertTrue(dump.exists())
        self.assertTrue(staged.exists())

    def test_corrupt_legacy_postgres_dump_prevents_retirement(self):
        dump, _staged = self._legacy_postgres_backup()
        dump.write_bytes(b"corrupt independent backup")
        job = self._run()
        self.assertEqual("failed", job["cleanup"]["state"])
        self.assertFalse(job.get("rollback_retired", False))
        self.assertTrue((self.data / "db.sqlite").exists())

    def test_new_unreviewed_entry_stops_deletion_and_remains(self):
        original_delete = self.controller._delete

        def add_new(job, roots):
            (self.data / "new-unreviewed").write_bytes(b"preserve me")
            return original_delete(job, roots)

        with patch.object(self.controller, "_delete", side_effect=add_new):
            job = self._run()
        self.assertEqual("failed", job["cleanup"]["state"])
        self.assertTrue(job["rollback_retired"])
        self.assertEqual(b"preserve me", (self.data / "new-unreviewed").read_bytes())
        resumed = self._run()
        self.assertEqual("failed", resumed["cleanup"]["state"])
        self.assertTrue((self.data / "new-unreviewed").exists())

    def test_replacement_after_manifest_is_preserved(self):
        original_delete = self.controller._delete

        def replace_entry(job, roots):
            path = self.data / "content" / "one"
            path.unlink()
            path.write_bytes(b"new owner")
            return original_delete(job, roots)

        with patch.object(self.controller, "_delete", side_effect=replace_entry):
            job = self._run()
        self.assertEqual("failed", job["cleanup"]["state"])
        self.assertTrue(job["rollback_retired"])
        self.assertEqual(b"new owner", (self.data / "content" / "one").read_bytes())

    def test_retirement_is_on_disk_before_first_unlink_and_resume_uses_same_manifest(
        self,
    ):
        real_unlink = os.unlink
        attempted = []

        def interrupt(name, *args, **kwargs):
            parent_fd = kwargs.get("dir_fd")
            parent = (
                Path(os.readlink(f"/proc/self/fd/{parent_fd}"))
                if parent_fd is not None
                else None
            )
            if parent is not None and any(
                parent == root or root in parent.parents
                for root in (self.data, self.runtime)
            ):
                retained = self.manager._read(self.manager._job_path(self.job_id))
                self.assertTrue(retained["rollback_retired"])
                attempted.append(name)
                if len(attempted) == 2:
                    raise OSError("simulated interrupted cleanup")
            return real_unlink(name, *args, **kwargs)

        with patch.object(cleanup_module.os, "unlink", side_effect=interrupt):
            job = self._run()
        self.assertEqual("failed", job["cleanup"]["state"])
        self.assertTrue(job["rollback_retired"])
        manifest = job["cleanup"]["manifest_path"]
        resumed = self._run()
        self.assertEqual("completed", resumed["cleanup"]["state"])
        self.assertEqual(manifest, resumed["cleanup"]["manifest_path"])
        self.assertFalse(self.data.exists())

    def test_manifest_tampering_is_blocked(self):
        original_delete = self.controller._delete

        def tamper(job, roots):
            with Path(job["cleanup"]["manifest_path"]).open("ab") as handle:
                handle.write(b"changed whitelist")
            return original_delete(job, roots)

        with patch.object(self.controller, "_delete", side_effect=tamper):
            job = self._run()
        self.assertEqual("failed", job["cleanup"]["state"])
        self.assertTrue((self.data / "db.sqlite").exists())

    def test_shared_hardlink_blocks_before_retirement(self):
        os.link(self.data / "content" / "one", self.root / "independent-link")
        job = self._run()
        self.assertEqual("failed", job["cleanup"]["state"])
        self.assertFalse(job.get("rollback_retired", False))
        self.assertTrue((self.root / "independent-link").exists())
        self.assertTrue((self.data / "content" / "one").exists())

    def test_replaced_manifest_between_file_verification_and_sqlite_open_is_rejected(
        self,
    ):
        roots = self.controller._roots(self.job)
        self.job["cleanup"] = self.controller._manifest(self.job, roots)
        path = Path(self.job["cleanup"]["manifest_path"])
        replacement = path.with_suffix(".replacement.sqlite")
        shutil.copyfile(path, replacement)
        with sqlite3.connect(replacement) as connection:
            connection.execute(
                "UPDATE entries SET stamp=? WHERE root=0 AND path=?",
                (b"[0]", b"db.sqlite"),
            )
        real_connect = sqlite3.connect

        def replace_before_connect(database, *args, **kwargs):
            os.replace(replacement, path)
            return real_connect(database, *args, **kwargs)

        with patch.object(
            cleanup_module.sqlite3, "connect", side_effect=replace_before_connect
        ):
            with self.assertRaises(InfiniDyskV2MigrationError):
                self.controller._open_manifest(self.job)
        self.assertTrue((self.data / "db.sqlite").exists())

    def test_verified_sqlite_read_transaction_survives_pathname_replacement(self):
        self.job["cleanup"] = self.controller._manifest(
            self.job, self.controller._roots(self.job)
        )
        path = Path(self.job["cleanup"]["manifest_path"])
        replacement = path.with_suffix(".replacement.sqlite")
        shutil.copyfile(path, replacement)
        with sqlite3.connect(replacement) as connection:
            connection.execute("DELETE FROM entries")
        connection = self.controller._open_manifest(self.job)
        try:
            expected = connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            self.assertGreater(expected, 0)
            os.replace(replacement, path)
            self.assertEqual(
                expected,
                connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0],
            )
        finally:
            connection.close()

    def test_legacy_empty_backup_preference_preserves_exact_config_rollback_guard(self):
        current = copy.deepcopy(self.config.config)
        original = copy.deepcopy(current)
        original["infinidysk"]["runtime_generation"] = "1"
        self.job["plan"]["config_snapshot"] = original
        self.job["activation_fingerprint"] = _digest(
            self.manager._owned(current, self.job["plan"])
        )
        self.config.config["infinidysk"]["backup_destination_id"] = ""
        self.manager._restore_config(self.job)
        self.assertEqual("1", self.config.config["infinidysk"]["runtime_generation"])
        self.config.config = copy.deepcopy(current)
        self.config.config["infinidysk"]["backup_destination_id"] = "nas"
        with self.assertRaises(InfiniDyskV2MigrationError):
            self.manager._restore_config(self.job)

    def test_dead_worker_public_state_preserves_retirement(self):
        job = {
            **self.job,
            "rollback_retired": True,
            "cleanup": {"state": "deleting", "manifest_ready": True},
        }
        public = self.controller.public_state(job)
        self.assertTrue(public["rollback_retired"])
        self.assertEqual("failed", public["cleanup"]["state"])
        self.assertTrue(public["cleanup"]["resumable"])
        self.assertFalse(self.controller.has_recovery_pending_job())

    def test_fresh_v2_install_does_not_offer_v1_retirement(self):
        self.job["plan"]["mode"] = "fresh"
        self.manager._write(self.manager._job_path(self.job_id), self.job)
        self.assertFalse(self.controller.public_state(self.job)["can_cleanup"])
        with self.assertRaises(InfiniDyskV2MigrationError):
            self._preview()

    def test_import_proof_from_a_different_job_cannot_authorize_cleanup(self):
        self.job["accepted_import"]["plan_fingerprint"] = "c" * 64
        self.manager._write(self.manager._job_path(self.job_id), self.job)
        with self.assertRaises(InfiniDyskV2MigrationError):
            self._preview()
        self.assertTrue((self.data / "db.sqlite").exists())

    def _move_data_for_uri_test(self):
        previous = self.data
        self.data = self.root / "v1 data"
        previous.rename(self.data)
        self.job["plan"]["source"]["data_dir"] = str(self.data)
        fingerprints = self.job["snapshot"]["original_fingerprints"]
        fingerprints[str(self.data)] = fingerprints.pop(str(previous))
        fingerprint = _digest(fingerprints)
        self.job["snapshot"]["snapshot_fingerprint"] = fingerprint
        self.job["accepted_import"]["snapshot_fingerprint"] = fingerprint
        self.config.config["infinidysk"]["env"]["CONFIG_PATH"] = str(self.data)
        self.manager._write(self.manager._job_path(self.job_id), self.job)

    def test_local_database_uri_consumers_block_cleanup_preview_without_source_changes(
        self,
    ):
        self._move_data_for_uri_test()
        path = str(self.data / "db.sqlite")
        encoded = quote(path)
        references = (
            "file:" + path,
            "file://" + path,
            "file://localhost" + path,
            "file:" + encoded + "?mode=ro&cache=shared",
            "sqlite:" + path,
            "sqlite://" + path,
            "sqlite:///" + path,
            "sqlite+pysqlite:///" + path,
            "sqlite+aiosqlite:///" + encoded + "?mode=ro&cache=shared",
            "sqlite:///file:" + encoded + "?uri=true&mode=ro",
            "sqlite+aiosqlite:///file://localhost" + encoded + "?uri=true",
            "--database='sqlite:///" + path + "?mode=ro&cache=shared'",
            "DATABASE_URL=sqlite:///" + encoded + "?mode=ro",
            "sqlite:////" + path,
            "//" + path.lstrip("/"),
        )
        before = _source_fingerprint(self.data, scratch_root=self.manager.state_root)
        for reference in references:
            with self.subTest(reference=reference):
                self.config.config["another_service"] = {
                    "env": {"DATABASE_URL": reference}
                }
                preview = self._preview()
                self.assertFalse(preview["ready"])
                self.assertTrue(
                    any("references the v1" in item for item in preview["blockers"])
                )
        self.assertEqual(
            before, _source_fingerprint(self.data, scratch_root=self.manager.state_root)
        )
        self.assertIsNone(self.controller._thread)
        self.assertFalse(
            self.manager._read(self.manager._job_path(self.job_id)).get(
                "rollback_retired", False
            )
        )

    def test_remote_and_relative_database_urls_do_not_become_local_references(self):
        self._move_data_for_uri_test()
        path = str(self.data / "db.sqlite")
        encoded = quote(path)
        values = (
            "https://example.invalid" + encoded + "?database=" + encoded,
            "postgresql://app:reserved-example@127.0.0.1" + encoded,
            "file://example.invalid" + encoded,
            "file:relative/data.sqlite",
            "sqlite+aiosqlite:///" + encoded.lstrip("/"),
            "sqlite+pysqlite:///" + encoded.lstrip("/"),
            "sqlite:///:memory:",
            "file:" + quote(encoded),
        )
        for value in values:
            with self.subTest(reference=value):
                self.config.config["another_service"] = {"env": {"DATABASE_URL": value}}
                self.assertTrue(self._preview()["ready"])

    def test_saved_effective_native_database_into_v1_blocks_preview(self):
        selection = "sqlite:" + str(self.data / "active.sqlite") + "?mode=rw"
        (self.v2data / "database.toml").write_text("url = " + json.dumps(selection))
        preview = self._preview()
        self.assertFalse(preview["ready"])
        self.assertTrue(
            any("references the v1" in item for item in preview["blockers"])
        )
        self.assertIsNone(self.controller._thread)

    def test_effective_native_database_is_bound_by_digest_and_rechecked_before_start(
        self,
    ):
        path = self.v2data / "database.toml"
        first = "postgresql://fixture:private-example@127.0.0.1/fixture_a"
        second = "postgresql://fixture:private-example@127.0.0.1/fixture_b"
        path.write_text("url = " + json.dumps(first))
        preview = self._preview()
        self.assertTrue(preview["ready"], preview)
        retained = self.manager._read(
            self.manager.state_root
            / "cleanup-previews"
            / (preview["preview_token"] + ".json")
        )
        self.assertEqual(
            retained["binding"]["native_database"],
            hashlib.sha256(first.encode()).hexdigest(),
        )
        self.assertNotIn("private-example", json.dumps(retained))
        path.write_text("url = " + json.dumps(second))
        with self.assertRaisesRegex(InfiniDyskV2MigrationError, "ownership changed"):
            self.controller.start(self._request(preview))
        self.assertIsNone(self.controller._thread)
        self.assertFalse(self.updater.updating.locked())
        self.assertTrue((self.data / "db.sqlite").exists())

    def test_native_database_selection_read_error_blocks_without_exposing_uri(self):
        (self.v2data / "database.toml").write_text(
            'url = "postgresql://fixture:private-example@127.0.0.1/broken'
        )
        preview = self._preview()
        self.assertFalse(preview["ready"])
        self.assertNotIn("private-example", json.dumps(preview))
        self.assertTrue(
            any("database selection" in item for item in preview["blockers"])
        )


if __name__ == "__main__":
    unittest.main()
