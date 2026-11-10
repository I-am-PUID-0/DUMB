from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from utils.infinidysk_v2_migration import (
    ACKNOWLEDGEMENTS,
    InfiniDyskV2MigrationError,
    InfiniDyskV2MigrationManager,
)


class Config:
    """Use the real manager's get(key, section=None) convention."""

    def __init__(self, root, config):
        self.config = config
        self.file_path = str(root / "dumb-config.json")
        self.save_config()

    def get(self, key, section=None):
        value = self.config.get(section, {}) if section is not None else self.config
        return value.get(key) if isinstance(value, dict) else None

    def save_config(self, process_name=None):
        Path(self.file_path).write_text(json.dumps(self.config))
        Path(self.file_path).chmod(0o600)


class Executor:
    supported_source_providers = ("sqlite",)

    def __init__(self):
        self.calls = []
        self.skipped = 0
        self.fail_recovery = False
        self.gate = None
        self.started = threading.Event()
        self.oidc_verified = False

    def preflight(self, plan):
        return {"blockers": []}

    def receipt(self, plan):
        return {
            "execution_verified": True,
            "isolation_verified": True,
            "source_untouched": True,
            "import_complete": True,
            "inventory_verified": True,
            "plan_fingerprint": plan["plan_fingerprint"],
            "report": {"skipped_files": self.skipped, "failed_files": 0},
            "oidc_policy_verified": self.oidc_verified,
        }

    def rehearse(self, plan, workspace, progress):
        self.calls.append("rehearse")
        self.started.set()
        if self.gate is not None and not self.gate.wait(timeout=5):
            raise TimeoutError("test release not signalled")
        workspace["capture_source"]()
        receipt = self.receipt(plan)
        workspace["accept_import"](receipt)
        return {**receipt, "source_restored": True}

    def cutover(self, plan, workspace, progress):
        self.calls.append("cutover")
        workspace["capture_source"]()
        candidate = Path(workspace["candidate_data"])
        (candidate / "infinidysk.sqlite").write_text("v2 data after import")
        receipt = self.receipt(plan)
        workspace["accept_import"](receipt)
        target = Path(plan["target"]["data_dir"])
        if target.exists():
            target.rmdir()
        candidate.rename(target)
        workspace["activate_config"](plan["target"]["service"])
        return {**receipt, "generation_2_started": True}

    def rollback(self, job, progress):
        self.calls.append("rollback")
        if self.fail_recovery:
            raise OSError("recovery unavailable")
        job["restore_config"]()
        return {"generation_2_stopped": True, "source_restored": True}


class InfiniDyskV2MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "v1"
        self.source.mkdir()
        (self.source / "version.txt").write_text("v1.7.1")
        (self.source / "db-contract.json").write_text(
            json.dumps(
                {
                    "provider": "sqlite",
                    "terminalMigration": "20260824160000_Add-Health-Repair-Pending",
                }
            )
        )
        with sqlite3.connect(self.source / "db.sqlite") as db:
            db.executescript(
                "CREATE TABLE __EFMigrationsHistory(MigrationId TEXT);"
                "INSERT INTO __EFMigrationsHistory VALUES('20260824160000_Add-Health-Repair-Pending');"
                "CREATE TABLE QueueItems(Id INTEGER);"
                "CREATE TABLE ConfigItems(ConfigName TEXT, ConfigValue TEXT);"
                "CREATE TABLE OriginalData(Value TEXT);"
                "INSERT INTO OriginalData VALUES('original');"
            )
        (self.source / "blobs").mkdir()
        (self.source / "blobs" / "original").write_text("original metadata")
        self.mount = self.root / "mounts" / "infinidysk"
        self.mount.mkdir(parents=True)
        self.config = Config(
            self.root,
            {
                "puid": 1000,
                "pgid": 1000,
                "infinidysk": {
                    "enabled": False,
                    "process_name": "InfiniDysk",
                    "runtime_generation": "1",
                    "config_dir": str(self.source),
                    "env": {
                        "TZ": "UTC",
                        "FRONTEND_BACKEND_API_KEY": "private-example-key",
                    },
                    "v2_runtime_dir": str(self.root / "runtime-v2"),
                    "v2_data_dir": str(self.root / "data-v2"),
                    "mount_backend": "rclone",
                    "mount_path": str(self.mount),
                },
                "rclone": {
                    "instances": {
                        "usenet": {
                            "enabled": True,
                            "key_type": "infinidysk",
                            "mount_dir": str(self.mount.parent),
                            "mount_name": self.mount.name,
                            "process_name": "rclone InfiniDysk",
                        }
                    }
                },
                "unrelated": {"keep": "original"},
            },
        )
        self.handler = SimpleNamespace(processes={}, shutting_down=False)
        self.updater = SimpleNamespace(updating=threading.Lock())
        self.executor = Executor()
        self.manager = self.new_manager()
        self.managers = [self.manager]
        self.admission = []
        for name in (
            "infinidysk_namespace_migration_active",
            "infinidysk_postgres_migration_active",
            "infinidysk_external_mutation_active",
        ):
            item = patch(
                "utils.infinidysk_migration_admission." + name, return_value=False
            )
            item.start()
            self.admission.append(item)

    def tearDown(self):
        if self.executor.gate:
            self.executor.gate.set()
        try:
            for manager in self.managers:
                if manager._thread is not None and manager._thread.ident is not None:
                    manager._thread.join(timeout=3)
                    self.assertFalse(manager._thread.is_alive())
        finally:
            for item in reversed(self.admission):
                item.stop()
            self.temp.cleanup()

    def new_manager(self):
        return InfiniDyskV2MigrationManager(
            process_handler=self.handler,
            updater=self.updater,
            config_manager=self.config,
            state_root=self.root / "state",
            executor=self.executor,
        )

    def preview_request(self):
        preview = self.manager.preview(
            {
                "mode": "migrate",
                "mount_backend": "rclone",
                "import_strategy": "preserve",
            }
        )
        self.assertTrue(preview["ready"], preview["blockers"])
        return preview, {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": dict.fromkeys(ACKNOWLEDGEMENTS, True),
        }

    def wait(self, response):
        self.manager._thread.join(timeout=3)
        self.assertFalse(self.manager._thread.is_alive())
        return self.manager.get_job(response["job"]["job_id"])["job"]

    def rehearsal(self):
        preview, request = self.preview_request()
        job = self.wait(self.manager.rehearse(request))
        self.assertEqual("rehearsed", job["status"], job)
        return preview, request, job

    def test_preview_is_private_metadata_and_never_runs_converter(self):
        preview, request = self.preview_request()
        self.assertTrue(preview["rehearsal_required"])
        self.assertEqual(str(self.mount), preview["target"]["mount_path"])
        self.assertFalse(self.executor.calls)
        public = json.dumps(preview) + json.dumps(self.manager.status())
        self.assertNotIn("private-example-key", public)
        self.assertNotIn("config_snapshot", public)
        saved = next((self.root / "state" / "previews").glob("*.json"))
        self.assertEqual(0o600, saved.stat().st_mode & 0o777)

    def test_start_requires_actual_matching_native_rehearsal(self):
        _, request = self.preview_request()
        with self.assertRaisesRegex(
            InfiniDyskV2MigrationError, "Invalid migration job identity"
        ):
            self.manager.start(request)
        self.assertFalse(self.executor.calls)

    def test_rehearsal_then_cutover_and_rollback_preserve_v1_and_unrelated_config(self):
        old = (self.source / "db.sqlite").read_bytes()
        _, request, rehearsal = self.rehearsal()
        self.assertEqual(old, (self.source / "db.sqlite").read_bytes())
        request["rehearsal_job_id"] = rehearsal["job_id"]
        job = self.wait(self.manager.start(request))
        self.assertEqual("completed", job["status"], job)
        self.assertTrue(job["can_rollback"])
        self.assertEqual("2", self.config.config["infinidysk"]["runtime_generation"])
        self.assertEqual(old, (self.source / "db.sqlite").read_bytes())
        self.config.config["unrelated"]["keep"] = "changed after cutover"
        result = self.manager.rollback(
            {"job_id": job["job_id"], "confirmation": job["rollback_confirmation_text"]}
        )["job"]
        self.assertEqual("rolled_back", result["status"])
        self.assertFalse(result["result"]["reverse_copy_performed"])
        self.assertEqual("1", self.config.config["infinidysk"]["runtime_generation"])
        self.assertEqual(
            "changed after cutover", self.config.config["unrelated"]["keep"]
        )
        self.assertEqual(old, (self.source / "db.sqlite").read_bytes())
        self.assertTrue((self.root / "data-v2" / "infinidysk.sqlite").exists())

    def test_expired_or_stale_preview_cannot_start_worker(self):
        _, request = self.preview_request()
        self.config.config["infinidysk"]["env"]["TZ"] = "Etc/UTC"
        with self.assertRaisesRegex(InfiniDyskV2MigrationError, "changed"):
            self.manager.rehearse(request)
        self.assertFalse(self.executor.calls)
        self.config.config["infinidysk"]["env"]["TZ"] = "UTC"
        with patch("utils.infinidysk_v2_migration.time.time", return_value=10**12):
            with self.assertRaisesRegex(InfiniDyskV2MigrationError, "expired"):
                self.manager.rehearse(request)

    def test_skipped_files_block_acceptance_and_restore_original(self):
        self.executor.skipped = 1
        _, request = self.preview_request()
        job = self.wait(self.manager.rehearse(request))
        self.assertEqual("failed_restored", job["status"])
        self.assertFalse(job["result"]["validated"])
        self.assertIn("skipped", job["error"])
        self.assertEqual("1", self.config.config["infinidysk"]["runtime_generation"])

    def test_queue_revalidated_from_quiesced_snapshot_before_native_import(self):
        _, request = self.preview_request()
        with sqlite3.connect(self.source / "db.sqlite") as db:
            db.execute("INSERT INTO QueueItems VALUES(1)")
        job = self.wait(self.manager.rehearse(request))
        self.assertEqual("failed_restored", job["status"])
        self.assertIn("queue", job["error"])
        with sqlite3.connect(self.source / "db.sqlite") as db:
            self.assertEqual(
                1, db.execute("SELECT COUNT(*) FROM QueueItems").fetchone()[0]
            )

    def test_schema_floor_is_checked_on_actual_snapshot(self):
        preview, request = self.preview_request()
        with sqlite3.connect(self.source / "db.sqlite") as db:
            db.execute(
                "UPDATE __EFMigrationsHistory SET MigrationId='20260820160000_Old'"
            )
        job = self.wait(self.manager.rehearse(request))
        self.assertEqual("failed_restored", job["status"])
        self.assertIn("schema", job["error"])

    def test_original_database_symlink_or_hardlink_is_refused(self):
        original = self.source / "db.sqlite"
        linked = self.source / "linked.sqlite"
        linked.hardlink_to(original)
        _, request = self.preview_request()
        job = self.wait(self.manager.rehearse(request))
        self.assertEqual("failed_restored", job["status"])
        self.assertIn("linked", job["error"])

    def test_acknowledgements_do_not_replace_oidc_policy_verification(self):
        with sqlite3.connect(self.source / "db.sqlite") as db:
            db.execute(
                "INSERT INTO ConfigItems VALUES('oidc.issuer','https://issuer.example')"
            )
        _, request = self.preview_request()
        job = self.wait(self.manager.rehearse(request))
        self.assertEqual("failed_restored", job["status"])
        self.assertIn("OIDC", job["error"])

    def test_snapshot_refuses_managed_source_that_is_still_running(self):
        _, request = self.preview_request()
        process = Mock(pid=123456)
        process.poll.return_value = None
        self.handler.processes["infini"] = {
            "name": "InfiniDysk",
            "process_obj": process,
        }
        # A changed process identity first invalidates the saved preflight.
        with self.assertRaisesRegex(InfiniDyskV2MigrationError, "identity changed"):
            self.manager.rehearse(request)
        _, request = self.preview_request()
        job = self.wait(self.manager.rehearse(request))
        self.assertEqual("failed_restored", job["status"])
        self.assertIn("still running", job["error"])

    def test_only_one_job_runs_and_busy_updater_never_reaches_executor(self):
        self.executor.gate = threading.Event()
        _, request = self.preview_request()
        response = self.manager.rehearse(request)
        self.assertTrue(self.executor.started.wait(timeout=1))
        with self.assertRaisesRegex(InfiniDyskV2MigrationError, "active"):
            self.manager.rehearse(request)
        self.executor.gate.set()
        self.wait(response)
        _, request = self.preview_request()
        self.updater.updating.acquire()
        try:
            calls = list(self.executor.calls)
            job = self.wait(self.manager.rehearse(request))
            self.assertEqual("failed", job["status"])
            self.assertEqual(calls, self.executor.calls)
        finally:
            self.updater.updating.release()

    def test_failed_recovery_remains_blocking_across_manager_restart(self):
        self.executor.skipped = 1
        self.executor.fail_recovery = True
        _, request = self.preview_request()
        job = self.wait(self.manager.rehearse(request))
        self.assertEqual("recovery_required", job["status"])
        self.assertTrue(job["can_rollback"])
        replacement = self.new_manager()
        self.managers.append(replacement)
        self.assertTrue(replacement.has_blocking_job())
        self.assertTrue(replacement.has_recovery_pending_job())

    def test_fresh_install_cannot_discard_existing_v1_data(self):
        result = self.manager.preview({"mode": "fresh"})
        self.assertFalse(result["ready"])
        self.assertTrue(any("Existing v1 data" in item for item in result["blockers"]))

    def test_no_executor_and_unverified_postgres_fail_closed(self):
        self.manager.executor = None
        self.assertFalse(self.manager.preview({"mode": "migrate"})["ready"])
        self.manager.executor = self.executor
        self.config.config["infinidysk"]["env"]["DATABASE_PROVIDER"] = "postgres"
        result = self.manager.preview({"mode": "migrate"})
        self.assertFalse(result["ready"])
        self.assertTrue(any("PostgreSQL" in item for item in result["blockers"]))

    def test_source_and_target_overlap_is_blocked_without_effects(self):
        self.config.config["infinidysk"]["v2_data_dir"] = str(self.source / "v2")
        result = self.manager.preview({"mode": "migrate"})
        self.assertFalse(result["ready"])
        self.assertFalse((self.source / "v2").exists())

    def test_client_cannot_supply_acceptance_receipt_or_skip_executor(self):
        _, request = self.preview_request()
        request["receipt"] = self.executor.receipt(
            self.manager._plan({"mode": "migrate"})
        )
        with patch.object(self.executor, "rehearse", return_value=request["receipt"]):
            job = self.wait(self.manager.rehearse(request))
        self.assertEqual("failed_restored", job["status"])
        self.assertFalse(job["result"]["validated"])

    def test_custom_legacy_mount_is_authoritative_over_new_default(self):
        self.config.config["infinidysk"]["mount_path"] = "/mnt/debrid/infinidysk"
        preview, _ = self.preview_request()
        self.assertEqual(str(self.mount), preview["current"]["mount_path"])
        self.assertEqual(str(self.mount), preview["target"]["mount_path"])
        self.assertEqual(
            str(self.mount), self.manager.status()["current"]["mount_path"]
        )

    def test_native_mount_preserves_explicit_legacy_epoch_dates(self):
        instance = self.config.config["rclone"]["instances"]["usenet"]
        for flag, expected in (
            ("--no-modtime", "true"),
            ("--no-modtime=true", "true"),
            ("--no-modtime=false", None),
        ):
            with self.subTest(flag=flag):
                instance["command"] = ["rclone", "mount", flag]
                plan = self.manager._plan({"mode": "migrate", "mount_backend": "fuse"})
                self.assertEqual(
                    expected,
                    plan["target"]["service"]["env"].get(
                        "INFINIDYSK__MOUNT__EPOCH_TIMES"
                    ),
                )
                self.assertEqual(["rclone", "mount", flag], instance["command"])

    def test_expected_managed_source_alias_is_bound_without_mutating_legacy_config(
        self,
    ):
        data_root = self.root / "data"
        data_root.mkdir()
        retained = data_root / "infinidysk"
        self.source.rename(retained)
        alias = self.root / "infinidysk"
        alias.symlink_to(retained, target_is_directory=True)
        self.source = retained
        self.config.config["data_root"] = str(data_root)
        self.config.config["infinidysk"]["config_dir"] = str(alias)
        with patch("utils.infinidysk_v2_migration._MANAGED_SOURCE_ALIASES", (alias,)):
            preview, request = self.preview_request()
            private_plan = self.manager._plan({"mode": "migrate"})
            self.assertEqual(str(retained), private_plan["source"]["data_dir"])
            self.assertEqual(
                str(alias), private_plan["source"]["service"]["config_dir"]
            )
            self.assertEqual(str(alias), private_plan["source"]["aliases"][0]["path"])
            job = self.wait(self.manager.rehearse(request))
        self.assertEqual("rehearsed", job["status"], job)
        self.assertTrue(alias.is_symlink())

    def test_foreign_source_alias_and_changed_managed_alias_are_refused(self):
        alias = self.root / "foreign"
        alias.symlink_to(self.source, target_is_directory=True)
        self.config.config["infinidysk"]["config_dir"] = str(alias)
        with self.assertRaisesRegex(InfiniDyskV2MigrationError, "unmanaged"):
            self.manager.preview({"mode": "migrate"})

    def test_late_bad_receipt_does_not_override_an_earlier_accepted_checkpoint(self):
        original = self.executor.rehearse

        def run(plan, workspace, progress):
            receipt = original(plan, workspace, progress)
            receipt["report"]["skipped_files"] = 1
            return receipt

        self.executor.rehearse = run
        _, request = self.preview_request()
        job = self.wait(self.manager.rehearse(request))
        self.assertEqual("failed_restored", job["status"])
        self.assertFalse(job["result"]["validated"])

    def test_errors_redact_private_connection_urls(self):
        with patch.object(
            self.executor,
            "rehearse",
            side_effect=RuntimeError(
                "postgres://example:password-with-s@127.0.0.1/example"
            ),
        ):
            _, request = self.preview_request()
            job = self.wait(self.manager.rehearse(request))
        self.assertNotIn("password-with-s", job["error"])
        self.assertIn("[REDACTED]", job["error"])

    def test_candidate_creation_never_changes_shared_parent_permissions(self):
        parent = self.root / "delivery"
        parent.mkdir(mode=0o755)
        parent.chmod(0o755)
        self.config.config["infinidysk"]["v2_data_dir"] = str(parent / "data-v2")
        self.rehearsal()
        self.assertEqual(0o755, parent.stat().st_mode & 0o777)

    def test_fresh_target_is_explicitly_enabled_without_bypassing_existing_data(self):
        shutil.rmtree(self.source)
        self.source.mkdir()
        preview = self.manager.preview({"mode": "fresh"})
        self.assertTrue(preview["ready"], preview["blockers"])
        self.assertTrue(preview["target"]["enabled"])

    def test_private_state_rejects_symlink_and_job_path_traversal(self):
        with self.assertRaises(InfiniDyskV2MigrationError):
            self.manager.get_job("../../outside")
        self.preview_request()
        path = next((self.root / "state" / "previews").glob("*.json"))
        original = path.with_suffix(".original")
        path.rename(original)
        path.symlink_to(original)
        with self.assertRaises(InfiniDyskV2MigrationError):
            self.manager._read(path)

    def test_rollback_refuses_overwriting_changed_task_owned_configuration(self):
        _, request, rehearsal = self.rehearsal()
        request["rehearsal_job_id"] = rehearsal["job_id"]
        job = self.wait(self.manager.start(request))
        self.assertEqual("completed", job["status"])
        self.config.config["infinidysk"]["mount_path"] = str(self.root / "other")
        with self.assertRaisesRegex(
            InfiniDyskV2MigrationError, "configuration changed"
        ):
            self.manager.rollback(
                {
                    "job_id": job["job_id"],
                    "confirmation": job["rollback_confirmation_text"],
                }
            )
        self.assertEqual("2", self.config.config["infinidysk"]["runtime_generation"])
        self.assertTrue(self.manager.has_recovery_pending_job())

    def test_live_data_links_must_remain_internal_after_candidate_relocation(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "file").write_text("unrelated")
        link = self.source / "unsafe"
        targets = (
            str(outside),
            str(self.source / "blobs" / "original"),
            "../outside/file",
            f"../{self.source.name}/blobs/original",
        )
        for target in targets:
            with self.subTest(target=target):
                link.symlink_to(target)
                with patch("utils.infinidysk_v2_migration.shutil.copytree") as copying:
                    preview = self.manager.preview({"mode": "migrate"})
                self.assertFalse(preview["ready"])
                self.assertTrue(
                    any("symbolic link" in item for item in preview["blockers"])
                )
                copying.assert_not_called()
                link.unlink()
        (self.source / "relative").symlink_to("blobs/original")
        preview = self.manager.preview({"mode": "migrate"})
        self.assertTrue(preview["ready"], preview["blockers"])
        _, _, job = self.rehearsal()
        private = self.manager._read(self.manager._job_path(job["job_id"]))
        candidate = Path(self.manager._workspace(private)["candidate_data"])
        self.assertTrue((candidate / "relative").is_symlink())
        self.assertEqual(
            (candidate / "relative").resolve(), candidate / "blobs" / "original"
        )

    def test_link_added_after_dispatch_is_refused_before_cold_copy(self):
        self.executor.gate = threading.Event()
        _, request = self.preview_request()
        result = self.manager.rehearse(request)
        try:
            self.assertTrue(self.executor.started.wait(timeout=2))
            (self.source / "unsafe").symlink_to("../outside")
            with patch("utils.infinidysk_v2_migration.shutil.copytree") as copying:
                self.executor.gate.set()
                job = self.wait(result)
            self.assertFalse(job["result"]["validated"])
            copying.assert_not_called()
        finally:
            self.executor.gate.set()

    def test_insufficient_space_and_entry_bound_refuse_before_source_copy(self):
        from utils.infinidysk_v2_migration import _capacity_check

        with patch(
            "utils.infinidysk_v2_migration.shutil.disk_usage",
            return_value=SimpleNamespace(free=1),
        ):
            preview = self.manager.preview({"mode": "migrate"})
            self.assertFalse(preview["ready"])
            self.assertTrue(any("free space" in value for value in preview["blockers"]))
        with (
            patch("utils.infinidysk_v2_inventory.MAX_TREE_ENTRIES", 1),
            patch("utils.infinidysk_v2_migration.shutil.copytree") as copying,
        ):
            preview = self.manager.preview({"mode": "migrate"})
            self.assertFalse(preview["ready"])
            self.assertTrue(
                any("inventory exceeds" in value for value in preview["blockers"])
            )
            copying.assert_not_called()
        with patch(
            "utils.infinidysk_v2_migration.shutil.disk_usage",
            return_value=SimpleNamespace(free=200 * 1024 * 1024),
        ):
            with self.assertRaisesRegex(InfiniDyskV2MigrationError, "free space"):
                _capacity_check(
                    [(self.root, 80 * 1024 * 1024), (self.root, 80 * 1024 * 1024)]
                )

    def test_full_blob_inventory_is_summarized_once_without_binding_live_contents(self):
        from utils.infinidysk_v2_inventory import tree_summary

        for index in range(40):
            (self.source / "blobs" / f"blob-{index}").write_text("retained metadata")
        first = self.manager._plan({"mode": "migrate"})
        summary = first["capacity"]["source"]
        self.assertEqual(summary["root_count"], 1)
        self.assertEqual(summary["entries"], tree_summary(self.source)["entries"])
        self.assertEqual(summary["logical_bytes"], tree_summary(self.source)["bytes"])
        (self.source / "blobs" / "new-active-entry").write_text(
            "permitted before quiescence"
        )
        second = self.manager._plan({"mode": "migrate"})
        self.assertEqual(first["plan_fingerprint"], second["plan_fingerprint"])
        self.assertEqual(
            second["capacity"]["source"]["entries"],
            summary["entries"] + 1,
        )

    def test_large_complete_inventory_keeps_full_copy_capacity_guard_and_small_state(
        self,
    ):
        from utils.infinidysk_v2_inventory import MAX_TREE_MANIFEST_BYTES

        source_bytes = 64 * 1024**3
        summary = {"entries": 350_000, "bytes": source_bytes}
        with (
            patch(
                "utils.infinidysk_v2_migration._source_summary", return_value=summary
            ) as scanning,
            patch(
                "utils.infinidysk_v2_migration.shutil.disk_usage",
                return_value=SimpleNamespace(free=100 * 1024**3),
            ),
            patch("utils.infinidysk_v2_migration.shutil.copytree") as copying,
            patch("utils.infinidysk_v2_migration._source_fingerprint") as hashing,
        ):
            preview = self.manager.preview({"mode": "migrate"})
        self.assertFalse(preview["ready"])
        self.assertTrue(any("free space" in value for value in preview["blockers"]))
        scanning.assert_called_once()
        copying.assert_not_called()
        hashing.assert_not_called()
        with (
            patch(
                "utils.infinidysk_v2_migration._source_summary", return_value=summary
            ),
            patch(
                "utils.infinidysk_v2_migration.shutil.disk_usage",
                return_value=SimpleNamespace(free=400 * 1024**3),
            ),
        ):
            plan = self.manager._plan({"mode": "migrate"})
        database_bytes = (self.source / "db.sqlite").stat().st_size
        capacity = plan["capacity"]
        self.assertEqual(
            sum(row["base_required_bytes"] for row in capacity["filesystems"]),
            2 * source_bytes
            + MAX_TREE_MANIFEST_BYTES
            + 2 * database_bytes
            + max(64 * 1024**2, database_bytes * 3),
        )
        self.assertEqual(capacity["source"]["root_count"], 1)
        self.assertEqual(capacity["source"]["entries"], 350_000)
        self.assertIsInstance(capacity["filesystems"][0]["required_bytes"], int)
        self.assertLess(len(json.dumps(capacity)), 1024)
        self.assertFalse(capacity["reserved"])

    def test_preview_exposes_complete_capacity_when_space_is_sufficient_or_short(self):
        preview, _ = self.preview_request()
        capacity = preview["capacity"]
        self.assertEqual(capacity["status"], "sufficient")
        self.assertTrue(capacity["complete"])
        self.assertTrue(capacity["estimated"])
        self.assertFalse(capacity["reserved"])
        self.assertIsInstance(capacity["observed_at"], int)
        self.assertGreater(capacity["source"]["entries"], 0)
        self.assertEqual(capacity["source"]["root_count"], 1)
        self.assertEqual(preview["protocol"], 1)
        with patch(
            "utils.infinidysk_v2_migration.shutil.disk_usage",
            return_value=SimpleNamespace(free=1),
        ):
            short = self.manager.preview({"mode": "migrate"})
        self.assertFalse(short["ready"])
        self.assertEqual(short["capacity"]["status"], "insufficient")
        self.assertTrue(short["capacity"]["complete"])
        row = short["capacity"]["filesystems"][0]
        self.assertEqual(row["shortfall_bytes"], row["required_bytes"] - 1)
        self.assertFalse(row["sufficient"])
        self.assertTrue(
            any(
                "required" in message and "free" in message and "short" in message
                for message in short["blockers"]
            )
        )
        self.assertNotIn(str(self.root), json.dumps(short["capacity"]))

    def test_incomplete_inventory_has_unavailable_capacity_without_partial_totals(self):
        with patch(
            "utils.infinidysk_v2_migration._source_summary",
            side_effect=InfiniDyskV2MigrationError(
                "Source inventory could not be read completely."
            ),
        ):
            preview = self.manager.preview({"mode": "migrate"})
        capacity = preview["capacity"]
        self.assertFalse(preview["ready"])
        self.assertEqual(capacity["status"], "unavailable")
        self.assertFalse(capacity["complete"])
        self.assertEqual(capacity["filesystems"], [])
        self.assertEqual(
            capacity["source"],
            {"entries": None, "logical_bytes": None, "root_count": None},
        )

    def test_postgres_inspection_failure_has_unavailable_capacity(self):
        self.config.config["infinidysk"]["postgres_enabled"] = True
        self.executor.supported_source_providers = ("sqlite", "postgres")
        binding = {
            "physical_identity": "fixture-physical-identity",
            "schema_fingerprint": "a" * 64,
            "migration_history_fingerprint": "b" * 64,
            "terminal_migration": "20260824160000_Add-Health-Repair-Pending",
            "session_timezone": "UTC",
        }
        for invalid_binding in (binding, {"database_size_bytes": 1024**3}):
            with self.subTest(binding_keys=sorted(invalid_binding)):
                with patch.object(
                    self.executor,
                    "preflight",
                    return_value={"blockers": [], "source_binding": invalid_binding},
                ):
                    preview = self.manager.preview({"mode": "migrate"})
                self.assertFalse(preview["ready"])
                self.assertEqual(preview["capacity"]["status"], "unavailable")
                self.assertFalse(preview["capacity"]["complete"])
                self.assertEqual(preview["capacity"]["filesystems"], [])
                self.assertTrue(
                    any("source size" in item for item in preview["blockers"])
                )

    def test_filesystem_probe_failure_returns_unavailable_without_partial_totals(self):
        with patch(
            "utils.infinidysk_v2_migration.shutil.disk_usage",
            side_effect=OSError("Filesystem free space could not be measured."),
        ):
            preview = self.manager.preview({"mode": "migrate"})
        self.assertFalse(preview["ready"])
        self.assertEqual(preview["capacity"]["status"], "unavailable")
        self.assertFalse(preview["capacity"]["complete"])
        self.assertEqual(preview["capacity"]["filesystems"], [])
        self.assertTrue(
            all(value is None for value in preview["capacity"]["source"].values())
        )

    def test_capacity_free_space_changes_do_not_change_preview_fingerprint(self):
        with patch(
            "utils.infinidysk_v2_migration.shutil.disk_usage",
            return_value=SimpleNamespace(free=10 * 1024**3),
        ):
            first = self.manager.preview({"mode": "migrate"})
        with patch(
            "utils.infinidysk_v2_migration.shutil.disk_usage",
            return_value=SimpleNamespace(free=20 * 1024**3),
        ):
            second = self.manager.preview({"mode": "migrate"})
        self.assertTrue(first["ready"])
        self.assertTrue(second["ready"])
        self.assertEqual(first["plan_fingerprint"], second["plan_fingerprint"])
        self.assertNotEqual(
            first["capacity"]["filesystems"][0]["free_bytes"],
            second["capacity"]["filesystems"][0]["free_bytes"],
        )

    def test_space_lost_after_preview_blocks_dispatch_before_any_executor(self):
        _, request = self.preview_request()
        with patch(
            "utils.infinidysk_v2_migration.shutil.disk_usage",
            return_value=SimpleNamespace(free=1),
        ):
            with self.assertRaisesRegex(InfiniDyskV2MigrationError, "free space"):
                self.manager.rehearse(request)
        self.assertEqual(self.executor.calls, [])
        self.assertIsNone(self.manager._thread)

    def test_space_lost_after_dispatch_blocks_cold_copy(self):
        self.executor.gate = threading.Event()
        _, request = self.preview_request()
        response = self.manager.rehearse(request)
        try:
            self.assertTrue(self.executor.started.wait(timeout=2))
            with (
                patch(
                    "utils.infinidysk_v2_migration.shutil.disk_usage",
                    return_value=SimpleNamespace(free=1),
                ),
                patch("utils.infinidysk_v2_migration.shutil.copytree") as copying,
            ):
                self.executor.gate.set()
                job = self.wait(response)
            self.assertFalse(job["result"]["validated"])
            self.assertIn("free space", job["error"])
            copying.assert_not_called()
        finally:
            self.executor.gate.set()

    def test_known_cold_manifest_budget_refuses_before_source_copy(self):
        for index in range(30):
            (self.source / "blobs" / (f"blob-{index}-" + "x" * 180)).touch()
        _, request = self.preview_request()
        with (
            patch("utils.infinidysk_v2_inventory.MAX_TREE_MANIFEST_BYTES", 8192),
            patch("utils.infinidysk_v2_migration.shutil.copytree") as copying,
        ):
            job = self.wait(self.manager.rehearse(request))
        self.assertFalse(job["result"]["validated"])
        self.assertIn("manifest", job["error"])
        copying.assert_not_called()

    def test_retained_tree_verification_ignores_directory_allocation_size(self):
        from utils.infinidysk_v2_inventory import tree_fingerprint
        import os
        import stat

        original = tree_fingerprint(self.source, scratch_root=self.root)
        original_stat = os.stat

        def different_directory_allocation(path, *args, **kwargs):
            value = original_stat(path, *args, **kwargs)
            if path == "blobs" and stat.S_ISDIR(value.st_mode):
                fields = {
                    field: getattr(value, field)
                    for field in dir(value)
                    if field.startswith("st_")
                }
                fields["st_size"] += 8192
                return SimpleNamespace(**fields)
            return value

        with patch(
            "utils.infinidysk_v2_inventory.os.stat", different_directory_allocation
        ):
            changed_allocation = tree_fingerprint(self.source, scratch_root=self.root)
        self.assertEqual(original, changed_allocation)
        (self.source / "blobs" / "original").write_text("changed actual bytes")
        self.assertNotEqual(
            original, tree_fingerprint(self.source, scratch_root=self.root)
        )

    def test_combined_and_legacy_rclone_core_aliases_are_inventoried_and_owned(self):
        instance = self.config.config["rclone"]["instances"]["usenet"]
        instance.pop("key_type")
        instance["core_service"] = "decypharr,infinidysk"
        preview = self.manager.preview({"mode": "migrate", "mount_backend": "fuse"})
        self.assertTrue(preview["ready"], preview["blockers"])
        self.assertEqual(len(preview["inventory"]["mounts"]), 1)
        plan = self.manager._plan({"mode": "migrate", "mount_backend": "fuse"})
        self.assertIn(["rclone", "instances", "usenet"], plan["owned_config_paths"])
        instance["core_service"] = "nzbdav"
        preview = self.manager.preview({"mode": "migrate", "mount_backend": "fuse"})
        self.assertTrue(preview["ready"], preview["blockers"])
        self.assertEqual(len(preview["inventory"]["mounts"]), 1)

    def test_postgres_identity_preflight_is_required_and_bound_without_live_contents(
        self,
    ):
        self.config.config["infinidysk"]["postgres_enabled"] = True
        self.executor.supported_source_providers = ("sqlite", "postgres")
        missing = self.manager.preview({"mode": "migrate"})
        self.assertFalse(missing["ready"])
        self.assertTrue(
            any("identity preflight" in item for item in missing["blockers"])
        )
        binding = {
            "physical_identity": {"system_identifier": "123", "database_oid": "456"},
            "schema_fingerprint": "a" * 64,
            "migration_history_fingerprint": "b" * 64,
            "terminal_migration": "20260824160000_Add-Health-Repair-Pending",
            "session_timezone": "UTC",
            "sequence_state": {"live": 1},
            "database_size_bytes": 1024,
        }
        self.executor.preflight = lambda plan: {
            "blockers": [],
            "source_binding": binding,
        }
        first = self.manager.preview({"mode": "migrate"})
        self.assertTrue(first["ready"], first["blockers"])
        binding["sequence_state"]["live"] = 2
        second = self.manager.preview({"mode": "migrate"})
        self.assertEqual(first["plan_fingerprint"], second["plan_fingerprint"])
        binding["physical_identity"]["database_oid"] = "789"
        changed = self.manager.preview({"mode": "migrate"})
        self.assertNotEqual(first["plan_fingerprint"], changed["plan_fingerprint"])

    def archive_profile(self):
        from utils.backup_storage import get_backup_storage

        self.config.config["infinidysk"]["v2_data_dir"] = str(
            self.root / "local" / "data-v2"
        )
        self.config.config["infinidysk"]["v2_runtime_dir"] = str(
            self.root / "local" / "runtime-v2"
        )
        destination = self.root / "cold-storage"
        destination.mkdir()
        storage = get_backup_storage(self.config)
        storage.save_profile(
            {
                "id": "nas",
                "name": "Cold archive",
                "path": str(destination),
                "enabled": True,
            }
        )
        return storage, destination

    def archive_request(self):
        preview = self.manager.preview(
            {"mode": "migrate", "backup_destination_id": "nas"}
        )
        self.assertTrue(preview["ready"], preview["blockers"])
        return preview, {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": dict.fromkeys(ACKNOWLEDGEMENTS, True),
        }

    def test_selected_archive_captures_originals_without_full_local_backup_tree(self):
        from utils.infinidysk_v2_archive import verify_retained_archive

        storage, destination = self.archive_profile()
        original = (self.source / "db.sqlite").read_bytes()
        preview, request = self.archive_request()
        self.assertEqual(preview["backup_storage"]["mode"], "cold_archive")
        self.assertNotIn("binding_fingerprint", preview["backup_storage"])
        self.assertNotIn("artifact_root", preview["backup_storage"])
        with patch(
            "utils.infinidysk_v2_migration.shutil.copytree", wraps=shutil.copytree
        ) as copying:
            job = self.wait(self.manager.rehearse(request))
        self.assertEqual(job["status"], "rehearsed", job)
        private = self.manager._read(self.manager._job_path(job["job_id"]))
        snapshot = private["snapshot"]
        workspace = self.manager._workspace(private)
        self.assertEqual(snapshot["backup_format"], "pax_tar_v1")
        self.assertNotIn("data_snapshot", snapshot)
        self.assertFalse((Path(snapshot["source_snapshot"]) / "source-data").exists())
        self.assertFalse(
            (Path(snapshot["source_snapshot"]) / "source-runtime").exists()
        )
        with closing(sqlite3.connect(snapshot["source_database"])) as database:
            self.assertEqual(
                database.execute("SELECT Value FROM OriginalData").fetchall(),
                [("original",)],
            )
        self.assertNotEqual(
            Path(snapshot["source_database"]), self.source / "db.sqlite"
        )
        self.assertEqual(
            snapshot["candidate_identity"]["inode"],
            Path(workspace["candidate_data"]).stat().st_ino,
        )
        self.assertEqual(
            (Path(workspace["candidate_data"]) / "blobs" / "original").read_text(),
            "original metadata",
        )
        self.assertTrue(
            any(
                Path(call.args[0]) == self.source
                and Path(call.args[1]) == Path(workspace["candidate_data"])
                for call in copying.call_args_list
            )
        )
        self.assertEqual((self.source / "db.sqlite").read_bytes(), original)
        self.assertEqual(
            len(storage.list_artifacts("nas", "major-" + job["job_id"])), 1
        )
        self.assertTrue(
            verify_retained_archive(snapshot, private["plan"], storage)["verified"]
        )

    def test_selected_archive_capacity_moves_only_retained_tree_to_destination(self):
        from utils.infinidysk_v2_inventory import MAX_TREE_MANIFEST_BYTES
        from utils.infinidysk_v2_migration import MAX_STATE_BYTES

        storage, _ = self.archive_profile()
        source_bytes, entries = 64 * 1024**3, 350_000
        summary = {"bytes": source_bytes, "entries": entries}
        captured = []

        def capacity(requirements, **kwargs):
            captured.extend(requirements)
            return {"status": "sufficient"}

        with (
            patch(
                "utils.infinidysk_v2_migration._source_summary", return_value=summary
            ),
            patch(
                "utils.infinidysk_v2_migration._capacity_check", side_effect=capacity
            ),
        ):
            self.manager._plan({"mode": "migrate", "backup_destination_id": "nas"})
        by_role = {role: (path, size) for path, size, role in captured}
        database_bytes = (self.source / "db.sqlite").stat().st_size
        self.assertEqual(
            by_role["local_verification"][1],
            2 * database_bytes + MAX_TREE_MANIFEST_BYTES + MAX_STATE_BYTES,
        )
        self.assertGreaterEqual(by_role["cold_archive"][1], source_bytes)
        self.assertEqual(
            by_role["cold_archive"][0],
            Path(storage.bound_identity("nas")["artifact_root"]),
        )
        self.assertGreaterEqual(by_role["candidate"][1], source_bytes)
        self.assertNotIn("retained_backup", by_role)

    def test_selected_archive_destination_cannot_overlap_source_or_control(self):
        from utils.backup_storage import get_backup_storage

        storage = get_backup_storage(self.config)
        storage.save_profile(
            {
                "id": "nas",
                "name": "Unsafe overlap",
                "path": str(self.source),
                "enabled": True,
            }
        )
        preview = self.manager.preview(
            {"mode": "migrate", "backup_destination_id": "nas"}
        )
        self.assertFalse(preview["ready"])
        self.assertTrue(any("separate" in message for message in preview["blockers"]))
        self.assertEqual(self.executor.calls, [])

    def test_selected_archive_replacement_blocks_dispatch_without_local_fallback(self):
        self.archive_profile()
        _, request = self.archive_request()
        destination = self.root / "cold-storage"
        destination.rename(self.root / "old-cold-storage")
        destination.mkdir()
        with self.assertRaisesRegex(
            InfiniDyskV2MigrationError, "unavailable|identity|destination"
        ):
            self.manager.rehearse(request)
        self.assertEqual(self.executor.calls, [])
        self.assertFalse((destination / ".dumb-backups").exists())

    def test_archive_io_does_not_hold_manager_status_lock(self):
        from utils.infinidysk_v2_archive import create_retained_archive

        self.archive_profile()
        _, request = self.archive_request()
        entered, release = threading.Event(), threading.Event()

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=4):
                raise TimeoutError("fixture release was not signalled")
            return create_retained_archive(*args, **kwargs)

        with patch(
            "utils.infinidysk_v2_archive.create_retained_archive", side_effect=blocked
        ):
            response = self.manager.rehearse(request)
            try:
                self.assertTrue(entered.wait(timeout=2))
                self.assertTrue(self.manager._lock.acquire(timeout=0.2))
                self.manager._lock.release()
                status = self.manager.status()
                self.assertEqual(status["job"]["status"], "running")
                self.assertTrue(self.manager.has_blocking_job())
            finally:
                release.set()
                job = self.wait(response)
        self.assertEqual(job["status"], "rehearsed", job)

    def test_archive_source_addition_before_acceptance_is_detected(self):
        from utils.infinidysk_v2_archive import create_retained_archive

        self.archive_profile()
        _, request = self.archive_request()

        def changed(*args, **kwargs):
            receipt = create_retained_archive(*args, **kwargs)
            (self.source / "blobs" / "late-entry").write_text("unbacked bytes")
            return receipt

        with (
            patch(
                "utils.infinidysk_v2_archive.create_retained_archive",
                side_effect=changed,
            ),
            patch("utils.infinidysk_v2_migration.shutil.copytree") as copying,
        ):
            job = self.wait(self.manager.rehearse(request))
        self.assertEqual(job["status"], "failed_restored", job)
        self.assertIn("source changed", job["error"])
        copying.assert_not_called()

    def test_archive_postgres_capture_retains_original_tree_and_local_verified_staging(
        self,
    ):
        from utils.infinidysk_v2_archive import verify_retained_archive

        storage, _ = self.archive_profile()
        self.config.config["infinidysk"]["postgres_enabled"] = True
        self.executor.supported_source_providers = ("sqlite", "postgres")
        binding = {
            "physical_identity": {"system_identifier": "123", "database_oid": "456"},
            "schema_fingerprint": "a" * 64,
            "migration_history_fingerprint": "b" * 64,
            "terminal_migration": "20260824160000_Add-Health-Repair-Pending",
            "session_timezone": "UTC",
            "database_size_bytes": 1024,
        }
        self.executor.preflight = lambda plan: {
            "blockers": [],
            "source_binding": binding,
        }
        original = (self.source / "db.sqlite").read_bytes()

        def rehearse(plan, workspace, progress):
            root = Path(workspace["root"])
            root.mkdir(parents=True, mode=0o700)
            staged, dump = root / "v1-postgres-import.sqlite", root / "v1-postgres.dump"
            shutil.copyfile(self.source / "db.sqlite", staged)
            with closing(sqlite3.connect(staged)) as database:
                database.execute("PRAGMA journal_mode=DELETE")
                database.execute(
                    "INSERT INTO OriginalData VALUES('read-only PostgreSQL snapshot')"
                )
                database.commit()
            dump.write_bytes(b"closed independently restore-verified fixture dump")
            workspace["capture_source"](
                postgres_snapshot={
                    "staged_sqlite": str(staged),
                    "dump_path": str(dump),
                    "verified_restore": True,
                    "source_read_only": True,
                    "physical_identity": binding["physical_identity"],
                    "source_binding": {"binding_fingerprint": "c" * 64},
                    "restore_proof": {"verified": True, "source_binding": "c" * 64},
                    "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
                    "staged_sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
                }
            )
            receipt = self.executor.receipt(plan)
            workspace["accept_import"](receipt)
            return {**receipt, "source_restored": True}

        self.executor.rehearse = rehearse
        _, request = self.archive_request()
        job = self.wait(self.manager.rehearse(request))
        self.assertEqual(job["status"], "rehearsed", job)
        private = self.manager._read(self.manager._job_path(job["job_id"]))
        snapshot = private["snapshot"]
        self.assertEqual(
            Path(snapshot["source_database"]),
            Path(snapshot["source_snapshot"]) / "v1-postgres-import.sqlite",
        )
        self.assertTrue(snapshot["postgres_snapshot"]["verified_restore"])
        self.assertEqual((self.source / "db.sqlite").read_bytes(), original)
        candidate = Path(self.manager._workspace(private)["candidate_data"])
        with sqlite3.connect(candidate / "db.sqlite") as database:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM OriginalData").fetchone()[0], 2
            )
        self.assertTrue(
            verify_retained_archive(snapshot, private["plan"], storage)["verified"]
        )

    def test_fresh_selected_archive_keeps_candidate_local_without_source_database(self):
        self.archive_profile()
        shutil.rmtree(self.source)
        preview = self.manager.preview(
            {"mode": "fresh", "backup_destination_id": "nas"}
        )
        self.assertTrue(preview["ready"], preview["blockers"])
        request = {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": dict.fromkeys(ACKNOWLEDGEMENTS, True),
        }
        job = self.wait(self.manager.start(request))
        self.assertEqual(job["status"], "completed", job)
        private = self.manager._read(self.manager._job_path(job["job_id"]))
        self.assertIsNone(private["snapshot"]["source_database"])
        self.assertTrue(private["snapshot"]["cold_archive"]["verified"])
        self.assertTrue(
            (self.root / "local" / "data-v2" / "infinidysk.sqlite").is_file()
        )
        self.assertFalse(self.source.exists())

    def make_retained_wal(self):
        # Produce a cold main DB + committed WAL without opening the originals.
        raw = self.root / "wal-fixture"
        raw.mkdir()
        shutil.copyfile(self.source / "db.sqlite", raw / "db.sqlite")
        database = sqlite3.connect(raw / "db.sqlite")
        try:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute("PRAGMA wal_autocheckpoint=0")
            database.execute("INSERT INTO OriginalData VALUES('committed WAL only')")
            database.commit()
            for suffix in ("", "-wal", "-shm"):
                shutil.copyfile(
                    raw / ("db.sqlite" + suffix), self.source / ("db.sqlite" + suffix)
                )
        finally:
            database.close()
        return {
            suffix: (self.source / ("db.sqlite" + suffix)).read_bytes()
            for suffix in ("", "-wal", "-shm")
        }

    def test_archive_sqlite_wal_rows_are_in_closed_independent_validation_database(
        self,
    ):
        self.archive_profile()
        original = self.make_retained_wal()
        _, request = self.archive_request()
        job = self.wait(self.manager.rehearse(request))
        self.assertEqual(job["status"], "rehearsed", job)
        private = self.manager._read(self.manager._job_path(job["job_id"]))
        snapshot = private["snapshot"]
        closed = Path(snapshot["source_database"])
        self.assertEqual(closed.name, "source-database.sqlite")
        self.assertFalse((closed.parent / "raw").exists())
        self.assertFalse(Path(str(closed) + "-wal").exists())
        with sqlite3.connect(f"file:{closed}?mode=ro", uri=True) as database:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM OriginalData").fetchone()[0], 2
            )
        self.assertEqual(snapshot["source_schema"]["tables"]["OriginalData"], 2)
        for suffix, contents in original.items():
            self.assertEqual(
                (self.source / ("db.sqlite" + suffix)).read_bytes(), contents
            )

    def test_archive_capacity_includes_all_raw_sqlite_sidecars_and_closed_peak(self):
        from utils.infinidysk_v2_inventory import MAX_TREE_MANIFEST_BYTES
        from utils.infinidysk_v2_migration import MAX_STATE_BYTES

        self.archive_profile()
        self.make_retained_wal()
        (self.source / "db.sqlite-journal").write_bytes(
            b"private fixture journal bytes"
        )
        raw_bytes = sum(
            (self.source / ("db.sqlite" + suffix)).stat().st_size
            for suffix in ("", "-wal", "-shm", "-journal")
        )
        requirements = []

        def capacity(rows, **kwargs):
            requirements.extend(rows)
            return {"status": "sufficient"}

        with patch(
            "utils.infinidysk_v2_migration._capacity_check", side_effect=capacity
        ):
            self.manager._plan({"mode": "migrate", "backup_destination_id": "nas"})
        local = next(
            size for _, size, role in requirements if role == "local_verification"
        )
        self.assertEqual(
            local, 2 * raw_bytes + MAX_TREE_MANIFEST_BYTES + MAX_STATE_BYTES
        )
        archived = next(
            size for _, size, role in requirements if role == "cold_archive"
        )
        self.assertGreater(archived, raw_bytes)

    def test_sibling_archive_path_is_allowed_but_reserved_candidate_namespace_is_not(
        self,
    ):
        storage, _ = self.archive_profile()
        self.config.config["infinidysk"]["v2_data_dir"] = str(self.root / "data-v2")
        self.config.config["infinidysk"]["v2_runtime_dir"] = str(
            self.root / "runtime-v2"
        )
        preview, _ = self.archive_request()
        self.assertTrue(preview["ready"])
        reserved = self.root / (".data-v2.dumb-v2-" + "a" * 32)
        reserved.mkdir()
        storage.save_profile(
            {
                "id": "reserved",
                "name": "Unsafe candidate",
                "path": str(reserved),
                "enabled": True,
            }
        )
        preview = self.manager.preview(
            {"mode": "migrate", "backup_destination_id": "reserved"}
        )
        self.assertFalse(preview["ready"])
        self.assertTrue(any("separate" in value for value in preview["blockers"]))

    def test_pg_candidate_stage_discards_only_copied_stale_sqlite_sidecars(self):
        import stat

        original = self.make_retained_wal()
        staged = self.root / "verified-pg.sqlite"
        shutil.copyfile(self.source / "db.sqlite", staged)
        with closing(sqlite3.connect(staged)) as database:
            database.execute("PRAGMA journal_mode=DELETE")
            database.execute("INSERT INTO OriginalData VALUES('verified PG state')")
            database.commit()
        for mode in ("legacy", "archive"):
            candidate = self.root / ("candidate-" + mode)
            shutil.copytree(self.source, candidate)
            value = candidate.stat()
            identity = {
                "device": value.st_dev,
                "inode": value.st_ino,
                "mode": stat.S_IFDIR,
            }
            self.manager._stage_candidate_database(candidate, staged, identity)
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse((candidate / ("db.sqlite" + suffix)).exists())
            with sqlite3.connect(
                f"file:{candidate / 'db.sqlite'}?mode=ro", uri=True
            ) as database:
                values = {
                    row[0] for row in database.execute("SELECT Value FROM OriginalData")
                }
            self.assertEqual(values, {"original", "verified PG state"})
        for suffix, contents in original.items():
            self.assertEqual(
                (self.source / ("db.sqlite" + suffix)).read_bytes(), contents
            )

    def test_legacy_plan_blocks_physical_source_control_alias_without_selected_storage(
        self,
    ):
        from utils.backup_storage import paths_overlap

        def aliases(first, second):
            if {Path(first), Path(second)} == {self.source, self.manager.state_root}:
                return True
            return paths_overlap(first, second)

        with patch("utils.backup_storage.paths_overlap", side_effect=aliases):
            preview = self.manager.preview({"mode": "migrate"})
        self.assertFalse(preview["ready"])
        self.assertTrue(
            any("outside the retained" in value for value in preview["blockers"])
        )
        self.assertEqual(self.executor.calls, [])


class InfiniDyskV2CapacityTests(unittest.TestCase):
    def capacity(self, requirements, devices, free):
        from utils.infinidysk_v2_migration import _capacity_check

        class Destination:
            def __init__(self, value):
                self.value = value

            def exists(self):
                return True

            def stat(self):
                return SimpleNamespace(st_dev=devices[self.value])

        def usage(path):
            available = free[devices[path.value]]
            if isinstance(available, Exception):
                raise available
            return SimpleNamespace(free=available)

        with (
            patch("utils.infinidysk_v2_migration.Path", Destination),
            patch("utils.infinidysk_v2_migration.shutil.disk_usage", side_effect=usage),
        ):
            return _capacity_check(requirements)

    def test_shared_filesystem_aggregates_requirements_and_preserves_shortfall_report(
        self,
    ):
        from utils.infinidysk_v2_migration import InfiniDyskV2CapacityError

        mib = 1024**2
        with self.assertRaises(InfiniDyskV2CapacityError) as caught:
            self.capacity(
                [
                    ("backup-private-root", 128 * mib, "retained_backup"),
                    ("candidate-private-root", 128 * mib, "candidate"),
                ],
                {"backup-private-root": 91, "candidate-private-root": 91},
                {91: 300 * mib},
            )
        report = caught.exception.capacity
        self.assertEqual(report["status"], "insufficient")
        self.assertTrue(report["complete"])
        self.assertEqual(len(report["filesystems"]), 1)
        row = report["filesystems"][0]
        self.assertEqual(row["id"], "filesystem_1")
        self.assertEqual(row["roles"], ["retained_backup", "candidate"])
        self.assertEqual(row["base_required_bytes"], 256 * mib)
        self.assertEqual(row["safety_margin_bytes"], 64 * mib)
        self.assertEqual(row["required_bytes"], 320 * mib)
        self.assertEqual(row["free_bytes"], 300 * mib)
        self.assertEqual(row["shortfall_bytes"], 20 * mib)
        self.assertIn("320.00 MiB required", str(caught.exception))
        self.assertIn("20.00 MiB short", str(caught.exception))
        self.assertNotIn("private-root", json.dumps(report))
        self.assertNotIn("device", json.dumps(report))

    def test_separate_filesystems_each_have_their_own_free_space_and_margin(self):
        mib = 1024**2
        report = self.capacity(
            [
                ("backup", 128 * mib, "retained_backup"),
                ("candidate", 128 * mib, "candidate"),
            ],
            {"backup": 91, "candidate": 92},
            {91: 192 * mib, 92: 256 * mib},
        )
        self.assertEqual(report["status"], "sufficient")
        self.assertEqual(len(report["filesystems"]), 2)
        self.assertEqual(
            [row["free_bytes"] for row in report["filesystems"]],
            [192 * mib, 256 * mib],
        )
        for row in report["filesystems"]:
            self.assertEqual(row["required_bytes"], 192 * mib)
            self.assertEqual(row["shortfall_bytes"], 0)
            self.assertTrue(row["sufficient"])

    def test_report_collects_all_short_filesystems_before_refusing(self):
        from utils.infinidysk_v2_migration import InfiniDyskV2CapacityError

        mib = 1024**2
        with self.assertRaises(InfiniDyskV2CapacityError) as caught:
            self.capacity(
                [
                    ("backup", 128 * mib, "retained_backup"),
                    ("temporary", 128 * mib, "postgres_temporary"),
                ],
                {"backup": 91, "temporary": 92},
                {91: 120 * mib, 92: 150 * mib},
            )
        self.assertEqual(
            [
                row["shortfall_bytes"]
                for row in caught.exception.capacity["filesystems"]
            ],
            [72 * mib, 42 * mib],
        )

    def test_later_filesystem_probe_failure_never_returns_a_partial_report(self):
        mib = 1024**2
        with self.assertRaisesRegex(OSError, "second filesystem unavailable"):
            self.capacity(
                [
                    ("backup", 128 * mib, "retained_backup"),
                    ("candidate", 128 * mib, "candidate"),
                ],
                {"backup": 91, "candidate": 92},
                {91: 256 * mib, 92: OSError("second filesystem unavailable")},
            )

    def test_ten_percent_margin_is_included_in_required_bytes(self):
        base = 2 * 1024**3
        report = self.capacity(
            [("backup", base, "retained_backup")], {"backup": 91}, {91: 4 * base}
        )
        row = report["filesystems"][0]
        self.assertEqual(row["safety_margin_bytes"], base // 10)
        self.assertEqual(row["required_bytes"], base + base // 10)


if __name__ == "__main__":
    unittest.main()
