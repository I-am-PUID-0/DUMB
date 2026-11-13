import copy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from utils.infinidysk_v2_subprocess import MigrationProcessCleanupError

from utils.infinidysk_v2_execution import (
    ContainedImportError,
    InfiniDyskV2Executor,
    NativeCandidate,
    verify_import_inventory,
)


class Config:
    def __init__(self, service):
        self.config = {
            "puid": os.geteuid() or 1000,
            "pgid": os.getegid() or 1000,
            "infinidysk": service,
            "sonarr": {
                "instances": {"fixture": {"enabled": True, "process_name": "Sonarr"}}
            },
            "rclone": {
                "instances": {
                    "legacy": {
                        "enabled": True,
                        "key_type": "infinidysk",
                        "process_name": "rclone InfiniDysk",
                        "mount_dir": "/mnt",
                        "mount_name": "legacy",
                    }
                }
            },
        }

    def get(self, key, section=None):
        return (self.config.get(section, {}) if section else self.config).get(key)


class Handler:
    def __init__(self):
        self.process_names = {
            name: SimpleNamespace(poll=lambda: None)
            for name in ("InfiniDysk", "rclone InfiniDysk", "Sonarr")
        }
        self.calls = []
        self.shutting_down = False

    def start_prepared_process(self, name):
        return self.start_process(name)

    def _prefixed_name(self, name):
        return name

    def stop_process(self, name):
        self.calls.append(("stop", name))
        self.process_names.pop(name, None)

    def start_process(self, name):
        self.calls.append(("start", name))
        self.process_names[name] = SimpleNamespace(poll=lambda: None)
        return True, None


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        target = self.root / "target"
        self.service = {
            "runtime_generation": "2",
            "process_name": "InfiniDysk",
            "mount_backend": "rclone",
            "mount_path": "/mnt/legacy",
            "v2_data_dir": str(target),
            "env": {"TZ": "UTC"},
        }
        self.config = Config(self.service)
        self.handler = Handler()
        self.executor = InfiniDyskV2Executor(self.handler, self.config)
        self.plan = {
            "mode": "migrate",
            "plan_fingerprint": "a" * 64,
            "source": {
                "process_name": "InfiniDysk",
                "service": {
                    "runtime_generation": "1",
                    "process_name": "InfiniDysk",
                    "env": {"TZ": "UTC"},
                },
                "process_identity": {"pid": 1},
                "provider": "sqlite",
                "timezone": "UTC",
            },
            "target": {
                "service": self.service,
                "data_dir": str(target),
                "mount_path": "/mnt/legacy",
            },
            "inventory": {
                "mounts": [
                    {
                        "path": "/mnt/legacy",
                        "process_name": "rclone InfiniDysk",
                        "mounted": False,
                    }
                ],
                "consumers": [
                    {"process_name": "Sonarr", "process_identity": {"pid": 2}}
                ],
            },
        }
        self.workspace = {
            "candidate_data": str(self.root / (".target.dumb-v2-" + "b" * 32)),
            "root": str(self.root / "private" / ("b" * 32)),
        }
        Path(self.workspace["root"]).mkdir(parents=True)
        self.snapshots = []
        self.oidc = False
        self.download_key = "retained-fixture-download-key"
        with sqlite3.connect(self.root / "source.sqlite") as database:
            database.execute(
                "CREATE TABLE ConfigItems(ConfigName TEXT,ConfigValue TEXT)"
            )
            database.execute(
                "INSERT INTO ConfigItems VALUES('api.key',?)", (self.download_key,)
            )

        def capture(postgres_snapshot=None):
            self.snapshots.append(postgres_snapshot)
            candidate = Path(self.workspace["candidate_data"])
            candidate.mkdir()
            (candidate / "db-contract.json").write_text(
                json.dumps({"provider": "postgres" if postgres_snapshot else "sqlite"})
            )
            return {
                "source_schema": {"oidc_configured": self.oidc},
                "source_database": str(self.root / "source.sqlite"),
                "candidate_identity": {
                    "device": candidate.stat().st_dev,
                    "inode": candidate.stat().st_ino,
                    "mode": stat.S_IFDIR,
                },
            }

        self.workspace.update(
            capture_source=capture,
            accept_import=Mock(),
            activate_config=Mock(),
            expose_activation=Mock(),
            commit_activation=Mock(),
            resume_consumers=Mock(),
        )
        self.workspace["resume_consumers"].side_effect = self.resume_consumers
        self.progress = Mock()
        self.patches = []
        for name, value in (
            ("mount_records", []),
            ("install_v2_runtime", (True, None)),
            ("native_auth", {}),
            ("configure_v2_runtime", (True, None)),
            ("native_api_request", {"status": 200}),
            ("delivery_mount_ready", True),
            (
                "verify_import_inventory",
                {"files": 0, "history": 0, "skipped_files": 0, "failed_files": 0},
            ),
        ):
            patcher = patch("utils.infinidysk_v2_execution." + name, return_value=value)
            self.patches.append(patcher)
            patcher.start()
        self.candidate = Mock()
        self.candidate.stopped = True
        self.candidate.__enter__ = Mock(return_value=self.candidate)
        self.candidate.__exit__ = Mock(return_value=False)
        self.candidate.wait_import.return_value = (
            "Imported 0 directories, 0 files, 0 NZBs, 0 history rows, 0 queued jobs"
        )

        def imported(*unused, **kwargs):
            native = Path(self.workspace["candidate_data"]) / "infinidysk.sqlite"
            with sqlite3.connect(native) as database:
                database.execute(
                    "CREATE TABLE IF NOT EXISTS config(key TEXT,value TEXT)"
                )
                database.execute("DELETE FROM config WHERE key='sab'")
                database.execute(
                    "INSERT INTO config VALUES('sab',?)",
                    (json.dumps({"api_key": self.download_key}),),
                )
            return self.candidate.wait_import.return_value

        self.candidate.wait_import.side_effect = imported
        patcher = patch(
            "utils.infinidysk_v2_execution.NativeCandidate", return_value=self.candidate
        )
        self.patches.append(patcher)
        self.native = patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    def resume_consumers(self, receipt):
        # This executor fixture supplies already-accepted checkpoints; use the
        # real retained-scope planner and resume loop at its callback boundary.
        # Complete receipt/identity/worker checks have real-manager coverage in
        # test_infinidysk_v2_consumer_recovery.
        from utils.infinidysk_v2_consumer_recovery import InfiniDyskV2ConsumerRecovery

        controller = InfiniDyskV2ConsumerRecovery(
            SimpleNamespace(
                executor=self.executor,
                _config=lambda: self.config.config,
            )
        )
        names = self.progress.call_args_list[0].kwargs["original_running_names"]
        job = {
            "plan": self.plan,
            "activation_exposed": True,
            "activation_committed": True,
            "consumer_recovery": {"schema": 1},
        }
        consumers, _bindings = controller._scope(job, names)
        with (
            patch.object(controller, "_owner"),
            patch.object(controller, "_save"),
            patch.object(
                controller, "_validate", return_value=(self.service, receipt, consumers)
            ),
            patch.object(controller, "_ready", return_value=True),
        ):
            return controller.resume(job)

    def test_cutover_starts_delivery_before_any_consumer(self):
        observed = []

        def delivery(service, instances):
            observed.append(list(self.handler.calls))
            return True

        with patch(
            "utils.infinidysk_v2_execution.delivery_mount_ready", side_effect=delivery
        ):
            result = self.executor.cutover(self.plan, self.workspace, self.progress)
        self.assertTrue(result["generation_2_started"])
        self.workspace["accept_import"].assert_called_once()
        self.workspace["activate_config"].assert_called_once()
        starts = [name for action, name in self.handler.calls if action == "start"]
        self.assertEqual(starts, ["InfiniDysk", "rclone InfiniDysk", "Sonarr"])
        self.assertTrue(observed)
        self.assertNotIn(("start", "Sonarr"), observed[-1])

    def test_activation_journal_precedes_provider_and_commit_precedes_consumers(self):
        phases = []

        def expose(service, receipt):
            self.assertNotIn(("start", "InfiniDysk"), self.handler.calls)
            self.assertTrue(Path(self.plan["target"]["data_dir"]).exists())
            phases.append("exposed")

        def commit(receipt):
            self.assertEqual(phases, ["exposed"])
            self.assertIn(("start", "InfiniDysk"), self.handler.calls)
            self.assertIn(("start", "rclone InfiniDysk"), self.handler.calls)
            self.assertNotIn(("start", "Sonarr"), self.handler.calls)
            phases.append("committed")

        def resume(receipt):
            self.assertEqual(phases, ["exposed", "committed"])
            self.executor._start_retained("Sonarr", "consumer did not start")

        self.workspace["expose_activation"].side_effect = expose
        self.workspace["commit_activation"].side_effect = commit
        self.workspace["resume_consumers"].side_effect = resume
        self.assertTrue(
            self.executor.cutover(self.plan, self.workspace, self.progress)[
                "generation_2_started"
            ]
        )

    def test_failed_exposure_journal_never_starts_provider(self):
        self.workspace["expose_activation"].side_effect = OSError(
            "private journal unavailable"
        )
        with self.assertRaisesRegex(OSError, "journal"):
            self.executor.cutover(self.plan, self.workspace, self.progress)
        self.assertFalse(any(action == "start" for action, _ in self.handler.calls))
        self.workspace["commit_activation"].assert_not_called()
        self.workspace["resume_consumers"].assert_not_called()

    def test_mount_preparation_failure_exposes_only_safe_authored_reason(self):
        from utils.infinidysk_v2_fuse import FUSE_POLICY_REQUIRED

        with patch(
            "utils.infinidysk_v2_execution.configure_v2_runtime",
            return_value=(False, FUSE_POLICY_REQUIRED),
        ):
            with self.assertRaisesRegex(ContainedImportError, "user_allow_other"):
                self.executor.cutover(self.plan, self.workspace, self.progress)
        self.workspace["expose_activation"].assert_not_called()
        self.assertFalse(any(action == "start" for action, _ in self.handler.calls))

    def test_inventory_receives_only_verified_staged_blob_alias(self):
        capture = self.workspace["capture_source"]

        def aliased(**kwargs):
            result = capture(**kwargs)
            candidate = Path(self.workspace["candidate_data"])
            (candidate / "payloads").mkdir()
            (candidate / "blobs").symlink_to("payloads", target_is_directory=True)
            return result

        self.workspace["capture_source"] = aliased
        with patch(
            "utils.infinidysk_v2_execution.verify_import_inventory",
            return_value={
                "files": 0,
                "history": 0,
                "skipped_files": 0,
                "failed_files": 0,
            },
        ) as verify:
            self.executor.cutover(self.plan, self.workspace, self.progress)
        self.assertEqual(
            verify.call_args.kwargs["source_blob_root"],
            Path(self.workspace["candidate_data"]) / "payloads",
        )

    def test_escaping_staged_blob_alias_blocks_before_activation(self):
        capture = self.workspace["capture_source"]

        def aliased(**kwargs):
            result = capture(**kwargs)
            (Path(self.workspace["candidate_data"]) / "blobs").symlink_to(
                self.root, target_is_directory=True
            )
            return result

        self.workspace["capture_source"] = aliased
        with self.assertRaisesRegex(ContainedImportError, "alias"):
            self.executor.cutover(self.plan, self.workspace, self.progress)
        self.workspace["expose_activation"].assert_not_called()

    def test_validated_rehearsal_discards_only_candidate_after_restoration(self):
        outside = self.root / "outside.txt"
        outside.write_text("protected")
        receipt = Path(self.workspace["root"]) / "receipt.json"
        receipt.write_text("retained")
        capture = self.workspace["capture_source"]

        def populated(**kwargs):
            result = capture(**kwargs)
            candidate = Path(self.workspace["candidate_data"])
            (candidate / "nested").mkdir()
            (candidate / "nested" / "payload").write_bytes(b"disposable")
            (candidate / "outside-link").symlink_to(outside)
            return result

        self.workspace["capture_source"] = populated
        discard = self.executor._discard_rehearsal_candidate

        def checked(*args):
            self.candidate.__exit__.assert_called_once()
            self.workspace["accept_import"].assert_called_once()
            self.assertEqual(self.handler.calls[-1], ("start", "Sonarr"))
            self.assertIn(
                "source_restored",
                [call.args[0] for call in self.progress.call_args_list],
            )
            return discard(*args)

        with patch.object(
            self.executor, "_discard_rehearsal_candidate", side_effect=checked
        ):
            result = self.executor.rehearse(self.plan, self.workspace, self.progress)
        self.assertTrue(result["candidate_discarded"])
        self.assertTrue(result["source_restored"])
        self.assertFalse(Path(self.workspace["candidate_data"]).exists())
        self.assertEqual(outside.read_text(), "protected")
        self.assertEqual(receipt.read_text(), "retained")
        self.assertFalse(Path(self.plan["target"]["data_dir"]).exists())

    def test_disposal_refuses_unowned_or_promoted_root_without_mutation(self):
        snapshot = self.workspace["capture_source"]()
        candidate = Path(self.workspace["candidate_data"])
        with self.assertRaisesRegex(ContainedImportError, "ownership"):
            self.executor._discard_rehearsal_candidate(self.plan, self.workspace, {})
        self.assertTrue((candidate / "db-contract.json").exists())
        target = Path(self.plan["target"]["data_dir"])
        candidate.rename(target)
        workspace = {**self.workspace, "candidate_data": str(target)}
        with self.assertRaisesRegex(ContainedImportError, "ownership"):
            self.executor._discard_rehearsal_candidate(self.plan, workspace, snapshot)
        self.assertTrue((target / "db-contract.json").exists())

    def test_disposal_refuses_root_replacement(self):
        snapshot = self.workspace["capture_source"]()
        candidate = Path(self.workspace["candidate_data"])
        original = self.root / "retained-original-candidate"
        candidate.rename(original)
        candidate.mkdir()
        marker = candidate / "foreign.txt"
        marker.write_text("foreign")
        with self.assertRaisesRegex(ContainedImportError, "root changed"):
            self.executor._discard_rehearsal_candidate(
                self.plan, self.workspace, snapshot
            )
        self.assertEqual(marker.read_text(), "foreign")
        self.assertTrue((original / "db-contract.json").exists())

    def test_disposal_refuses_descendant_mount_before_removing_any_file(self):
        snapshot = self.workspace["capture_source"]()
        candidate = Path(self.workspace["candidate_data"])
        with (
            patch(
                "utils.infinidysk_v2_execution.mount_records",
                return_value=[{"path": str(candidate / "mounted-file")}],
            ),
            self.assertRaisesRegex(ContainedImportError, "mount"),
        ):
            self.executor._discard_rehearsal_candidate(
                self.plan, self.workspace, snapshot
            )
        self.assertTrue((candidate / "db-contract.json").exists())

    def test_disposal_refuses_file_bind_mount_even_on_the_same_device(self):
        from utils.infinidysk_v2_rehearsal_cleanup import _candidate_mount_id

        snapshot = self.workspace["capture_source"]()
        candidate = Path(self.workspace["candidate_data"])
        inode = (candidate / "db-contract.json").stat().st_ino

        def mount(descriptor):
            return (
                "foreign-file-mount"
                if os.fstat(descriptor).st_ino == inode
                else _candidate_mount_id(descriptor)
            )

        with (
            patch(
                "utils.infinidysk_v2_rehearsal_cleanup._candidate_mount_id",
                side_effect=mount,
            ),
            self.assertRaisesRegex(ContainedImportError, "mounted"),
        ):
            self.executor._discard_rehearsal_candidate(
                self.plan, self.workspace, snapshot
            )
        self.assertTrue((candidate / "db-contract.json").exists())

    def test_disposal_rechecks_each_record_after_complete_preflight(self):
        snapshot = self.workspace["capture_source"]()
        candidate = Path(self.workspace["candidate_data"])
        marker = candidate / "db-contract.json"
        saved = self.root / "original-entry"
        calls = []

        def replace_after_scan(path):
            calls.append(path)
            if len(calls) == 2:
                marker.rename(saved)
                marker.write_text("foreign replacement")

        with (
            patch.object(
                self.executor, "_assert_unmounted", side_effect=replace_after_scan
            ),
            self.assertRaisesRegex(ContainedImportError, "changed"),
        ):
            self.executor._discard_rehearsal_candidate(
                self.plan, self.workspace, snapshot
            )
        self.assertEqual(marker.read_text(), "foreign replacement")
        self.assertTrue(saved.exists())

    def test_failed_acceptance_or_restore_never_discards_candidate(self):
        for failure in ("accept", "restore", "native"):
            with self.subTest(failure=failure):
                if failure == "accept":
                    self.workspace["accept_import"].side_effect = ValueError(
                        "accept failed"
                    )
                    patcher = patch.object(
                        self.executor,
                        "_restore_original",
                        wraps=self.executor._restore_original,
                    )
                elif failure == "restore":
                    self.workspace["accept_import"].side_effect = None
                    patcher = patch.object(
                        self.executor,
                        "_restore_original",
                        side_effect=ValueError("restore failed"),
                    )
                else:
                    self.candidate.stopped = False
                    patcher = patch.object(
                        self.executor,
                        "_restore_original",
                        wraps=self.executor._restore_original,
                    )
                with (
                    patcher,
                    patch.object(
                        self.executor, "_discard_rehearsal_candidate"
                    ) as discard,
                ):
                    with self.assertRaises((ValueError, ContainedImportError)):
                        self.executor.rehearse(self.plan, self.workspace, self.progress)
                    discard.assert_not_called()
                candidate = Path(self.workspace["candidate_data"])
                self.assertTrue(candidate.exists())
                shutil.rmtree(candidate)

    def test_missing_delivery_mount_keeps_consumers_stopped(self):
        with patch.object(
            self.executor,
            "_wait_delivery",
            side_effect=ContainedImportError("delivery absent"),
        ):
            with self.assertRaisesRegex(ContainedImportError, "delivery absent"):
                self.executor.cutover(self.plan, self.workspace, self.progress)
        self.assertNotIn(("start", "Sonarr"), self.handler.calls)

    def test_oidc_source_is_blocked_without_claiming_policy_verification(self):
        self.oidc = True
        with self.assertRaisesRegex(ContainedImportError, "OIDC"):
            self.executor.rehearse(self.plan, self.workspace, self.progress)
        self.native.assert_not_called()
        self.workspace["accept_import"].assert_not_called()

    def test_pg_snapshot_is_restored_then_candidate_only_marker_is_sqlite(self):
        self.plan["source"]["provider"] = "postgres"
        receipt = {"verified_restore": True}

        def inspect_candidate(*args, **kwargs):
            self.assertEqual(
                json.loads((Path(args[1]) / "db-contract.json").read_text())[
                    "provider"
                ],
                "sqlite",
            )
            self.assertEqual(args[6]["DATABASE_PROVIDER"], "sqlite")
            self.assertNotIn("DATABASE_CONNECTION_STRING", args[6])
            return self.candidate

        self.native.side_effect = inspect_candidate
        with (
            patch(
                "utils.infinidysk_v2_execution.create_postgres_snapshot",
                return_value=receipt,
            ) as snapshot,
            patch(
                "utils.infinidysk_v2_execution.postgres_tools",
                return_value=Path("/private/tools"),
            ),
        ):
            result = self.executor.rehearse(self.plan, self.workspace, self.progress)
        self.assertEqual(self.snapshots, [receipt])
        self.assertTrue(result["source_restored"])
        self.assertTrue(callable(snapshot.call_args.kwargs["verify_restore"]))

    def test_failed_pg_backup_never_launches_native(self):
        self.plan["source"]["provider"] = "postgres"
        with (
            patch(
                "utils.infinidysk_v2_execution.postgres_tools",
                return_value=Path("/tools"),
            ),
            patch(
                "utils.infinidysk_v2_execution.create_postgres_snapshot",
                side_effect=ValueError("backup refused"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "backup refused"):
                self.executor.rehearse(self.plan, self.workspace, self.progress)
        self.assertFalse(self.snapshots)
        self.native.assert_not_called()

    def test_rollback_restores_all_actual_previous_owners_even_if_mount_was_not_ready(
        self,
    ):
        restored = []
        job = {
            "plan": self.plan,
            "execution": {
                "original_running_names": ["InfiniDysk", "rclone InfiniDysk", "Sonarr"]
            },
            "restore_config": lambda: restored.append(True),
        }
        result = self.executor.rollback(job, self.progress)
        self.assertTrue(result["source_restored"])
        self.assertTrue(restored)
        self.assertEqual(
            [n for a, n in self.handler.calls if a == "start"],
            ["InfiniDysk", "rclone InfiniDysk", "Sonarr"],
        )

    def test_rollback_will_not_restore_config_with_descendant_mount_still_active(self):
        restore = Mock()
        with patch(
            "utils.infinidysk_v2_execution.mount_records",
            return_value=[{"path": "/mnt/legacy/nested"}],
        ):
            with self.assertRaisesRegex(ContainedImportError, "mount"):
                self.executor.rollback(
                    {"plan": self.plan, "restore_config": restore}, self.progress
                )
        restore.assert_not_called()
        self.assertFalse(any(action == "start" for action, name in self.handler.calls))

    def test_quiescence_persists_running_owner_checkpoint_before_stopping(self):
        self.executor.rehearse(self.plan, self.workspace, self.progress)
        call = self.progress.call_args_list[0]
        self.assertEqual(
            call.kwargs["original_running_names"],
            ["InfiniDysk", "rclone InfiniDysk", "Sonarr"],
        )

    def test_configured_root_identity_is_refused_before_kernel_probe_or_quiescence(
        self,
    ):
        for field in ("puid", "pgid"):
            with self.subTest(field=field):
                original = self.config.config[field]
                self.config.config[field] = 0
                with (
                    patch("utils.infinidysk_v2_execution.os.geteuid", return_value=0),
                    patch("utils.infinidysk_v2_execution.ctypes.CDLL") as libc,
                    patch("utils.infinidysk_v2_execution.subprocess.run") as spawning,
                ):
                    libc.return_value.syscall.return_value = 4
                    result = self.executor.preflight(self.plan)
                self.assertTrue(
                    any("non-root" in value for value in result["blockers"])
                )
                spawning.assert_not_called()
                with self.assertRaisesRegex(ContainedImportError, "non-root"):
                    self.executor.rehearse(self.plan, self.workspace, self.progress)
                self.assertEqual(self.handler.calls, [])
                self.config.config[field] = original

    def test_kernel_preflight_failure_is_visible_before_quiescence(self):
        with (
            patch("utils.infinidysk_v2_execution.os.geteuid", return_value=0),
            patch(
                "utils.infinidysk_v2_execution.shutil.which",
                return_value="/usr/bin/tool",
            ),
            patch("utils.infinidysk_v2_execution.ctypes.CDLL") as libc,
            patch(
                "utils.infinidysk_v2_execution.subprocess.run",
                side_effect=OSError("unshare denied"),
            ),
        ):
            libc.return_value.syscall.return_value = 4
            result = self.executor.preflight(self.plan)
        self.assertTrue(any("probe failed" in x for x in result["blockers"]))
        self.assertEqual(self.handler.calls, [])


class NativeShutdownTests(unittest.TestCase):
    def candidate(self, root):
        value = object.__new__(NativeCandidate)
        value.process = Mock(pid=987654, poll=Mock(return_value=0))
        value.output = Mock()
        value.payload_path = root / "payload.json"
        value.payload_path.write_text("private")
        value.stopped = False
        return value

    def test_shutdown_waits_for_descendant_writers_after_reaping_main_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            candidate = self.candidate(Path(temporary))
            with (
                patch("utils.infinidysk_v2_execution.os.killpg"),
                patch("utils.infinidysk_v2_execution.time.sleep"),
                patch(
                    "utils.infinidysk_v2_execution._native_group_has_live_members",
                    side_effect=[True, False],
                ) as live,
            ):
                candidate.__exit__()
            candidate.process.wait.assert_called_once_with(timeout=10)
            self.assertEqual(live.call_count, 2)
            self.assertTrue(candidate.stopped)
            candidate.output.close.assert_called_once()
            self.assertFalse(candidate.payload_path.exists())

    def test_unproven_native_writer_shutdown_refuses_disposal_and_closes_private_files(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            candidate = self.candidate(Path(temporary))
            with (
                patch("utils.infinidysk_v2_execution.os.killpg"),
                patch(
                    "utils.infinidysk_v2_execution._native_group_has_live_members",
                    return_value=True,
                ),
                patch(
                    "utils.infinidysk_v2_execution.time.monotonic", side_effect=[0, 4]
                ),
            ):
                with self.assertRaisesRegex(
                    MigrationProcessCleanupError, "live writers"
                ) as caught:
                    candidate.__exit__()
            self.assertFalse(caught.exception.cleanup_complete)
            self.assertEqual(
                caught.exception.cleanup_evidence["process_group"],
                candidate.process.pid,
            )
            self.assertFalse(candidate.stopped)
            candidate.output.close.assert_called_once()
            self.assertFalse(candidate.payload_path.exists())

    def test_process_group_check_ignores_zombies_but_detects_running_members(self):
        from utils.infinidysk_v2_execution import _native_group_has_live_members

        with tempfile.TemporaryDirectory() as temporary:
            member = Path(temporary) / "12345"
            member.mkdir()
            for state, expected in (("Z", False), ("S", True)):
                (member / "stat").write_bytes(
                    b"12345 (owned-\xff worker) " + state.encode() + b" 1 987654 1 0"
                )
                with (
                    patch("utils.infinidysk_v2_execution.os.killpg"),
                    patch("utils.infinidysk_v2_execution.os.scandir") as scan,
                ):
                    scan.return_value.__enter__.return_value = iter(
                        [SimpleNamespace(name=member.name, path=str(member))]
                    )
                    self.assertEqual(_native_group_has_live_members(987654), expected)


class ImportInventoryTests(unittest.TestCase):
    def test_real_source_and_target_identity_comparison_rejects_changed_history_or_names(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "old.sqlite"
            target = root / "new.sqlite"
            identifier = "00000000-0000-0000-0000-0000000000ab"
            with sqlite3.connect(source) as db:
                db.executescript(
                    "CREATE TABLE DavItems(Id TEXT,Name TEXT,Type INTEGER,Path TEXT);CREATE TABLE HistoryItems(Id TEXT, DownloadStatus INTEGER);"
                )
                db.execute(
                    "INSERT INTO DavItems VALUES(?,?,2,'/content/movie')",
                    (identifier, "movie.mkv"),
                )
                db.execute("INSERT INTO HistoryItems VALUES(?,1)", (identifier,))
            with sqlite3.connect(target) as db:
                db.executescript(
                    "CREATE TABLE items(id BLOB,name TEXT,kind INTEGER);CREATE TABLE history(id BLOB);"
                )
                value = bytes.fromhex(identifier.replace("-", ""))
                db.execute("INSERT INTO items VALUES(?,?,?)", (value, "movie.mkv", 2))
                db.execute("INSERT INTO history VALUES(?)", (value,))
            report = (
                "Imported 0 directories, 1 files, 0 NZBs, 1 history rows, 0 queued jobs"
            )
            result = verify_import_inventory(source, target, report)
            self.assertEqual((result["files"], result["history"]), (1, 1))
            with sqlite3.connect(target) as db:
                db.execute("UPDATE items SET name='changed.mkv'")
            with self.assertRaisesRegex(ContainedImportError, "names"):
                verify_import_inventory(source, target, report)
            with sqlite3.connect(target) as db:
                db.execute("UPDATE items SET name='movie.mkv'")
                db.execute("DELETE FROM history")
            with self.assertRaisesRegex(ContainedImportError, "history"):
                verify_import_inventory(source, target, report)


if __name__ == "__main__":
    unittest.main()


class NativeImportWatchdogTests(unittest.TestCase):
    def candidate(self, root):
        value = object.__new__(NativeCandidate)
        value.timeout = 1800
        value.check_cancel = Mock()
        value.record_report = Mock()
        value.process = SimpleNamespace(pid=987654, poll=Mock(return_value=None))
        proof = root / "proof.json"
        proof.write_text(
            json.dumps(
                {
                    "pid": 987654,
                    "landlock_abi": 3,
                    "external_network": False,
                    "network_namespace": "native",
                }
            )
        )
        value.payload = {"proof_path": str(proof)}
        return value

    def run_wait(self, candidate, states, times):
        replies = [
            (
                state
                if state is None or isinstance(state, BaseException)
                else {"status": 200, "body": state}
            )
            for state in states
        ]
        replies.append({"status": 200, "body": {"report": "verified native report"}})
        if getattr(candidate, "nntp_bridge", None) is not None:
            replies.append({"status": 200, "body": {"profiles": []}})
        candidate.probe = Mock(side_effect=replies)
        progress = Mock()
        with (
            patch("utils.infinidysk_v2_execution.time.monotonic", side_effect=times),
            patch("utils.infinidysk_v2_execution.time.sleep"),
            patch(
                "utils.infinidysk_v2_execution.os.readlink",
                side_effect=lambda path: "native" if "987654" in path else "controller",
            ),
        ):
            report = candidate.wait_import(True, progress)
        return report, progress

    def test_advancing_import_is_allowed_beyond_previous_elapsed_cutoffs(self):
        with tempfile.TemporaryDirectory() as temporary:
            candidate = self.candidate(Path(temporary))
            report, progress = self.run_wait(
                candidate,
                [
                    {
                        "state": "importing",
                        "progress": {"stage": "files", "processed": 1, "total": 3},
                    },
                    {
                        "state": "importing",
                        "progress": {"stage": "files", "processed": 2, "total": 3},
                    },
                    {
                        "state": "done",
                        "progress": {"stage": "done", "processed": 3, "total": 3},
                    },
                ],
                [0, 0, 7201, 14401],
            )
            self.assertEqual("verified native report", report)
            self.assertEqual(
                14401, progress.call_args.kwargs["import_progress"]["elapsed_seconds"]
            )
            self.assertEqual(
                3, progress.call_args.kwargs["import_progress"]["processed"]
            )

    def test_silent_import_warns_without_aborting_and_recovers_on_real_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            candidate = self.candidate(Path(temporary))
            report, progress = self.run_wait(
                candidate,
                [
                    {"state": "importing", "progress": {"stage": "files", "done": 1}},
                    {"state": "importing", "progress": {"stage": "files", "done": 1}},
                    {"state": "importing", "progress": {"stage": "files", "done": 1}},
                    {"state": "importing", "progress": {"stage": "files", "done": 2}},
                    {"state": "done", "progress": {"stage": "done", "done": 3}},
                ],
                [0, 0, 1801, 2200, 2300, 2400],
            )
            self.assertEqual("verified native report", report)
            messages = [call.args[1] for call in progress.call_args_list]
            self.assertIn("No observable native progress", messages[1])
            self.assertIn("Cancel migration", messages[1])
            self.assertIn("No observable native progress", messages[2])
            self.assertEqual("Contained native stage: files", messages[3])
            self.assertEqual(
                2400, progress.call_args.kwargs["import_progress"]["elapsed_seconds"]
            )

    def test_eta_or_poll_heartbeats_do_not_clear_stall_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            candidate = self.candidate(Path(temporary))
            _, progress = self.run_wait(
                candidate,
                [
                    {
                        "state": "importing",
                        "progress": {"stage": "files", "done": 1, "eta_seconds": 100},
                    },
                    {
                        "state": "importing",
                        "progress": {"stage": "files", "done": 1, "eta_seconds": 1000},
                    },
                    {
                        "state": "importing",
                        "progress": {"stage": "files", "done": 1, "eta_seconds": 2000},
                    },
                    {"state": "done", "progress": {"stage": "done", "done": 2}},
                ],
                [0, 0, 1801, 2200, 3000],
            )
            messages = [call.args[1] for call in progress.call_args_list]
            self.assertIn("No observable native progress", messages[1])
            self.assertIn("No observable native progress", messages[2])

    def test_status_probe_timeout_warns_and_keeps_live_importer_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            candidate = self.candidate(Path(temporary))
            report, progress = self.run_wait(
                candidate,
                [
                    {"state": "importing", "progress": {"stage": "files"}},
                    subprocess.TimeoutExpired("contained status probe", 8),
                    OSError("temporary unavailable"),
                    {"state": "done", "progress": {"stage": "done"}},
                ],
                [0, 0, 1801, 2200, 2400],
            )
            self.assertEqual("verified native report", report)
            messages = [call.args[1] for call in progress.call_args_list]
            self.assertIn("No observable native progress", messages[1])
            self.assertIn("currently unavailable", messages[1])
            self.assertIn("No observable native progress", messages[2])
            self.assertEqual("Contained native stage: done", messages[-1])

    def test_cancellation_still_interrupts_an_import_after_a_stall_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            candidate = self.candidate(Path(temporary))
            candidate.check_cancel.side_effect = [
                None,
                None,
                ContainedImportError("cancel after warning"),
            ]
            candidate.probe = Mock(
                return_value={"status": 200, "body": {"state": "importing"}}
            )
            progress = Mock()
            with (
                patch(
                    "utils.infinidysk_v2_execution.time.monotonic",
                    side_effect=[0, 0, 1900],
                ),
                patch("utils.infinidysk_v2_execution.time.sleep"),
                self.assertRaisesRegex(ContainedImportError, "cancel after warning"),
            ):
                candidate.wait_import(True, progress)
            self.assertIn("No observable native progress", progress.call_args.args[1])
            self.assertEqual(2, candidate.probe.call_count)

    def test_probe_failure_does_not_hide_importer_process_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            candidate = self.candidate(Path(temporary))
            candidate.process.poll.return_value = 1
            candidate.probe = Mock(side_effect=subprocess.TimeoutExpired("status", 8))
            with self.assertRaisesRegex(ContainedImportError, "importer exited"):
                candidate.wait_import(True, Mock())

    def test_native_failed_state_is_still_rejected_immediately(self):
        with tempfile.TemporaryDirectory() as temporary:
            candidate = self.candidate(Path(temporary))
            with self.assertRaisesRegex(ContainedImportError, "native import failed"):
                self.run_wait(candidate, [{"state": "failed"}], [0, 99999])

    def test_provider_traffic_keeps_long_running_file_recovery_alive(self):
        with tempfile.TemporaryDirectory() as temporary:
            candidate = self.candidate(Path(temporary))
            candidate.nntp_bridge = SimpleNamespace(
                last_activity=1899, stats={"active_connections": 1, "commands": 10}
            )
            report, _ = self.run_wait(
                candidate,
                [
                    {"state": "importing", "progress": {"stage": "files"}},
                    {"state": "done", "progress": {"stage": "done"}},
                ],
                [0, 0, 1900],
            )
            self.assertEqual("verified native report", report)

    def test_cancellation_is_checked_before_another_native_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            candidate = self.candidate(Path(temporary))
            candidate.check_cancel.side_effect = ContainedImportError(
                "cancel checkpoint"
            )
            candidate.probe = Mock()
            with self.assertRaisesRegex(ContainedImportError, "cancel checkpoint"):
                candidate.wait_import(True, Mock())
            candidate.probe.assert_not_called()
