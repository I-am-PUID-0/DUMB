import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import psycopg2

from utils.infinidysk_v2_postgres import (
    InfiniDyskV2PostgresManager,
    NativePostgresError,
    inspect_target,
)


class Config:
    def __init__(self, data):
        self.config = data

    def get(self, key, section=None):
        return (self.config.get(section, {}) if section else self.config).get(key)


class NativePostgresTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.data = self.root / "native"
        self.data.mkdir()
        with sqlite3.connect(self.data / "infinidysk.sqlite") as db:
            db.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT)")
            db.execute("INSERT INTO items VALUES(1, 'reserved example')")
        self.config = Config(
            {
                "data_root": str(self.root),
                "infinidysk": {
                    "runtime_generation": "2",
                    "v2_data_dir": str(self.data),
                    "process_name": "InfiniDysk",
                    "mount_backend": "rclone",
                },
            }
        )
        self.clock = 1000
        self.identity = {
            "system_identifier": "11",
            "database_oid": "22",
            "schema": "public",
            "owner": "example",
            "collation": "C",
            "relations": [],
        }
        self.view = {"backend": "sqlite", "env_managed": False, "migration": None}
        self.calls = []
        self.started_ms = 10
        self.delivery_ready = True
        self.cleanup_warnings = []
        self.health = {"status": "ok"}
        self.native_unavailable = False
        self.lifecycle = []
        self.on_start = None
        self.handler = SimpleNamespace(
            media_protection_manager=None,
            process_names={"InfiniDysk": object()},
            _prefixed_name=lambda name: name,
            stop_process=self.stop_process,
            start_process=self.start_process,
        )
        self.manager = InfiniDyskV2PostgresManager(
            config_manager=self.config,
            process_handler=self.handler,
            state_root=self.root / "state",
            native_request=self.native,
            target_inspector=lambda *a, **k: copy.deepcopy(self.identity),
            delivery_checker=lambda *a: self.delivery_ready,
            clock=lambda: self.clock,
        )
        self.options = {
            "target": {
                "host": "127.0.0.1",
                "database": "example_native",
                "user": "example",
                "password": "fixture-only-secret",
            }
        }

    def native(self, service, method, path, payload=None, **kwargs):
        if self.native_unavailable:
            raise OSError("fixture unavailable")
        self.calls.append((method, path, copy.deepcopy(payload)))
        body = {}
        if path == "/api/v1/database" and method == "GET":
            body = copy.deepcopy(self.view)
        elif path.endswith("/check"):
            body = {"version": "PostgreSQL fixture", "error": None, "warnings": []}
        elif path.endswith("/migration") and method == "POST":
            self.view["migration"] = {"state": "copying", "done": 0, "total": 1}
        elif path.endswith("/migration") and method == "DELETE":
            self.view["migration"] = None
            body = {"warnings": self.cleanup_warnings}
        elif path.endswith("/cutover"):
            if method == "POST":
                self.view.update(backend="postgres", migration=None)
                (self.data / "infinidysk.sqlite").rename(
                    self.data / "infinidysk.sqlite.pre-postgres"
                )
                url = self.manager._job()["target_url_private"]
                (self.data / "database.toml").write_text(
                    "url = " + json.dumps(url) + "\n"
                )
                body = {"started_ms": self.started_ms}
                self.started_ms += 1
            else:
                body = {"state": "none", "started_ms": self.started_ms}
        elif path == "/healthz":
            body = self.health
        return {"status": 200, "body": body}

    def stop_process(self, name):
        self.lifecycle.append(("stop", name))
        self.handler.process_names.pop(name, None)

    def start_process(self, name):
        self.lifecycle.append(("start", name))
        private = self.manager._job()
        self.assertEqual("sqlite_restored", private["rollback_phase"])
        info = (self.data / "infinidysk.sqlite").stat()
        self.assertEqual(
            {"device": info.st_dev, "inode": info.st_ino},
            private["rollback_sqlite_identity_private"],
        )
        self.handler.process_names[name] = object()
        self.view.update(backend="sqlite", migration=None)
        self.native_unavailable = False
        if self.on_start:
            self.on_start()
        return True, None

    def finish_cutover(self):
        job = self.begin()
        self.view["migration"]["state"] = "syncing"
        self.manager.cutover(self.action(job, "cutover"))
        result = self.manager.status()["job"]
        self.assertEqual("completed", result["status"])
        return result

    def begin(self):
        preview = self.manager.preview(self.options)
        request = {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledge_backup": True,
            "acknowledge_destructive_target": True,
        }
        return self.manager.start(request)["job"]

    def action(self, job, kind):
        status = self.manager.status()
        return {
            "job_id": job["job_id"],
            "confirmation": status["confirmation_texts"][kind],
            "acknowledge_backup": True,
            "acknowledge_destructive_target": True,
        }

    def test_backup_precedes_native_mutation_and_secrets_stay_private(self):
        job = self.begin()
        private = self.manager._job()
        with sqlite3.connect(private["snapshot_private"]) as snapshot:
            self.assertEqual(
                [(1, "reserved example")],
                snapshot.execute("SELECT * FROM items").fetchall(),
            )
        self.assertEqual(0o600, (self.root / "state/job.json").stat().st_mode & 0o777)
        public = json.dumps(self.manager.status())
        self.assertNotIn("fixture-only-secret", public)
        self.assertNotIn("target_url_private", public)
        self.assertEqual("copying", job["status"])
        self.assertTrue(self.manager.has_blocking_job())

    def test_stale_preview_and_target_drift_never_start_copy(self):
        preview = self.manager.preview(self.options)
        self.identity["database_oid"] = "99"
        request = {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledge_backup": True,
            "acknowledge_destructive_target": True,
        }
        with self.assertRaises(NativePostgresError):
            self.manager.start(request)
        self.assertFalse(
            any(
                method == "POST" and path.endswith("/migration")
                for method, path, _ in self.calls
            )
        )

    def test_non_c_collation_never_admits_native_copy_or_cleanup(self):
        self.identity["collation"] = "en_US.UTF-8"
        with self.assertRaisesRegex(NativePostgresError, "C collation"):
            self.manager.preview(self.options)
        self.assertFalse(any(method == "POST" for method, _, _ in self.calls))
        self.identity["collation"] = "C"
        job = self.begin()
        request = self.action(job, "cancel")
        self.identity["collation"] = "different"
        with self.assertRaisesRegex(NativePostgresError, "C collation"):
            self.manager.cancel(request)
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.calls))

    def test_expired_preview_does_not_allocate_a_job(self):
        preview = self.manager.preview(self.options)
        self.clock += 301
        with self.assertRaises(NativePostgresError):
            self.manager.start(
                {
                    "preview_token": preview["preview_token"],
                    "confirmation": preview["confirmation_text"],
                    "acknowledge_backup": True,
                    "acknowledge_destructive_target": True,
                }
            )
        self.assertIsNone(self.manager._job())

    def test_cancel_rechecks_physical_target_before_destructive_native_call(self):
        job = self.begin()
        request = self.action(job, "cancel")
        self.identity["system_identifier"] = "different"
        with self.assertRaises(NativePostgresError):
            self.manager.cancel(request)
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.calls))

    def test_cutover_observes_native_exec_instead_of_restarting_process(self):
        job = self.begin()
        self.view["migration"]["state"] = "syncing"
        self.manager.cutover(self.action(job, "cutover"))
        result = self.manager.status()
        self.assertEqual("completed", result["job"]["status"])
        self.assertTrue(result["actions"]["rollback"])
        self.assertFalse(self.manager.has_blocking_job())
        self.assertFalse(hasattr(self.handler, "restart_process"))

    def test_out_of_band_and_environment_managed_moves_are_not_adopted_blindly(self):
        self.view["migration"] = {"state": "syncing"}
        self.assertFalse(self.manager.status()["actions"]["start"])
        self.view.update(migration=None, env_managed=True)
        with self.assertRaises(NativePostgresError):
            self.manager.preview(self.options)

    def test_controller_restart_resumes_the_persisted_native_job(self):
        job = self.begin()
        self.view["migration"]["state"] = "syncing"
        replacement = InfiniDyskV2PostgresManager(
            config_manager=self.config,
            state_root=self.root / "state",
            native_request=self.native,
            target_inspector=lambda *a, **k: copy.deepcopy(self.identity),
            delivery_checker=lambda *a: self.delivery_ready,
            clock=lambda: self.clock,
        )
        status = replacement.status()
        self.assertEqual(job["job_id"], status["job"]["job_id"])
        self.assertEqual("syncing", status["job"]["status"])

    def test_wrong_job_and_missing_acknowledgement_cannot_cancel(self):
        job = self.begin()
        request = self.action(job, "cancel")
        request["job_id"] = "another-job"
        with self.assertRaises(NativePostgresError):
            self.manager.cancel(request)
        request["job_id"] = job["job_id"]
        request["acknowledge_destructive_target"] = False
        with self.assertRaises(NativePostgresError):
            self.manager.cancel(request)
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.calls))

    def test_cutover_holds_protection_until_selected_delivery_is_ready(self):
        protection = SimpleNamespace(
            begin_planned=Mock(
                return_value={"status": "protected", "token": "fixture-protection"}
            ),
            complete_planned=Mock(),
        )
        self.handler.media_protection_manager = protection
        job = self.begin()
        self.view["migration"]["state"] = "syncing"
        self.delivery_ready = False
        self.manager.cutover(self.action(job, "cutover"))
        self.assertEqual("cutover", self.manager.status()["job"]["status"])
        self.assertEqual(
            "fixture-protection", self.manager._job()["protection_token_private"]
        )
        protection.complete_planned.assert_not_called()
        self.delivery_ready = True
        self.assertEqual("completed", self.manager.status()["job"]["status"])
        protection.complete_planned.assert_called_once_with(
            "fixture-protection", success=True
        )
        self.assertNotIn("protection_token_private", self.manager._job())

    def test_missing_native_health_proof_cannot_complete_cutover(self):
        job = self.begin()
        self.view["migration"]["state"] = "syncing"
        self.health = {}
        self.manager.cutover(self.action(job, "cutover"))
        self.assertEqual("cutover", self.manager.status()["job"]["status"])
        self.health = {"status": "ok"}
        self.assertEqual("completed", self.manager.status()["job"]["status"])

    def test_real_driver_error_never_exposes_private_connection_details(self):
        private_detail = (
            "postgres://example:fixture-only-secret@db.example.invalid/private"
        )
        with patch(
            "utils.infinidysk_v2_postgres.psycopg2.connect",
            side_effect=psycopg2.OperationalError(private_detail),
        ):
            with self.assertRaises(NativePostgresError) as caught:
                inspect_target({"host": "db.example.invalid"})
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("fixture-only-secret", rendered)
        self.assertNotIn(private_detail, rendered)
        self.assertIn("could not be verified", str(caught.exception))

    def test_malformed_boot_toml_error_is_sanitized_and_blocks_mutation(self):
        (self.data / "database.toml").write_text('url = "fixture-only-secret\n')
        with self.assertRaises(NativePostgresError) as caught:
            self.manager._boot()
        self.assertNotIn("fixture-only-secret", str(caught.exception))
        self.assertTrue(self.manager.has_blocking_job())

    def test_boot_journal_is_bounded_and_rejects_redirected_or_nonregular_files(self):
        boot = self.data / "database.toml"
        boot.write_bytes(b"#" + b"x" * (64 * 1024))
        with self.assertRaisesRegex(NativePostgresError, "bounded"):
            self.manager._boot()
        self.assertTrue(self.manager.has_blocking_job())
        boot.unlink()
        elsewhere = self.root / "elsewhere.toml"
        elsewhere.write_text('url = "postgres://reserved.invalid/example"\n')
        boot.symlink_to(elsewhere)
        with self.assertRaises(NativePostgresError):
            self.manager._boot()
        boot.unlink()
        boot.mkdir()
        with self.assertRaisesRegex(NativePostgresError, "bounded regular file"):
            self.manager._boot()

    def test_cold_resume_binds_journal_target_and_associated_mount_configs(self):
        associated = {
            "enabled": True,
            "key_type": "infinidysk",
            "mount_dir": str(self.root),
            "mount_name": "mount",
            "process_name": "rclone Infini",
        }
        unrelated = {
            "enabled": True,
            "key_type": "RealDebrid",
            "process_name": "rclone other",
        }
        self.config.config["rclone"] = {
            "instances": {"infini": associated, "other": unrelated}
        }
        self.config.config["postgres"] = {"host": "127.0.0.1", "port": 5432}
        self.begin()
        private = self.manager._job()
        (self.data / "database.toml").write_text(
            '[migration]\nstate = "copying"\ntarget = '
            + json.dumps(private["target_url_private"])
            + "\n"
        )
        self.assertTrue(
            self.manager.resume_runtime_allowed(
                "infinidysk", self.config.get("infinidysk")
            )
        )
        self.assertTrue(self.manager.resume_runtime_allowed("rclone", associated))
        self.assertTrue(
            self.manager.resume_runtime_allowed("postgres", self.config.get("postgres"))
        )
        self.assertFalse(self.manager.resume_runtime_allowed("rclone", unrelated))
        self.assertFalse(
            self.manager.resume_runtime_allowed(
                "rclone", {**associated, "mount_name": "changed"}
            )
        )
        self.assertFalse(
            self.manager.resume_runtime_allowed(
                "postgres", {"host": "127.0.0.1", "port": 5433}
            )
        )
        self.identity["database_oid"] = "replaced"
        self.assertFalse(
            self.manager.resume_runtime_allowed(
                "infinidysk", self.config.get("infinidysk")
            )
        )

    def test_outside_native_boot_journal_blocks_generic_mutation_without_api_poll(self):
        from utils import infinidysk_migration_admission as admission
        import utils.infinidysk_v2_postgres as module

        (self.data / "database.toml").write_text(
            '[migration]\nstate = "copying"\ntarget = "postgres://reserved.invalid/example"\n'
        )
        fake_major = SimpleNamespace(
            INFINIDYSK_V2_MIGRATION_MANAGER=SimpleNamespace(
                has_blocking_job=lambda: False
            )
        )
        with (
            patch.dict(sys.modules, {"utils.infinidysk_v2_migration": fake_major}),
            patch.object(module, "INFINIDYSK_V2_POSTGRES_MANAGER", self.manager),
            patch.object(
                admission, "infinidysk_namespace_migration_active", return_value=False
            ),
        ):
            self.assertTrue(self.manager.has_blocking_job())
            with self.assertRaises(admission.InfiniDyskMigrationAdmissionError):
                admission.reserve_infinidysk_external_mutation("fixture generic update")

    def test_cancel_cleanup_warning_preserves_usable_sqlite_and_unblocks_lifecycle(
        self,
    ):
        job = self.begin()
        self.cleanup_warnings = ["Target reset needs review"]
        result = self.manager.cancel(self.action(job, "cancel"))["job"]
        self.assertEqual("cancelled", result["status"])
        self.assertTrue(result["cleanup_required"])
        self.assertFalse(result["recovery_required"])
        self.assertFalse(self.manager.has_blocking_job())
        with sqlite3.connect(self.data / "infinidysk.sqlite") as source:
            self.assertEqual(
                [(1, "reserved example")],
                source.execute("SELECT * FROM items").fetchall(),
            )

    def test_cancel_retains_bounded_source_cleanup_warning_evidence_privately(self):
        job = self.begin()
        self.cleanup_warnings = [
            "CAPTURE_LEFT source capture cleanup needs restart postgres://example:fixture-only-secret@db.example.invalid/private"
        ] * 20
        result = self.manager.cancel(self.action(job, "cancel"))["job"]
        private = self.manager._job()["cleanup_warnings_private"]
        self.assertEqual(8, len(private))
        self.assertIn("CAPTURE_LEFT", private[0])
        self.assertNotIn("fixture-only-secret", json.dumps(private))
        self.assertNotIn("postgres://", json.dumps(private))
        self.assertNotIn("CAPTURE_LEFT", json.dumps(result))
        self.assertTrue(result["cleanup_required"])
        self.assertIn("SQLite remains active", result["message"])
        self.assertFalse(self.manager.has_blocking_job())

    def test_next_job_does_not_overwrite_private_cleanup_binding_evidence(self):
        job = self.begin()
        self.cleanup_warnings = ["CAPTURE_LEFT source capture cleanup needs restart"]
        self.manager.cancel(self.action(job, "cancel"))
        evidence = Path(self.manager._job()["cleanup_evidence_private"])
        original = evidence.read_bytes()
        record = json.loads(original)
        self.assertEqual(job["job_id"], record["job_id"])
        self.assertEqual("11", record["target_identity"]["system_identifier"])
        self.assertEqual("22", record["target_identity"]["database_oid"])
        self.assertEqual("example_native", record["target_summary"]["dbname"])
        self.assertEqual(0o600, evidence.stat().st_mode & 0o777)
        self.assertNotIn("fixture-only-secret", original.decode())
        self.options["target"]["database"] = "another_empty_target"
        new_job = self.begin()
        self.assertNotEqual(job["job_id"], new_job["job_id"])
        self.assertEqual(original, evidence.read_bytes())

    def test_terminal_previous_job_does_not_adopt_an_outside_native_transfer(self):
        job = self.begin()
        self.manager.cancel(self.action(job, "cancel"))
        self.view["migration"] = {"state": "syncing"}
        status = self.manager.status()
        self.assertFalse(status["ready"])
        self.assertFalse(status["actions"]["start"])
        self.assertTrue(any("outside DUMB" in item for item in status["blockers"]))

    def test_rollback_works_without_postgresql_connectivity(self):
        job = self.finish_cutover()
        request = self.action(job, "rollback")
        self.manager.target_inspector = Mock(
            side_effect=psycopg2.OperationalError("fixture unreachable")
        )
        self.native_unavailable = True
        result = self.manager.rollback(request)["job"]
        self.assertEqual("rolled_back", result["status"])
        self.manager.target_inspector.assert_not_called()
        self.assertFalse((self.data / "database.toml").exists())

    def test_rollback_retry_keeps_post_restore_sqlite_writes_and_does_not_restart(self):
        protection = SimpleNamespace(
            begin_planned=Mock(
                return_value={"status": "protected", "token": "fixture-protection"}
            ),
            complete_planned=Mock(),
        )
        job = self.finish_cutover()
        self.handler.media_protection_manager = protection
        request = self.action(job, "rollback")
        self.delivery_ready = False

        def write_after_start():
            with sqlite3.connect(self.data / "infinidysk.sqlite") as source:
                source.execute(
                    "INSERT INTO items VALUES(2, 'write after SQLite restore')"
                )

        self.on_start = write_after_start
        with self.assertRaisesRegex(NativePostgresError, "waiting for selected mount"):
            self.manager.rollback(request)
        first_lifecycle = list(self.lifecycle)
        info = (self.data / "infinidysk.sqlite").stat()
        self.assertEqual("sqlite_restored", self.manager._job()["rollback_phase"])
        protection.complete_planned.assert_not_called()
        self.delivery_ready = True
        self.manager.target_inspector = Mock(
            side_effect=AssertionError("PostgreSQL must not be consulted")
        )
        self.assertEqual("rolled_back", self.manager.rollback(request)["job"]["status"])
        self.assertEqual(first_lifecycle, self.lifecycle)
        after = (self.data / "infinidysk.sqlite").stat()
        self.assertEqual((info.st_dev, info.st_ino), (after.st_dev, after.st_ino))
        with sqlite3.connect(self.data / "infinidysk.sqlite") as source:
            self.assertEqual(
                2, source.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            )
        protection.complete_planned.assert_called_once_with(
            "fixture-protection", success=True
        )

    def test_restored_sqlite_replacement_is_never_overwritten_on_retry(self):
        job = self.finish_cutover()
        request = self.action(job, "rollback")
        self.delivery_ready = False
        with self.assertRaises(NativePostgresError):
            self.manager.rollback(request)
        replacement = self.data / "different.sqlite"
        with sqlite3.connect(replacement) as database:
            database.execute("CREATE TABLE preserved(value TEXT)")
            database.execute("INSERT INTO preserved VALUES('replacement must survive')")
        os.replace(replacement, self.data / "infinidysk.sqlite")
        before = (self.data / "infinidysk.sqlite").read_bytes()
        self.delivery_ready = True
        with self.assertRaisesRegex(NativePostgresError, "identity changed"):
            self.manager.rollback(request)
        self.assertEqual(before, (self.data / "infinidysk.sqlite").read_bytes())

    def test_request_objects_and_acknowledgements_fail_closed_without_echoing_input(
        self,
    ):
        for value in (
            None,
            [],
            "fixture-only-secret",
            {"target": None},
            {"target": []},
            {"target": "fixture-only-secret"},
            {"target": {"use_managed_postgres": "false"}},
        ):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(NativePostgresError) as caught:
                    self.manager.preview(value)
                self.assertNotIn("fixture-only-secret", str(caught.exception))
        preview = self.manager.preview(self.options)
        request = {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledge_backup": True,
            "acknowledge_destructive_target": True,
        }
        for field in ("acknowledge_backup", "acknowledge_destructive_target"):
            for value in ("false", "true", 1, []):
                with self.assertRaises(NativePostgresError):
                    self.manager.start({**request, field: value})
        self.assertIsNone(self.manager._job())


if __name__ == "__main__":
    unittest.main()
