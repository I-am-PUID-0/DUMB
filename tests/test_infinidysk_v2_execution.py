import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
            "candidate_data": str(self.root / "candidate"),
            "root": str(self.root / "private"),
        }
        Path(self.workspace["root"]).mkdir()
        self.snapshots = []
        self.oidc = False

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
            }

        self.workspace.update(
            capture_source=capture, accept_import=Mock(), activate_config=Mock()
        )
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
        self.candidate.__enter__ = Mock(return_value=self.candidate)
        self.candidate.__exit__ = Mock(return_value=False)
        self.candidate.wait_import.return_value = (
            "Imported 0 directories, 0 files, 0 NZBs, 0 history rows, 0 queued jobs"
        )
        patcher = patch(
            "utils.infinidysk_v2_execution.NativeCandidate", return_value=self.candidate
        )
        self.patches.append(patcher)
        self.native = patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

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
                    "CREATE TABLE DavItems(Id TEXT,Name TEXT,Type INTEGER,Path TEXT);CREATE TABLE HistoryItems(Id TEXT);"
                )
                db.execute(
                    "INSERT INTO DavItems VALUES(?,?,2,'/content/movie')",
                    (identifier, "movie.mkv"),
                )
                db.execute("INSERT INTO HistoryItems VALUES(?)", (identifier,))
            with sqlite3.connect(target) as db:
                db.executescript(
                    "CREATE TABLE items(id BLOB,name TEXT);CREATE TABLE history(id BLOB);"
                )
                value = bytes.fromhex(identifier.replace("-", ""))
                db.execute("INSERT INTO items VALUES(?,?)", (value, "movie.mkv"))
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
