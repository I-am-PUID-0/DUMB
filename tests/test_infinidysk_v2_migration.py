import copy
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
            patch("utils.infinidysk_v2_migration.MAX_TREE_ENTRIES", 1),
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

    def test_retained_tree_verification_ignores_directory_allocation_size(self):
        from utils.infinidysk_v2_migration import _tree

        original = _tree(self.source, content=True)
        original_lstat = Path.lstat

        def different_directory_allocation(path, *args, **kwargs):
            value = original_lstat(path, *args, **kwargs)
            if path.is_dir() and path == self.source / "blobs":
                return SimpleNamespace(
                    st_mode=value.st_mode,
                    st_size=value.st_size + 8192,
                    st_mtime_ns=value.st_mtime_ns,
                )
            return value

        with patch.object(Path, "lstat", different_directory_allocation):
            changed_allocation = _tree(self.source, content=True)
        self.assertEqual(original, changed_allocation)
        (self.source / "blobs" / "original").write_text("changed actual bytes")
        self.assertNotEqual(original, _tree(self.source, content=True))

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


if __name__ == "__main__":
    unittest.main()
