"""Real manager failure/retry checkpoints preserve accepted generation 2 writes."""

import copy
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from tests import test_infinidysk_v2_migration as fixtures
from tests import test_infinidysk_v2_low_space_pipeline as pipeline_fixtures
from utils import infinidysk_v2_consumer_recovery as recovery
from utils.infinidysk_v2_execution import InfiniDyskV2Executor
from utils.infinidysk_v2_runtime import COMMIT, MARKER, RELEASE, native_auth


def isolated_contract(method):
    @wraps(method)
    def run(self):
        test_id = f"tests.{Path(__file__).stem}.{type(self).__name__}.{method.__name__}"
        if os.environ.get("DUMB_V2_RECOVERY_TEST") == test_id:
            return method(self)
        result = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", test_id],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "DUMB_V2_RECOVERY_TEST": test_id},
            capture_output=True,
            text=True,
            timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    return run


class ConsumerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.InfiniDyskV2MigrationTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root, self.config = self.fixture.root, self.fixture.config
        self.config.config.update(puid=os.geteuid() or 1000, pgid=os.getegid() or 1000)
        self.config.config["infinidysk"]["enabled"] = True
        for key, name, port in (
            ("prowlarr", "Prowlarr", 9696),
            ("radarr", "Radarr", 7878),
            ("sonarr", "Sonarr", 8989),
        ):
            self.config.config[key] = {
                "enabled": True,
                "process_name": name,
                "port": port,
            }
        self.config.config["whisparr"] = {
            "enabled": True,
            "process_name": "Stopped Whisparr",
        }
        self.handler = pipeline_fixtures.LowSpacePipelineTests.Handler()
        self.handler.start_prepared_process = self.handler.start_process
        for name in ("InfiniDysk", "rclone InfiniDysk", "Prowlarr", "Radarr", "Sonarr"):
            self.handler.start_process(name)
        self.handler.calls.clear()
        self.failure = "consumer"
        owner = self

        class Executor(InfiniDyskV2Executor):
            def __init__(self):
                super().__init__(owner.handler, owner.config)
                fixtures.Executor.__init__(self)

            preflight = fixtures.Executor.preflight
            receipt = fixtures.Executor.receipt

            def rehearse(self, plan, workspace, progress):
                self.calls.append("rehearse")
                names = self._quiesce(plan, progress)
                workspace["capture_source"]()
                receipt = self.receipt(plan)
                workspace["accept_import"](receipt)
                self._restore_original(plan, names)
                return {**receipt, "source_restored": True}

            def cutover(self, plan, workspace, progress):
                self.calls.append("cutover")
                self._quiesce(plan, progress)
                workspace["capture_source"]()
                candidate = Path(workspace["candidate_data"])
                with sqlite3.connect(candidate / "infinidysk.sqlite") as database:
                    database.execute("CREATE TABLE accepted_writes(value TEXT)")
                service = copy.deepcopy(plan["target"]["service"])
                candidate_service = {**service, "v2_data_dir": str(candidate)}
                native_auth(
                    candidate_service,
                    create=True,
                    uid=os.geteuid(),
                    gid=os.getegid(),
                    allow_v1_import=True,
                )
                receipt = self.receipt(plan)
                workspace["accept_import"](receipt)
                target = Path(plan["target"]["data_dir"])
                if target.exists():
                    target.rmdir()
                candidate.rename(target)
                service.update(
                    branch_enabled=False,
                    commit_sha="",
                    command=[str(Path(plan["target"]["runtime_dir"]) / "infinidysk")],
                )
                workspace["activate_config"](service)
                workspace["expose_activation"](service, receipt)
                with sqlite3.connect(target / "infinidysk.sqlite") as database:
                    database.execute(
                        "INSERT INTO accepted_writes VALUES('reserved new v2 write')"
                    )
                if owner.failure == "exposure":
                    raise RuntimeError("private payload must not escape")
                self.handler.start_prepared_process("InfiniDysk")
                self.handler.start_prepared_process("rclone InfiniDysk")
                workspace["commit_activation"](receipt)
                if owner.failure == "cancel":
                    owner.fixture.manager._cancel_requested.add(owner.job_id())
                    workspace["check_cancel"]()
                workspace["resume_consumers"](receipt)
                return {**receipt, "generation_2_started": True}

            def rollback(self, job, progress):
                self.calls.append("rollback")
                raise AssertionError(
                    "Exposed target must never automatically restore v1"
                )

        self.executor = Executor()
        self.fixture.handler, self.fixture.executor = self.handler, self.executor
        self.fixture.manager = self.fixture.new_manager()
        self.fixture.managers.append(self.fixture.manager)
        self.manager = self.fixture.manager
        self.controller = recovery.InfiniDyskV2ConsumerRecovery(self.manager)
        runtime = Path(self.config.config["infinidysk"]["v2_runtime_dir"])
        runtime.mkdir()
        binary = runtime / "infinidysk"
        binary.write_bytes(b"reserved pinned runtime fixture")
        binary.chmod(0o755)
        (runtime / "version.txt").write_text(RELEASE)
        (runtime / "commit.txt").write_text(COMMIT)
        (runtime / MARKER).write_text(
            json.dumps(
                {
                    "commit": COMMIT,
                    "release": RELEASE,
                    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                }
            )
        )
        self.config.save_config()
        self.patches = []
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        self.patch(
            "utils.infinidysk_v2_consumer_recovery.native_api_request",
            side_effect=self.health,
        )
        self.patch(
            "utils.infinidysk_v2_consumer_recovery.delivery_mount_ready",
            side_effect=self.delivery,
        )
        for name in (
            "infinidysk_namespace_recovery_pending",
            "infinidysk_postgres_recovery_pending",
        ):
            self.patch(
                "utils.infinidysk_migration_admission." + name, return_value=False
            )
        self.patch(
            "utils.infinidysk_v2_execution.delivery_mount_ready",
            side_effect=self.delivery,
        )
        self.patch(
            "utils.infinidysk_v2_consumer_recovery.socket.create_connection",
            side_effect=ConnectionRefusedError,
        )
        self.original_start = self.handler.start_process

        def prepared(name):
            if name == "Radarr" and self.failure == "consumer":
                self.handler.calls.append(("failed", name))
                return False, "reserved credentials must not escape"
            return self.original_start(name)

        self.handler.start_prepared_process = prepared

    def patch(self, target, **kwargs):
        item = patch(target, **kwargs)
        self.patches.append(item)
        return item.start()

    def job_id(self):
        return self.manager._active_id

    def health(self, *_args, **_kwargs):
        if not self.executor._running("InfiniDysk"):
            raise ConnectionError("reserved unavailable endpoint")
        return {"status": 200}

    def delivery(self, *_args, **_kwargs):
        return self.executor._running("InfiniDysk") and self.executor._running(
            "rclone InfiniDysk"
        )

    def held(self, failure="consumer"):
        self.failure = failure
        self.fixture.rehearsal()
        self.handler.calls.clear()
        _preview, request = self.fixture.preview_request()
        rehearsal = next(
            job for job in self.manager._jobs() if job["mode"] == "rehearsal"
        )
        request["rehearsal_job_id"] = rehearsal["job_id"]
        job = self.fixture.wait(self.manager.start(request))
        self.assertEqual(job["status"], "recovery_required", job)
        self.assertTrue(job["activation_exposed"])
        self.assertNotIn("rollback", self.executor.calls)
        self.assert_v2_writes()
        return job

    def assert_v2_writes(self):
        path = (
            Path(self.config.config["infinidysk"]["v2_data_dir"]) / "infinidysk.sqlite"
        )
        with sqlite3.connect(path) as database:
            self.assertEqual(
                database.execute("SELECT value FROM accepted_writes").fetchall(),
                [("reserved new v2 write",)],
            )
        self.assertEqual(self.config.config["infinidysk"]["runtime_generation"], "2")

    def action(self, job, finish=False, **extra):
        phrase = "FINISH CONSUMER RECOVERY " if finish else "RETRY CONSUMERS "
        response = self.controller.start(
            {"job_id": job["job_id"], "confirmation": phrase + job["job_id"], **extra},
            finish=finish,
        )
        self.manager._thread.join(timeout=10)
        self.assertFalse(self.manager._thread.is_alive())
        return self.manager.get_job(response["job"]["job_id"])["job"]

    @isolated_contract
    def test_committed_consumer_failure_holds_without_rollback_or_losing_writes(self):
        job = self.held()
        self.assertEqual(job["recovery_kind"], "v2_consumers")
        self.assertTrue(job["activation_committed"])
        self.assertTrue(job["consumer_recovery"]["ready"])
        self.assertEqual(
            job["consumer_recovery"]["pending"], ["Radarr", "Sonarr", "Prowlarr"]
        )
        self.assertFalse(job["can_rollback"])
        self.assertNotIn(("start", "Prowlarr"), self.handler.calls)
        self.assertNotIn("reserved credentials", json.dumps(job))

    @isolated_contract
    def test_exception_after_exposure_before_commit_preserves_target_and_retry_restarts_only_exact_set(
        self,
    ):
        job = self.held("exposure")
        self.assertEqual(job["recovery_kind"], "v2_activation")
        self.assertFalse(job["activation_committed"])
        self.assertFalse(job["consumer_recovery"]["ready"])
        self.failure = None
        self.handler.calls.clear()
        completed = self.action(job)
        self.assertEqual(completed["status"], "completed", completed)
        self.assertTrue(completed["activation_committed"])
        self.assertEqual(
            [name for kind, name in self.handler.calls if kind == "start"],
            ["InfiniDysk", "rclone InfiniDysk", "Radarr", "Sonarr", "Prowlarr"],
        )
        self.assert_v2_writes()

    @isolated_contract
    def test_late_cancel_after_commit_does_not_restore_v1_and_retry_keeps_v2_data(self):
        job = self.held("cancel")
        self.assertTrue(job["activation_committed"])
        self.failure = None
        completed = self.action(job)
        self.assertEqual(completed["status"], "completed", completed)
        self.assertNotIn("rollback", self.executor.calls)
        self.assert_v2_writes()

    @isolated_contract
    def test_controller_restart_shows_unready_then_retry_uses_prepared_start_without_installers(
        self,
    ):
        job = self.held()
        self.handler.process_names.clear()
        self.handler.processes.clear()
        self.manager._thread = None
        self.handler.startup_phase = "recovery_required"
        public = self.manager.get_job(job["job_id"])["job"]
        self.assertFalse(public["consumer_recovery"]["ready"])
        self.assertTrue(public["can_retry_consumers"])
        self.failure = None
        self.handler.calls.clear()
        with patch.object(
            self.handler,
            "start_process",
            side_effect=AssertionError("Generic setup path was used"),
        ):
            completed = self.action(public)
        self.assertEqual(completed["status"], "completed", completed)
        self.assertTrue(completed["result"]["restart_required"])
        self.assertIn("restart DUMB", completed["message"])
        self.assertEqual(self.handler.startup_phase, "recovery_required")
        restarted = self.fixture.new_manager()
        self.fixture.managers.append(restarted)
        self.assertFalse(restarted.has_recovery_pending_job())
        self.assertNotIn(
            "Stopped Whisparr", [name for _kind, name in self.handler.calls]
        )
        self.assert_v2_writes()

    @isolated_contract
    def test_finish_requires_acknowledgement_and_retains_pending_consumers(self):
        job = self.held()
        with self.assertRaisesRegex(recovery.InfiniDyskV2MigrationError, "acknowledge"):
            self.controller.start(
                {
                    "job_id": job["job_id"],
                    "confirmation": "FINISH CONSUMER RECOVERY " + job["job_id"],
                },
                finish=True,
            )
        before = list(self.handler.calls)
        completed = self.action(job, finish=True, acknowledge_pending_consumers=True)
        self.assertEqual(completed["status"], "completed", completed)
        self.assertEqual(self.handler.calls, before)
        self.assertTrue(completed["activation_committed"])
        self.assertTrue(completed["result"]["pending_consumers_acknowledged"])
        self.assertEqual(
            completed["consumer_recovery"]["pending"], ["Radarr", "Sonarr", "Prowlarr"]
        )
        self.assert_v2_writes()

    @isolated_contract
    def test_changed_data_directory_or_consumer_configuration_blocks_recovery_without_starts(
        self,
    ):
        for change in ("directory", "consumer"):
            with self.subTest(change=change):
                if change == "directory":
                    job = self.held()
                    target = Path(self.config.config["infinidysk"]["v2_data_dir"])
                    retained = target.with_name("accepted-original")
                    target.rename(retained)
                    target.mkdir()
                    before = list(self.handler.calls)
                    with self.assertRaises(recovery.ConsumerRecoveryError):
                        self.action(job)
                    self.assertEqual(self.handler.calls, before)
                    target.rmdir()
                    retained.rename(target)
                else:
                    self.config.config["radarr"]["command"] = ["unreviewed"]
                    before = list(self.handler.calls)
                    with self.assertRaises(recovery.ConsumerRecoveryError):
                        self.action(job)
                    self.assertEqual(self.handler.calls, before)
        self.assert_v2_writes()

    @isolated_contract
    def test_provider_switch_or_pending_copy_journal_blocks_recovery_without_losing_writes(
        self,
    ):
        job = self.held()
        journal = (
            Path(self.config.config["infinidysk"]["v2_data_dir"]) / "database.toml"
        )
        for contents in (
            'url = "postgresql://reserved:private-value@example.invalid/changed"\n',
            '[migration]\nstate = "copying"\ntarget = "postgresql://reserved:private-value@example.invalid/pending"\n',
        ):
            with self.subTest(contents=contents):
                journal.write_text(contents)
                before = list(self.handler.calls)
                with self.assertRaises(recovery.ConsumerRecoveryError) as caught:
                    self.action(job)
                self.assertNotIn("private-value", str(caught.exception))
                self.assertEqual(self.handler.calls, before)
                self.assert_v2_writes()
        journal.unlink()

    @isolated_contract
    def test_healthy_exposed_target_can_finish_with_committed_boundary(self):
        job = self.held("exposure")
        self.failure = None
        self.handler.start_prepared_process("InfiniDysk")
        self.handler.start_prepared_process("rclone InfiniDysk")
        private = self.manager._read(self.manager._job_path(job["job_id"]))
        self.controller.hold(private, recovery.ConsumerRecoveryError("target_unready"))
        job = self.manager.get_job(job["job_id"])["job"]
        self.assertFalse(job["activation_committed"])
        self.assertTrue(job["can_finish_consumer_recovery"])
        completed = self.action(job, finish=True, acknowledge_pending_consumers=True)
        self.assertEqual(completed["status"], "completed", completed)
        self.assertTrue(completed["activation_committed"])
        self.assert_v2_writes()

    @isolated_contract
    def test_worker_ownership_change_prevents_later_consumer_starts_or_checkpoint_overwrite(
        self,
    ):
        job = self.held()
        self.failure = None
        original = self.handler.start_prepared_process
        foreign_owner = "f" * 32

        def changed(name):
            result = original(name)
            if name == "Radarr":
                self.manager._active_id = foreign_owner
            return result

        before = list(self.handler.calls)
        with patch.object(self.handler, "start_prepared_process", changed):
            response = self.controller.start(
                {
                    "job_id": job["job_id"],
                    "confirmation": "RETRY CONSUMERS " + job["job_id"],
                }
            )
            self.manager._thread.join(timeout=10)
            self.assertFalse(self.manager._thread.is_alive())
        self.assertEqual(self.manager._active_id, foreign_owner)
        self.assertEqual(self.handler.calls[len(before) :], [("start", "Radarr")])
        private = self.manager._read(self.manager._job_path(response["job"]["job_id"]))
        self.assertEqual(private["consumer_recovery"]["resumed"], [])
        self.assertEqual(
            private["consumer_recovery"]["pending"], ["Radarr", "Sonarr", "Prowlarr"]
        )
        self.assert_v2_writes()
        self.manager._active_id = None

    @isolated_contract
    def test_unregistered_error_response_or_occupied_mount_blocks_duplicate_starts(
        self,
    ):
        job = self.held("exposure")
        self.failure = None
        before = list(self.handler.calls)
        for status in (401, 503):
            with (
                self.subTest(status=status),
                patch.object(
                    recovery, "native_api_request", return_value={"status": status}
                ),
            ):
                held = self.action(job)
                self.assertEqual(held["status"], "recovery_required")
                self.assertIn("unregistered", held["error"])
                self.assertEqual(self.handler.calls, before)
        mount = Path(self.config.config["infinidysk"]["mount_path"])
        with patch.object(
            recovery,
            "mount_records",
            return_value=[
                {
                    "path": str(mount / "orphan-child"),
                    "filesystem": "fuse.rclone",
                    "source": "reserved-orphan",
                }
            ],
        ):
            held = self.action(job)
        self.assertEqual(held["status"], "recovery_required")
        self.assertIn("unregistered", held["error"])
        self.assertEqual(self.handler.calls, before)
        self.assert_v2_writes()

    @isolated_contract
    def test_inconclusive_listener_ownership_blocks_restart_without_touching_data(self):
        job = self.held("exposure")
        self.failure = None
        before = list(self.handler.calls)
        with patch.object(
            recovery.socket,
            "create_connection",
            side_effect=TimeoutError("private value"),
        ):
            held = self.action(job)
        self.assertEqual(held["status"], "recovery_required")
        self.assertIn("unregistered", held["error"])
        self.assertNotIn("private value", held["error"])
        self.assertEqual(self.handler.calls, before)
        self.assert_v2_writes()

    @isolated_contract
    def test_same_inode_binary_and_matching_marker_rewrite_blocks_recovery(self):
        job = self.held()
        runtime = Path(self.config.config["infinidysk"]["v2_runtime_dir"])
        binary = runtime / "infinidysk"
        marker = runtime / MARKER
        original_inode = binary.stat().st_ino
        binary.write_bytes(b"unreviewed runtime replacement")
        provenance = json.loads(marker.read_text())
        provenance["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
        marker.write_text(json.dumps(provenance))
        self.assertEqual(binary.stat().st_ino, original_inode)
        before = list(self.handler.calls)
        with self.assertRaises(recovery.ConsumerRecoveryError):
            self.action(job)
        self.assertEqual(self.handler.calls, before)
        self.assert_v2_writes()

    @isolated_contract
    def test_actual_route_retry_and_finish_share_one_admission_reservation(self):
        from api.routers import infinidysk_v2 as router

        job = self.held()
        with (
            patch.object(router, "_manager", return_value=self.manager),
            patch.object(router, "get_project_version", return_value="2.24.0"),
        ):
            response = router._invoke(
                "major",
                "retry_consumers",
                {
                    "job_id": job["job_id"],
                    "confirmation": "RETRY CONSUMERS " + job["job_id"],
                },
                self.handler,
                self.fixture.updater,
                mutation=True,
            )
            self.manager._thread.join(timeout=10)
            self.assertFalse(self.manager._thread.is_alive())
            held = self.manager.get_job(response["job"]["job_id"])["job"]
            self.assertEqual(held["status"], "recovery_required", held)
            response = router._invoke(
                "major",
                "finish_consumer_recovery",
                {
                    "job_id": job["job_id"],
                    "confirmation": "FINISH CONSUMER RECOVERY " + job["job_id"],
                    "acknowledge_pending_consumers": True,
                },
                self.handler,
                self.fixture.updater,
                mutation=True,
            )
            self.manager._thread.join(timeout=10)
            self.assertFalse(self.manager._thread.is_alive())
        completed = self.manager.get_job(response["job"]["job_id"])["job"]
        self.assertEqual(completed["status"], "completed", completed)
        self.assertTrue(completed["result"]["pending_consumers_acknowledged"])
        self.assert_v2_writes()

    @isolated_contract
    def test_changed_worker_token_does_not_overwrite_a_later_same_job_checkpoint(self):
        job = self.held()
        self.failure = None
        original = self.handler.start_prepared_process

        def changed(name):
            result = original(name)
            if name == "Radarr":
                saved = self.manager._read(self.manager._job_path(job["job_id"]))
                saved["consumer_recovery"]["worker_token"] = "newer-worker-checkpoint"
                self.manager._write(self.manager._job_path(job["job_id"]), saved)
            return result

        with patch.object(self.handler, "start_prepared_process", changed):
            response = self.controller.start(
                {
                    "job_id": job["job_id"],
                    "confirmation": "RETRY CONSUMERS " + job["job_id"],
                }
            )
            self.manager._thread.join(timeout=10)
            self.assertFalse(self.manager._thread.is_alive())
        saved = self.manager._read(self.manager._job_path(response["job"]["job_id"]))
        self.assertEqual(
            saved["consumer_recovery"]["worker_token"], "newer-worker-checkpoint"
        )
        self.assertEqual(saved["consumer_recovery"]["resumed"], [])
        self.assertEqual(
            saved["consumer_recovery"]["pending"], ["Radarr", "Sonarr", "Prowlarr"]
        )
        self.assertIsNone(self.manager._active_id)
        self.assert_v2_writes()

    @isolated_contract
    def test_pending_retry_stops_at_first_failure_and_never_duplicates_live_owners(
        self,
    ):
        job = self.held()
        before = [name for kind, name in self.handler.calls if kind == "start"]
        again = self.action(job)
        self.assertEqual(again["status"], "recovery_required", again)
        self.assertEqual(
            [name for kind, name in self.handler.calls if kind == "start"], before
        )
        self.assertEqual(
            again["consumer_recovery"]["failed"][0]["reason_code"],
            "consumer_start_failed",
        )
        self.assert_v2_writes()


if __name__ == "__main__":
    unittest.main()
