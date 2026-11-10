"""Offline boot-order regressions: no real processes, global config or mounts."""

import ast
import copy
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


def definitions(filename, names, namespace):
    tree = ast.parse(Path(__file__).parents[1].joinpath(filename).read_text())
    selected = ast.Module(
        body=[
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in names
        ],
        type_ignores=[],
    )
    exec(compile(selected, filename, "exec"), namespace)  # noqa: S102
    return namespace


class StopBoot(BaseException):
    pass


class RecoveryBootTests(unittest.TestCase):
    def namespace(self):
        events = []
        controller = SimpleNamespace(boot_error=None)
        controller.initialize_controller = Mock(
            side_effect=lambda: events.append("initialize_controller")
        )
        controller.start_scheduler = Mock(
            side_effect=lambda: events.append("scheduler")
        )
        manager_module = ModuleType("utils.installation_backup")
        manager_module.get_installation_backup_manager = Mock(return_value=controller)
        hold = [None]
        manager_module.installation_backup_blocker = (
            lambda: controller.boot_error or hold[0]
        )
        self.addCleanup(patch.stopall)
        storage = SimpleNamespace(boot_error=None)
        storage_module = ModuleType("utils.backup_storage")
        storage_module.get_backup_storage = Mock(return_value=storage)
        patch.dict(
            sys.modules,
            {
                "utils.installation_backup": manager_module,
                "utils.backup_storage": storage_module,
            },
        ).start()
        handler = SimpleNamespace(
            start_auto_restart_monitor=Mock(
                side_effect=lambda: events.append("auto_restart")
            ),
            set_startup_phase=Mock(),
            setup_tracker_lock=threading.RLock(),
            setup_tracker=set(),
            start_process=Mock(return_value=(True, None)),
            register_external_process=Mock(),
            set_startup_expected_services=Mock(),
            set_recovery_required=Mock(),
        )
        manager = SimpleNamespace(
            file_path="/nonexistent-private-test/config.json",
            get=lambda key, default=None: {"puid": 1001, "pgid": 1001}.get(
                key, default
            ),
        )
        private = Mock(return_value=(True, None))
        users = SimpleNamespace(
            PRIVATE_CONTROLLER_CONFIG_ENTRIES={
                ".dumb-setup",
                "installation-backups",
                "backup-storage",
            },
            _repair_private_controller_tree=private,
            create_system_user=Mock(side_effect=lambda: events.append("create_user")),
        )
        namespace = definitions(
            "main.py",
            {
                "_repair_boot_controller_state",
                "_initialize_installation_recovery",
                "_start_recovery_control_plane",
                "main",
            },
            {
                "os": os,
                "threading": SimpleNamespace(
                    Event=lambda: SimpleNamespace(wait=lambda: events.append("wait"))
                ),
                "logger": Mock(),
                "user_management": users,
                "config": manager,
                "ProcessHandler": Mock(return_value=handler),
                "Update": Mock(),
                "ConnectionManager": Mock(),
                "websocket_manager": Mock(),
                "log_ascii_art": Mock(),
                "initialize_dependencies": Mock(
                    side_effect=lambda **kw: events.append(
                        "dependencies_recovery"
                        if kw["recovery_only"]
                        else "dependencies_normal"
                    )
                ),
                "frontend_entrypoint_exists": Mock(return_value=True),
                "start_fastapi_process": Mock(),
            },
        )
        return namespace, controller, handler, manager, users, events, hold

    def test_private_repair_only_visits_existing_fixed_state(self):
        ns, _, _, manager, users, _, _ = self.namespace()
        with tempfile.TemporaryDirectory() as root:
            manager.file_path = str(Path(root) / "config.json")
            Path(root, ".dumb-setup").mkdir()
            Path(root, "backup-storage").mkdir()
            Path(root, "some-application").mkdir()
            Path(root, "config.json").write_text("preserve")
            self.assertIsNone(ns["_repair_boot_controller_state"](manager))
            called = [
                Path(call.args[0]).name
                for call in users._repair_private_controller_tree.call_args_list
            ]
            self.assertEqual(called, [".dumb-setup", "backup-storage"])
            self.assertEqual(Path(root, "config.json").read_text(), "preserve")
            users.create_system_user.assert_not_called()

    def test_optional_private_state_failure_keeps_normal_boot_and_checks_installation_state(
        self,
    ):
        for optional in (".dumb-setup", "backup-storage"):
            with self.subTest(optional=optional):
                ns, controller, handler, manager, users, _, _ = self.namespace()
                with tempfile.TemporaryDirectory() as root:
                    manager.file_path = str(Path(root) / "config.json")
                    Path(root, optional).mkdir()
                    Path(root, "installation-backups").mkdir()
                    users._repair_private_controller_tree.side_effect = (
                        lambda path, *_: (
                            (False, "Unsafe optional private state.")
                            if Path(path).name == optional
                            else (True, None)
                        )
                    )
                    users.create_system_user.side_effect = StopBoot
                    with self.assertRaises(StopBoot):
                        ns["main"]()
                    inspected = [
                        Path(call.args[0]).name
                        for call in users._repair_private_controller_tree.call_args_list
                    ]
                    self.assertIn(optional, inspected)
                    self.assertIn("installation-backups", inspected)
                    controller.initialize_controller.assert_called_once()
                    self.assertIsNone(controller.boot_error)
                    self.assertFalse(
                        ns["initialize_dependencies"].call_args.kwargs["recovery_only"]
                    )
                    handler.start_auto_restart_monitor.assert_called_once()
                    self.assertIn(optional, repr(ns["logger"].error.call_args_list))
                    storage_module = sys.modules["utils.backup_storage"]
                    if optional == "backup-storage":
                        storage_module.get_backup_storage.assert_called_once_with(
                            manager
                        )
                        self.assertEqual(
                            storage_module.get_backup_storage.return_value.boot_error,
                            "Unsafe optional private state.",
                        )
                    else:
                        storage_module.get_backup_storage.assert_not_called()
                        self.assertIsNone(
                            storage_module.get_backup_storage.return_value.boot_error
                        )

    def test_unsafe_installation_state_still_holds_all_normal_startup(self):
        ns, controller, handler, manager, users, _, _ = self.namespace()
        with tempfile.TemporaryDirectory() as root:
            manager.file_path = str(Path(root) / "config.json")
            Path(root, ".dumb-setup").mkdir()
            Path(root, "installation-backups").mkdir()
            users._repair_private_controller_tree.return_value = (
                False,
                "Unsafe private controller state.",
            )
            ns["_start_recovery_control_plane"] = Mock()
            ns["main"]()
            controller.initialize_controller.assert_not_called()
            self.assertIn("Unsafe private controller state", controller.boot_error)
            self.assertTrue(
                ns["initialize_dependencies"].call_args.kwargs["recovery_only"]
            )
            users.create_system_user.assert_not_called()
            handler.start_auto_restart_monitor.assert_not_called()
            ns["_start_recovery_control_plane"].assert_called_once()

    def test_unsafe_private_state_latches_hold_without_initializer(self):
        ns, controller, _, _, _, _, _ = self.namespace()
        ns["_repair_boot_controller_state"] = (
            lambda _config: "Controller UID 0 cannot adopt unsafe private state from configured PUID 1001."
        )
        _, hold = ns["_initialize_installation_recovery"](ns["config"], object())
        self.assertIn("unsafe private state", hold)
        self.assertIn("restart DUMB", controller.boot_error)
        controller.initialize_controller.assert_not_called()

    def test_initialization_failure_is_safe_and_blocks_normal_boot(self):
        ns, controller, _, _, _, _, _ = self.namespace()
        controller.initialize_controller.side_effect = OSError(
            "private-password-never-log"
        )
        _, hold = ns["_initialize_installation_recovery"](ns["config"], object())
        self.assertTrue(hold)
        self.assertIn("restart", controller.boot_error)
        self.assertNotIn("private-password-never-log", repr(ns["logger"].mock_calls))

    def test_pending_restore_branches_before_global_migrations_and_workers(self):
        ns, controller, handler, _, users, events, hold = self.namespace()
        hold[0] = "Installation recovery owns managed state."
        ns["_repair_boot_controller_state"] = lambda _: events.append("repair")
        ns["_start_recovery_control_plane"] = Mock(
            side_effect=lambda *_: events.append("recovery_control")
        )
        ns["main"]()
        self.assertEqual(
            events,
            [
                "repair",
                "initialize_controller",
                "dependencies_recovery",
                "recovery_control",
                "wait",
            ],
        )
        users.create_system_user.assert_not_called()
        handler.start_auto_restart_monitor.assert_not_called()
        controller.start_scheduler.assert_not_called()
        self.assertIn("restart DUMB", controller.boot_error)

    def test_completed_restore_handoff_clears_before_normal_boot_decision(self):
        ns, controller, handler, _, users, events, hold = self.namespace()
        hold[0] = "Restart handoff pending."

        def clear():
            events.append("initialize_controller")
            hold[0] = None

        controller.initialize_controller.side_effect = clear
        ns["_repair_boot_controller_state"] = lambda _: events.append("repair")
        users.create_system_user.side_effect = StopBoot
        with self.assertRaises(StopBoot):
            ns["main"]()
        self.assertEqual(
            events[:4],
            ["repair", "initialize_controller", "dependencies_normal", "auto_restart"],
        )
        handler.start_auto_restart_monitor.assert_called_once()
        self.assertIsNone(controller.boot_error)

    def test_recovery_uses_installed_frontend_and_never_applies_saved_selectors(self):
        ns, _, handler, manager, _, _, _ = self.namespace()
        frontend = {
            "enabled": True,
            "process_name": "DUMB Frontend",
            "config_dir": "/installed",
            "command": ["node", ".output/server/index.mjs"],
            "branch_enabled": True,
            "commit_sha": "a" * 40,
            "env": {"PORT": "3005"},
        }
        root = {
            "api_service": {"enabled": True, "process_name": "DUMB API"},
            "frontend": frontend,
        }
        before = copy.deepcopy(root)
        manager.get = lambda key, default=None: root if key == "dumb" else default
        ns["_start_recovery_control_plane"](
            handler, manager, "Recovery; restart required."
        )
        ns["start_fastapi_process"].assert_called_once_with()
        handler.register_external_process.assert_called_once_with(
            "DUMB API", os.getpid()
        )
        handler.start_process.assert_called_once_with(
            "DUMB Frontend",
            config_dir="/installed",
            command=frontend["command"],
            suppress_logging=False,
            env=frontend["env"],
        )
        self.assertEqual(handler.setup_tracker, {"DUMB Frontend"})
        self.assertEqual(root, before)
        handler.set_startup_expected_services.assert_called_once_with(
            ["DUMB API", "DUMB Frontend"]
        )
        self.assertIn(
            "Installation recovery", handler.set_recovery_required.call_args.args[0]
        )

    def test_recovery_status_is_terminal_without_completing_normal_startup(self):
        source = Path(__file__).parents[1] / "utils" / "processes.py"
        tree = ast.parse(source.read_text())
        handler_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ProcessHandler"
        )
        method = next(
            node
            for node in handler_class.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "set_recovery_required"
        )
        namespace = {"time": time}
        exec(  # noqa: S102
            compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"),
            namespace,
        )
        event = threading.Event()
        event.set()
        handler = SimpleNamespace(
            startup_lock=threading.RLock(),
            startup_complete_event=event,
            _write_startup_state=Mock(),
            get_startup_status=Mock(
                return_value={"phase": "recovery_required", "terminal": True}
            ),
        )
        result = namespace["set_recovery_required"](
            handler, {"Installation recovery": "Restart required."}
        )
        self.assertFalse(event.is_set())
        self.assertEqual(handler.startup_phase, "recovery_required")
        self.assertEqual(
            handler.startup_failures, {"Installation recovery": "Restart required."}
        )
        self.assertGreater(handler.startup_completed_at, 0)
        self.assertTrue(result["terminal"])
        handler._write_startup_state.assert_called_once()

    def test_metrics_snapshot_and_write_share_one_persistence_reservation(self):
        source = Path(__file__).parents[1] / "main.py"
        tree = ast.parse(source.read_text())
        worker = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "metrics_history_worker"
        )
        for allowed in (True, False):
            with self.subTest(admitted=allowed):
                events = []

                @contextmanager
                def reserve(label):
                    self.assertEqual(label, "Metrics history persistence")
                    events.append("enter")
                    yield allowed
                    events.append("exit")

                collector = SimpleNamespace(
                    snapshot=lambda **_: events.append("snapshot") or {"sample": 1}
                )
                history = SimpleNamespace(write=lambda value: events.append("write"))
                dependencies = ModuleType("utils.dependencies")
                dependencies.get_metrics_collector = lambda: collector
                dependencies.get_metrics_history_manager = lambda: history
                backup = ModuleType("utils.installation_backup")
                backup.installation_persistence_work = reserve
                namespace = {
                    "config": SimpleNamespace(
                        config={"dumb": {"metrics": {"history_enabled": True}}}
                    ),
                    "logger": Mock(),
                    "time": SimpleNamespace(sleep=Mock(side_effect=StopBoot)),
                }
                exec(  # noqa: S102
                    compile(
                        ast.Module(body=[worker], type_ignores=[]), str(source), "exec"
                    ),
                    namespace,
                )
                with patch.dict(
                    sys.modules,
                    {
                        "utils.dependencies": dependencies,
                        "utils.installation_backup": backup,
                    },
                ):
                    with self.assertRaises(StopBoot):
                        namespace["metrics_history_worker"]()
                self.assertEqual(
                    events,
                    (
                        ["enter", "snapshot", "write", "exit"]
                        if allowed
                        else ["enter", "exit"]
                    ),
                )

    def test_missing_frontend_stays_unavailable_without_installing(self):
        ns, _, handler, manager, _, _, _ = self.namespace()
        manager.get = lambda *_: {
            "api_service": {"enabled": True},
            "frontend": {"enabled": True},
        }
        ns["frontend_entrypoint_exists"].return_value = False
        ns["_start_recovery_control_plane"](handler, manager, "Recovery hold.")
        handler.start_process.assert_not_called()
        failures = handler.set_recovery_required.call_args.args[0]
        self.assertIn("artifacts are missing", failures["DUMB Frontend"])
        self.assertEqual(handler.setup_tracker, set())

    def test_consumer_waits_and_disabled_control_services_never_start(self):
        ns, _, handler, manager, _, _, _ = self.namespace()
        manager.get = lambda *_: {
            "api_service": {"enabled": False},
            "frontend": {"enabled": True, "wait_for_mounts": ["/consumer"]},
        }
        ns["_start_recovery_control_plane"](handler, manager, "Recovery hold.")
        ns["start_fastapi_process"].assert_not_called()
        handler.start_process.assert_not_called()
        failures = handler.set_recovery_required.call_args.args[0]
        self.assertIn("disabled", failures["DUMB API"])
        self.assertIn("consumer readiness dependencies", failures["DUMB Frontend"])


class RecoveryHealthTests(unittest.TestCase):
    def check(self, running, expected, failures=None):
        verify = Mock(return_value=[])
        namespace = definitions(
            "healthcheck.py",
            {"main"},
            {
                "sys": sys,
                "load_startup_state": lambda: {
                    "phase": "recovery_required",
                    "expected_services": expected,
                    "failures": failures
                    or {"Installation recovery": "Restart required."},
                },
                "load_running_processes": lambda _: running,
                "verify_processes": verify,
            },
        )
        with (
            self.assertRaises(SystemExit) as stopped,
            patch("builtins.print") as output,
        ):
            namespace["main"]()
        return stopped.exception.code, verify, output

    def test_healthy_recovery_checks_only_actual_expected_control_processes(self):
        code, verify, output = self.check(
            {"API": 11, "Frontend": 12, "stale-consumer": 13}, ["API", "Frontend"]
        )
        self.assertEqual(code, 0)
        verify.assert_called_once_with({"API": 11, "Frontend": 12})
        self.assertIn(
            "normal services await recovery and restart", str(output.call_args)
        )

    def test_missing_control_process_is_unhealthy(self):
        code, _, _ = self.check({"API": 11}, ["API", "Frontend"])
        self.assertEqual(code, 1)

    def test_disabled_or_unavailable_control_plane_is_not_reported_healthy(self):
        code, _, _ = self.check(
            {"API": 11},
            ["API"],
            {"Installation recovery": "Hold", "Frontend": "Disabled frontend."},
        )
        self.assertEqual(code, 1)
        code, _, _ = self.check({}, [])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
