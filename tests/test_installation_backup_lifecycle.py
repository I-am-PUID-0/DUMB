"""Retained-set recovery ordering, boot handoff and cold-entry diagnostics."""

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from tests import test_installation_backup as fixtures
from utils import installation_backup as module
from utils.installation_backup import InstallationBackupError
from utils.installation_backup_archive import InstallationArchiveError, write_archive
from utils.installation_backup_lifecycle import (
    BackupLifecycleError,
    order_resume_rows,
    resume_wait_dependencies,
)


class ResumeOrderingTests(unittest.TestCase):
    def test_legacy_plex_first_mount_provider_last_is_reordered_exactly(self):
        rows = [
            {"key": "plex", "name": "Plex", "pid": 401},
            {"key": "postgres", "name": "PostgreSQL", "pid": 402},
            {"key": "infinidysk", "name": "IFD", "pid": 403},
            {"key": "rclone", "name": "Rclone", "pid": 404},
        ]
        config = {
            "plex": {
                "process_name": "Plex",
                "wait_for_mounts": ["/reserved/mount/ifd"],
            },
            "postgres": {"process_name": "PostgreSQL", "port": 5432},
            "infinidysk": {
                "process_name": "IFD",
                "port": 3000,
                "wait_for_tcp": {"host": "127.0.0.1", "port": 5432},
            },
            "rclone": {
                "instances": {
                    "ifd": {
                        "process_name": "Rclone",
                        "mount_dir": "/reserved/mount",
                        "mount_name": "ifd",
                        "wait_for_url": [{"url": "http://localhost:3000/health"}],
                    },
                    "unrecorded": {"process_name": "Unrecorded", "enabled": True},
                }
            },
        }
        original = deepcopy(rows)
        ordered = order_resume_rows(rows, config, {})
        self.assertEqual(
            [row["name"] for row in ordered], ["PostgreSQL", "IFD", "Rclone", "Plex"]
        )
        self.assertEqual(rows, original)
        self.assertEqual(sorted(ordered, key=lambda row: row["pid"]), original)
        ordered[0]["pid"] = 0
        self.assertEqual(rows, original)

    def test_native_mount_directory_and_local_endpoint_waits_are_combined(self):
        config = {
            "consumer": {
                "process_name": "Consumer",
                "wait_for_mounts": ["/reserved/native"],
                "wait_for_dir": "/reserved/provider/state/child",
                "wait_for_tcp": [{"host": "::1", "port": 9180}],
                "wait_for_url": [
                    {"url": "https://127.0.0.1:9443/health"},
                    {"url": "https://external.example:9555/health"},
                    {"url": "http:///invalid"},
                ],
            },
            "infinidysk": {
                "process_name": "Native",
                "runtime_generation": 2,
                "mount_backend": "fuse",
                "mount_path": "/reserved/native",
            },
            "directory": {
                "process_name": "Directory",
                "data_dir": "/reserved/provider/state",
            },
            "tcp": {"process_name": "TCP", "port": 9180, "host": "::"},
            "https": {"process_name": "HTTPS", "port": 9443},
            "external": {"process_name": "External", "port": 9555},
            "invalid": {"process_name": "Invalid", "port": 80},
        }
        rows = [
            {"key": key, "name": value["process_name"]} for key, value in config.items()
        ]
        edges = resume_wait_dependencies(rows, config)
        self.assertEqual(edges["Consumer"], {"Native", "Directory", "TCP", "HTTPS"})
        ordered = order_resume_rows(rows, config, {})
        names = [row["name"] for row in ordered]
        self.assertLess(names.index("Native"), names.index("Consumer"))
        self.assertNotIn("External", edges["Consumer"])
        self.assertNotIn("Invalid", edges["Consumer"])

    def test_family_dependencies_and_longest_path_owner_are_preserved(self):
        config = {
            "consumer": {
                "process_name": "Consumer",
                "wait_for_dir": "/reserved/data/specific/child",
            },
            "broad": {"process_name": "Broad", "data_dir": "/reserved/data"},
            "specific": {
                "process_name": "Specific",
                "data_dir": "/reserved/data/specific",
            },
            "database": {"process_name": "DB"},
        }
        rows = [
            {"key": key, "name": value["process_name"]} for key, value in config.items()
        ]
        self.assertEqual(
            resume_wait_dependencies(rows, config)["Consumer"], {"Specific"}
        )
        result = order_resume_rows(rows, config, {"specific": {"database"}})
        names = [row["name"] for row in result]
        self.assertLess(names.index("DB"), names.index("Specific"))
        self.assertLess(names.index("Specific"), names.index("Consumer"))

    def test_cycles_ambiguous_owners_and_changed_registry_are_rejected(self):
        config = {
            "first": {
                "process_name": "First",
                "port": 9101,
                "wait_for_tcp": {"port": 9102},
            },
            "second": {
                "process_name": "Second",
                "port": 9102,
                "wait_for_tcp": {"port": 9101},
            },
        }
        rows = [
            {"key": key, "name": value["process_name"]} for key, value in config.items()
        ]
        with self.assertRaisesRegex(BackupLifecycleError, "cycle"):
            order_resume_rows(rows, config, {})
        config["second"].pop("wait_for_tcp")
        config["second"]["port"] = 9101
        config["first"]["wait_for_tcp"] = {"port": 9101}
        config["third"] = {"process_name": "Third", "port": 9101}
        rows.append({"key": "third", "name": "Third"})
        with self.assertRaisesRegex(BackupLifecycleError, "multiple retained endpoint"):
            order_resume_rows(rows, config, {})
        with self.assertRaisesRegex(BackupLifecycleError, "matching configured"):
            order_resume_rows([{"key": "removed", "name": "Old"}], config, {})
        with self.assertRaisesRegex(BackupLifecycleError, "malformed or ambiguous"):
            order_resume_rows([rows[0], rows[0]], config, {})


class RecoveryLifecycleTests(unittest.TestCase):
    setUp = fixtures.ManagerTests.setUp
    join_worker = fixtures.ManagerTests.join_worker
    start_backup = fixtures.ManagerTests.start_backup

    def failed_record(self):
        with (
            patch.object(
                module,
                "write_archive",
                side_effect=InstallationArchiveError("Fixture changed entry"),
            ),
            patch.object(
                self.manager,
                "_resume",
                side_effect=InstallationBackupError("Fixture mount wait"),
            ),
        ):
            job = self.start_backup()
        self.assertEqual(job["status"], "recovery_required", job)
        self.assertEqual(self.manager.catalog("fixture")["artifacts"], [])
        return job["job_id"]

    def new_controller(self, label):
        with patch.object(
            module,
            "_boot_identity",
            return_value=[label, "reserved-process", "reserved-boot"],
        ):
            manager = module.InstallationBackupManager(
                self.config, self.handler, storage=self.storage
            )
        manager.workspace_root = self.manager.workspace_root
        return manager

    def test_resume_reorders_retained_legacy_job_and_never_adds_enabled_services(self):
        identifier = self.failed_record()
        self.config.config["plex"] = {
            "enabled": True,
            "process_name": "Plex",
            "wait_for_mounts": ["/reserved/mount/ifd"],
        }
        self.config.config["rclone"] = {
            "instances": {
                "retained": {
                    "enabled": True,
                    "process_name": "Rclone",
                    "mount_dir": "/reserved/mount",
                    "mount_name": "ifd",
                },
                "extra": {"enabled": True, "process_name": "Unrecorded"},
            }
        }
        job = self.manager._job(identifier)
        job["running_before"] = [
            {"key": "plex", "name": "Plex"},
            {"key": "rclone", "name": "Rclone"},
        ]
        self.manager._save(job)
        original = self.handler.start_process
        observed = []

        def start(name):
            current = self.manager.status()["job"]
            observed.append((name, current["status"], current["stage"]))
            if name == "Plex":
                self.assertIn("Rclone", self.handler.process_names)
            return original(name)

        before = len(self.handler.events)
        with patch.object(self.handler, "start_process", start):
            result = self.manager.rollback(
                identifier, {"confirmation": "ROLL BACK " + identifier}
            )
        self.assertEqual(
            self.handler.events[before:], [("start", "Rclone"), ("start", "Plex")]
        )
        self.assertEqual(
            observed,
            [("Rclone", "running", "resuming"), ("Plex", "running", "resuming")],
        )
        self.assertEqual(result["job"]["status"], "rolled_back")
        self.assertFalse(result["job"]["restart_required"])

    def test_live_original_pid_still_prevents_duplicate_start(self):
        identifier = self.failed_record()
        job = self.manager._job(identifier)
        job["running_before"][0]["pid"] = 2147483640
        self.manager._save(job)
        before = list(self.handler.events)
        with patch.object(self.manager, "_pid_alive", return_value=True):
            with self.assertRaisesRegex(InstallationBackupError, "recovery review"):
                self.manager.rollback(
                    identifier, {"confirmation": "ROLL BACK " + identifier}
                )
        self.assertEqual(self.handler.events, before)
        self.assertEqual(self.manager.status()["job"]["status"], "recovery_required")

    def test_dead_same_session_worker_becomes_recovery_but_live_rollback_does_not(self):
        identifier = self.failed_record()
        job = self.manager._job(identifier)
        job.update(status="running", stage="resuming")
        self.manager._save(job)
        self.manager._rollback_running = True
        self.assertEqual(self.manager.status()["job"]["status"], "running")
        self.manager._rollback_running = False
        result = self.manager.status()["job"]
        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(result["stage"], "interrupted")
        self.assertTrue(result["can_rollback"])
        self.assertTrue(self.manager.has_blocking_job())

    def test_failure_reports_resuming_while_worker_is_live_then_terminal_failure(self):
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        preview = self.manager.preview({"destination_id": "fixture"})
        request = {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": {
                "downtime": True,
                "secrets": True,
                "external_state": True,
            },
        }
        original = self.manager._resume

        def resume(identifier):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(identifier)

        with (
            patch.object(
                module,
                "write_archive",
                side_effect=InstallationArchiveError("Fixture cold identity change"),
            ),
            patch.object(self.manager, "_resume", resume),
        ):
            try:
                self.manager.start(request)
                self.assertTrue(entered.wait(5))
                job = self.manager.status()["job"]
                self.assertEqual((job["status"], job["stage"]), ("running", "resuming"))
                self.assertEqual(job["errors"], ["Fixture cold identity change"])
                with self.assertRaisesRegex(
                    InstallationBackupError, "writer is still live"
                ):
                    self.manager.rollback(
                        job["job_id"],
                        {"confirmation": job["rollback_confirmation_text"]},
                    )
            finally:
                release.set()
                self.join_worker()
        final = self.manager.status()["job"]
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["errors"], ["Fixture cold identity change"])
        self.assertFalse(self.manager.has_blocking_job())
        self.assertEqual(list(self.root.joinpath("archives").rglob("*.partial")), [])

    def test_cross_boot_backup_rollback_preserves_originals_until_second_verified_boot(
        self,
    ):
        identifier = self.failed_record()
        payload = self.payload.read_bytes()
        config = Path(self.config.file_path).read_bytes()
        auth_path = Path(os.environ["DUMB_AUTH_CONFIG_PATH"])
        auth = auth_path.read_bytes()
        first = self.new_controller("first-restart")
        before = list(self.handler.events)
        with (
            patch.object(module, "_MANAGER", first),
            patch.object(
                first, "_resume", side_effect=AssertionError("must not resume")
            ),
            patch.object(
                first,
                "_rollback_roots",
                side_effect=AssertionError("must not replace originals"),
            ),
        ):
            first.initialize_controller()
            result = first.rollback(
                identifier, {"confirmation": "ROLL BACK " + identifier}
            )
        self.assertEqual(result["job"]["status"], "rolled_back")
        self.assertTrue(result["job"]["restart_required"])
        self.assertTrue(first.has_blocking_job())
        self.assertEqual(self.handler.events, before)
        self.assertEqual(self.payload.read_bytes(), payload)
        self.assertEqual(Path(self.config.file_path).read_bytes(), config)
        self.assertEqual(auth_path.read_bytes(), auth)
        second = self.new_controller("second-restart")
        with patch.object(module, "_MANAGER", second):
            second.initialize_controller()
        self.assertFalse(second.has_blocking_job())
        self.assertFalse(second.get_job(identifier)["job"]["restart_required"])
        self.assertEqual(self.handler.events, before)

    def test_changed_config_keeps_cross_boot_rollback_held(self):
        identifier = self.failed_record()
        first = self.new_controller("first-restart")
        with patch.object(module, "_MANAGER", first):
            first.initialize_controller()
            first.rollback(identifier, {"confirmation": "ROLL BACK " + identifier})
        path = Path(self.config.file_path)
        path.write_bytes(path.read_bytes() + b"\\n")
        second = self.new_controller("second-restart")
        with patch.object(module, "_MANAGER", second):
            second.initialize_controller()
        self.assertTrue(second.has_blocking_job())
        self.assertTrue(second.get_job(identifier)["job"]["restart_required"])


class ChangedEntryDiagnosticsTests(unittest.TestCase):
    setUp = fixtures.ArchiveTests.setUp

    def test_changed_file_names_and_metadata_are_bounded_and_escaped(self):
        filename = "reserved\nname.sqlite"
        path = self.source / filename
        path.write_bytes(b"first")
        changed = []
        from utils import installation_backup_archive as archive

        original = archive._add

        def add(handle, row, reader=None):
            if row["name"] == "installation.json" and not changed:
                changed.append(True)
                path.write_bytes(b"changed payload never copied into diagnostic")
            return original(handle, row, reader)

        with patch.object(archive, "_add", add):
            with self.assertRaises(InstallationArchiveError) as caught:
                write_archive(io.BytesIO(), self.metadata, self.roots, self.scratch)
        detail = str(caught.exception)
        self.assertIn(json.dumps(str(path), ensure_ascii=True), detail)
        self.assertIn("changed metadata:", detail)
        self.assertIn("size", detail)
        self.assertNotIn("\n", detail)
        self.assertNotIn("changed payload never copied", detail)
        self.assertLess(len(detail), 1200)
        self.assertEqual(list(self.scratch.glob(".installation-manifest-*")), [])

    def test_path_bound_and_atime_are_independent_of_changed_metadata(self):
        from utils import installation_backup_archive as archive

        path = self.source / "payload"
        path.write_bytes(b"reserved")
        before = path.stat()
        after = os.stat_result(tuple(before))
        error = archive._changed_entry(
            "/" + "x" * 2000, archive._file_identity(before), after, "Changed entry"
        )
        self.assertLess(len(str(error)), 1100)
        self.assertIn("[path truncated]", str(error))
        self.assertNotIn("atime", str(error))


if __name__ == "__main__":
    unittest.main()
