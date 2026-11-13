"""Dependency-ordered retained consumers across native major migration recovery."""

import copy
from functools import wraps
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_infinidysk_v2_execution as fixtures
from utils.infinidysk_v2_execution import ContainedImportError
from utils.infinidysk_v2_subprocess import MigrationProcessCleanupError


def isolated_service_order_test(method):
    """Exercise actual lifecycle dependencies outside legacy router stubs.

    Full discovery imports older suites that permanently replace dependency_map
    in sys.modules. These integration contracts need the real shared graph, as
    do the existing isolated native-runtime tests.
    """

    @wraps(method)
    def isolated(self):
        test_id = f"tests.{Path(__file__).stem}.{type(self).__name__}.{method.__name__}"
        if os.environ.get("DUMB_NATIVE_SERVICE_ORDER_TEST_ID") == test_id:
            return method(self)
        result = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", test_id],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "DUMB_NATIVE_SERVICE_ORDER_TEST_ID": test_id},
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    return isolated


class MigrationServiceOrderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExecutionTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.config = self.fixture.config
        self.plan = self.fixture.plan
        self.handler = self.fixture.handler
        self.executor = self.fixture.executor
        self.progress = self.fixture.progress
        self.config.config["sonarr"]["instances"]["fixture"].update(
            port=8989, wait_for_mounts=["/mnt/legacy"]
        )
        self.config.config["radarr"] = {
            "instances": {
                "fixture": {
                    "enabled": True,
                    "process_name": "Radarr",
                    "port": 7878,
                    "wait_for_mounts": ["/mnt/legacy"],
                }
            }
        }
        self.config.config["prowlarr"] = {
            "enabled": True,
            "process_name": "Prowlarr",
            "port": 9696,
        }
        # Enabled but not previously running/inventory-owned: never start it.
        self.config.config["whisparr"] = {
            "instances": {"idle": {"enabled": True, "process_name": "Idle Whisparr"}}
        }
        self.plan["inventory"]["consumers"] = [
            {
                "service_key": key,
                "process_name": name,
                "process_identity": {"pid": index},
            }
            for index, (key, name) in enumerate(
                [("prowlarr", "Prowlarr"), ("radarr", "Radarr"), ("sonarr", "Sonarr")],
                20,
            )
        ]
        for name in ("Prowlarr", "Radarr"):
            self.handler.process_names[name] = SimpleNamespace(poll=lambda: None)

    def starts(self):
        return [name for action, name in self.handler.calls if action == "start"]

    def stops(self):
        return [name for action, name in self.handler.calls if action == "stop"]

    def assert_arr_before_prowlarr(self, names):
        self.assertLess(names.index("Radarr"), names.index("Prowlarr"))
        self.assertLess(names.index("Sonarr"), names.index("Prowlarr"))

    @isolated_service_order_test
    def test_fuse_cutover_stops_dependents_first_and_resumes_arrs_before_prowlarr(self):
        self.fixture.service["mount_backend"] = "fuse"
        seen = []

        def delivery(*_):
            seen.append(self.starts())
            return True

        with patch(
            "utils.infinidysk_v2_execution.delivery_mount_ready", side_effect=delivery
        ):
            result = self.executor.cutover(
                self.plan, self.fixture.workspace, self.progress
            )
        self.assertTrue(result["generation_2_started"])
        self.assertEqual(
            self.stops(),
            ["Prowlarr", "Sonarr", "Radarr", "rclone InfiniDysk", "InfiniDysk"],
        )
        self.assertEqual(self.starts(), ["InfiniDysk", "Radarr", "Sonarr", "Prowlarr"])
        self.assertEqual(seen, [["InfiniDysk"]])
        self.assertNotIn("Idle Whisparr", self.starts())
        self.assertNotIn("rclone InfiniDysk", self.starts())
        self.assertEqual(
            self.progress.call_args_list[0].kwargs["original_running_names"],
            ["InfiniDysk", "rclone InfiniDysk", "Radarr", "Sonarr", "Prowlarr"],
        )

    @isolated_service_order_test
    def test_rehearsal_restores_source_and_mount_before_arrs_and_indexer(self):
        self.fixture.service["mount_backend"] = "fuse"
        result = self.executor.rehearse(
            self.plan, self.fixture.workspace, self.progress
        )
        self.assertTrue(result["source_restored"])
        self.assertEqual(
            self.starts(),
            ["InfiniDysk", "rclone InfiniDysk", "Radarr", "Sonarr", "Prowlarr"],
        )
        self.assert_arr_before_prowlarr(self.starts())
        self.assertNotIn("Idle Whisparr", self.starts())

    @isolated_service_order_test
    def test_rclone_cutover_preserves_selected_owner_and_only_prior_consumers(self):
        result = self.executor.cutover(self.plan, self.fixture.workspace, self.progress)
        self.assertTrue(result["generation_2_started"])
        self.assertEqual(
            self.starts(),
            ["InfiniDysk", "rclone InfiniDysk", "Radarr", "Sonarr", "Prowlarr"],
        )
        self.assertNotIn("Idle Whisparr", self.starts())

    @isolated_service_order_test
    def test_rollback_reorders_old_checkpoint_and_restores_original_rclone_owner(self):
        self.fixture.service["mount_backend"] = "fuse"
        self.config.config["rclone"]["instances"]["legacy"]["enabled"] = False
        self.handler.process_names.pop("rclone InfiniDysk")

        def restore_config():
            self.config.config["infinidysk"] = copy.deepcopy(
                self.plan["source"]["service"]
            )
            self.config.config["rclone"]["instances"]["legacy"]["enabled"] = True

        names = ["Prowlarr", "Sonarr", "rclone InfiniDysk", "InfiniDysk", "Radarr"]
        result = self.executor.rollback(
            {
                "plan": self.plan,
                "execution": {"original_running_names": names},
                "restore_config": restore_config,
            },
            self.progress,
        )
        self.assertTrue(result["source_restored"])
        self.assertEqual(self.stops(), ["Prowlarr", "Sonarr", "Radarr", "InfiniDysk"])
        self.assertEqual(self.starts()[:2], ["InfiniDysk", "rclone InfiniDysk"])
        self.assert_arr_before_prowlarr(self.starts())
        self.assertEqual(set(self.starts()), set(names))
        self.assertEqual(len(self.starts()), len(set(self.starts())))

    @isolated_service_order_test
    def test_failed_arr_restart_never_starts_prowlarr_and_retry_skips_live_owners(self):
        self.handler.process_names.clear()
        start = self.handler.start_process

        def fail_radarr(name):
            if name == "Radarr":
                self.handler.calls.append(("start", name))
                return False, "unavailable"
            return start(name)

        names = ["InfiniDysk", "rclone InfiniDysk", "Prowlarr", "Radarr", "Sonarr"]
        with patch.object(self.handler, "start_process", side_effect=fail_radarr):
            with self.assertRaisesRegex(ContainedImportError, "consumer"):
                self.executor._restore_original(self.plan, names)
        self.assertEqual(self.starts(), ["InfiniDysk", "rclone InfiniDysk", "Radarr"])
        self.executor._restore_original(self.plan, names)
        self.assertEqual(self.starts().count("InfiniDysk"), 1)
        self.assertEqual(self.starts().count("rclone InfiniDysk"), 1)
        self.assert_arr_before_prowlarr(self.starts())

    @isolated_service_order_test
    def test_source_cycle_is_refused_before_any_stop_or_import(self):
        self.config.config["sonarr"]["instances"]["fixture"]["wait_for_tcp"] = {
            "host": "localhost",
            "port": 9696,
        }
        with self.assertRaisesRegex(ContainedImportError, "cycle"):
            self.executor._quiesce(self.plan, self.progress)
        self.assertEqual(self.handler.calls, [])
        self.progress.assert_not_called()
        self.fixture.native.assert_not_called()

    @isolated_service_order_test
    def test_preview_preflight_reports_graph_conflict_before_namespace_probe(self):
        self.config.config["sonarr"]["instances"]["fixture"]["wait_for_tcp"] = {
            "host": "localhost",
            "port": 9696,
        }
        with (
            patch("utils.infinidysk_v2_execution.os.geteuid", return_value=0),
            patch(
                "utils.infinidysk_v2_execution.shutil.which",
                return_value="/reserved/nsenter",
            ),
            patch("utils.infinidysk_v2_execution.ctypes.CDLL") as libc,
            patch("utils.infinidysk_v2_execution.subprocess.run") as spawning,
        ):
            libc.return_value.syscall.return_value = 4
            result = self.executor.preflight(self.plan)
        self.assertTrue(any("cycle" in message for message in result["blockers"]))
        spawning.assert_not_called()
        self.assertEqual(self.handler.calls, [])

    @isolated_service_order_test
    def test_new_fuse_target_cycle_is_refused_before_any_original_service_stops(self):
        self.fixture.service.update(
            mount_backend="fuse",
            wait_for_tcp={"host": "localhost", "port": 9696},
        )
        with self.assertRaisesRegex(ContainedImportError, "cycle"):
            self.executor._quiesce(self.plan, self.progress)
        self.assertEqual(self.handler.calls, [])
        self.progress.assert_not_called()
        self.fixture.native.assert_not_called()

    @isolated_service_order_test
    def test_changed_or_ambiguous_configured_identity_is_refused_before_stop(self):
        for mode in ("missing", "ambiguous"):
            with self.subTest(mode=mode):
                original = copy.deepcopy(self.config.config["prowlarr"])
                if mode == "missing":
                    del self.config.config["prowlarr"]
                else:
                    self.config.config["unrelated"] = copy.deepcopy(original)
                with self.assertRaisesRegex(
                    ContainedImportError, "configured identity"
                ):
                    self.executor._quiesce(self.plan, self.progress)
                self.assertEqual(self.handler.calls, [])
                self.config.config["prowlarr"] = original
                self.config.config.pop("unrelated", None)

    @isolated_service_order_test
    def test_rollback_rejects_changed_retained_scope_before_stop_or_restore(self):
        for unknown in ("Unreviewed process", {"process_name": "Prowlarr"}):
            with self.subTest(unknown=unknown):
                restored = []
                with self.assertRaisesRegex(ContainedImportError, "scope changed"):
                    self.executor.rollback(
                        {
                            "plan": self.plan,
                            "execution": {
                                "original_running_names": ["InfiniDysk", unknown]
                            },
                            "restore_config": lambda: restored.append(True),
                        },
                        self.progress,
                    )
                self.assertEqual(restored, [])
                self.assertEqual(self.handler.calls, [])

    @isolated_service_order_test
    def test_live_external_registration_prevents_duplicate_start(self):
        self.handler.process_names.pop("Sonarr")
        self.handler.external_processes = {"Sonarr": 2147483000}
        with patch.object(
            self.executor, "_pid_alive", side_effect=lambda pid: pid == 2147483000
        ):
            self.executor._start_retained("Sonarr", "must not start twice")
        self.assertEqual(self.handler.calls, [])

    @isolated_service_order_test
    def test_uninspectable_original_pid_keeps_recovery_held(self):
        self.handler.process_names["Sonarr"].pid = 2147483000

        def unreadable(pid):
            if pid == 2147483000:
                raise PermissionError("private fixture")
            return False

        with patch.object(self.executor, "_pid_alive", side_effect=unreadable):
            with self.assertRaises(MigrationProcessCleanupError) as caught:
                self.executor._quiesce(self.plan, self.progress)
        self.assertFalse(caught.exception.cleanup_complete)
        self.assertEqual(caught.exception.cleanup_evidence, {"process_group": None})
        self.fixture.native.assert_not_called()
        self.assertEqual(self.starts(), [])

    @isolated_service_order_test
    def test_unregistered_original_pid_keeps_cleanup_held_before_import(self):
        self.handler.process_names["Sonarr"].pid = 2147483000
        with patch.object(
            self.executor, "_pid_alive", side_effect=lambda pid: pid == 2147483000
        ):
            with self.assertRaises(MigrationProcessCleanupError) as caught:
                self.executor._quiesce(self.plan, self.progress)
        self.assertFalse(caught.exception.cleanup_complete)
        self.assertEqual(caught.exception.cleanup_evidence, {"process_group": None})
        self.fixture.native.assert_not_called()
        self.assertEqual(self.starts(), [])


if __name__ == "__main__":
    unittest.main()
