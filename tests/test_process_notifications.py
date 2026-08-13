import io
import signal
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from utils.processes import ProcessHandler


class ProcessNotificationTests(unittest.TestCase):
    def _handler_with_process(
        self, pid=1234, process_name="Example", managed_service=True
    ):
        handler = object.__new__(ProcessHandler)
        handler.init_attributes(Mock())
        process = Mock()
        process.pid = pid
        process.returncode = -15
        handler.processes[pid] = {
            "name": process_name,
            "internal_name": process_name,
            "description": process_name,
            "process_obj": process,
            "managed_service": managed_service,
        }
        handler.process_names[process_name] = process
        handler.startup_complete_event.set()
        handler._maybe_schedule_restart = Mock()
        return handler

    @patch("utils.processes.notify_event")
    def test_intentional_stop_is_not_reported_as_unexpected(self, notify_event):
        handler = self._handler_with_process()
        handler._intentional_stop_pids.add(1234)

        with patch("utils.processes.os.waitpid", side_effect=[(1234, 0), (0, 0)]):
            handler.reap_zombies(None, None)

        notify_event.assert_not_called()
        handler._maybe_schedule_restart.assert_not_called()
        self.assertNotIn(1234, handler._intentional_stop_pids)

    @patch("utils.processes.notify_event")
    def test_unplanned_exit_still_reports_and_considers_restart(self, notify_event):
        handler = self._handler_with_process()

        with patch("utils.processes.os.waitpid", side_effect=[(1234, 0), (0, 0)]):
            handler.reap_zombies(None, None)

        notify_event.assert_called_once()
        self.assertEqual(notify_event.call_args.args[0], "service.stopped.unexpectedly")
        handler._maybe_schedule_restart.assert_called_once()

    @patch("utils.processes.notify_event")
    def test_transient_setup_process_exit_is_not_reported(self, notify_event):
        handler = self._handler_with_process(
            process_name="dotnet_publish", managed_service=False
        )
        handler.processes[1234]["process_obj"].returncode = 0

        with patch("utils.processes.os.waitpid", side_effect=[(1234, 0), (0, 0)]):
            handler.reap_zombies(None, None)

        notify_event.assert_not_called()
        handler._maybe_schedule_restart.assert_not_called()

    def test_managed_shutdown_signals_the_complete_process_group(self):
        process = Mock(pid=1234)

        with (
            patch("utils.processes.os.getpgid", return_value=1234),
            patch("utils.processes.os.getpgrp", return_value=4321),
            patch("utils.processes.os.killpg") as killpg,
        ):
            process_group = ProcessHandler._signal_process_group(
                process, signal.SIGTERM
            )

        self.assertEqual(1234, process_group)
        killpg.assert_called_once_with(1234, signal.SIGTERM)
        process.terminate.assert_not_called()

    def test_managed_shutdown_falls_back_when_group_signalling_is_unavailable(self):
        process = Mock(pid=1234)

        with patch("utils.processes.os.getpgid", side_effect=PermissionError):
            process_group = ProcessHandler._signal_process_group(
                process, signal.SIGTERM
            )

        self.assertIsNone(process_group)
        process.terminate.assert_called_once_with()

    def test_stop_forces_remaining_group_after_launcher_exits(self):
        handler = self._handler_with_process()
        process = handler.process_names["Example"]
        process.poll.return_value = 0
        handler._get_shutdown_policy = Mock(
            return_value={"max_attempts": 1, "wait_timeout": 0}
        )
        handler._signal_process_group = Mock(side_effect=[1234, 1234])
        handler._process_group_alive = Mock(side_effect=[True, True, False, False])
        handler._update_running_processes_file = Mock()

        handler.stop_process("Example")

        handler._signal_process_group.assert_has_calls(
            [
                call(process, signal.SIGTERM),
                call(process, signal.SIGKILL, process_group=1234),
            ]
        )
        self.assertNotIn("Example", handler.process_names)

    def test_bazarr_shutdown_uses_one_grace_window(self):
        handler = self._handler_with_process()
        with patch(
            "utils.processes.CONFIG_MANAGER.find_key_for_process",
            return_value=("bazarr", None),
        ):
            policy = handler._get_shutdown_policy("Bazarr")

        self.assertEqual({"max_attempts": 1, "wait_timeout": 10}, policy)

    @patch("utils.processes.notify_event")
    def test_immediate_helper_failure_preserves_stderr_before_logging(
        self, notify_event
    ):
        handler = object.__new__(ProcessHandler)
        handler.init_attributes(Mock())
        process = Mock(
            pid=1234,
            returncode=1,
            stdout=io.StringIO(""),
            stderr=io.StringIO("fatal error: fuse.h: No such file or directory\n"),
        )
        process.poll.return_value = 1

        with (
            patch(
                "utils.processes.CONFIG_MANAGER.find_key_for_process",
                return_value=(None, None),
            ),
            patch("utils.processes.CONFIG_MANAGER.get", return_value={}),
            patch("utils.processes.subprocess.Popen", return_value=process),
            patch("utils.processes.SubprocessLogger") as subprocess_logger,
        ):
            success, error = handler.start_process(
                "go_build", "/tmp", ["go", "build", "."]
            )

        self.assertFalse(success)
        self.assertEqual(
            "go_build failed to stay running (exit code 1): "
            "fatal error: fuse.h: No such file or directory",
            error,
        )
        self.assertEqual(
            "fatal error: fuse.h: No such file or directory", handler.stderr
        )
        subprocess_logger.assert_not_called()
        notify_event.assert_called_once()

    @patch("utils.processes.notify_event")
    def test_maintainerr_yarn_helpers_retain_controller_identity(self, notify_event):
        def config_get(key, default=None):
            if key in {"puid", "pgid"}:
                return 1000
            if key == "rclone":
                return {}
            return default

        for process_name in (
            "maintainerr_yarn_install",
            "maintainerr_yarn_build",
            "maintainerr_yarn_focus",
            "maintainerr_yarn_rebuild_canvas",
        ):
            with self.subTest(process_name=process_name):
                handler = object.__new__(ProcessHandler)
                handler.init_attributes(Mock())
                handler._update_running_processes_file = Mock()
                process = Mock(
                    pid=1234,
                    returncode=0,
                    stdout=io.StringIO(""),
                    stderr=io.StringIO(""),
                )
                process.poll.return_value = 0

                with (
                    patch(
                        "utils.processes.CONFIG_MANAGER.find_key_for_process",
                        return_value=(None, None),
                    ),
                    patch("utils.processes.CONFIG_MANAGER.get", side_effect=config_get),
                    patch(
                        "utils.processes.subprocess.Popen", return_value=process
                    ) as popen,
                    patch("utils.processes.SubprocessLogger"),
                ):
                    success, error = handler.start_process(
                        process_name,
                        "/tmp",
                        ["node", "yarn.cjs", "install"],
                    )

                self.assertTrue(success, error)
                self.assertIsNone(popen.call_args.kwargs["preexec_fn"])

        notify_event.assert_not_called()

    @patch("utils.processes.notify_event")
    def test_managed_service_clean_immediate_exit_is_start_failure(self, notify_event):
        handler = object.__new__(ProcessHandler)
        handler.init_attributes(Mock())
        handler.setup_tracker.add("Example")
        process = Mock(
            pid=1234,
            returncode=0,
            stdout=io.StringIO("recovered panic: database permission denied\n"),
            stderr=io.StringIO(""),
        )
        process.poll.return_value = 0
        config = {
            "config_dir": "/tmp",
            "command": ["example"],
            "env": {},
        }

        def config_get(key, default=None):
            if key in {"puid", "pgid"}:
                return 1000
            return default if default is not None else {}

        with (
            patch(
                "utils.processes.CONFIG_MANAGER.find_key_for_process",
                return_value=("example", None),
            ),
            patch(
                "utils.processes.CONFIG_MANAGER.get_instance",
                return_value=config,
            ),
            patch("utils.processes.CONFIG_MANAGER.get", side_effect=config_get),
            patch("utils.processes.subprocess.Popen", return_value=process),
            patch("utils.processes.SubprocessLogger") as subprocess_logger,
        ):
            success, error = handler.start_process("Example", "/tmp", ["example"])

        self.assertFalse(success)
        self.assertEqual(
            "Example failed to stay running (exit code 0): "
            "recovered panic: database permission denied",
            error,
        )
        self.assertEqual("recovered panic: database permission denied", handler.stdout)
        self.assertTrue(
            any(
                "database permission denied" in str(call)
                for call in handler.logger.error.call_args_list
            )
        )
        self.assertNotIn("Example", handler.process_names)
        subprocess_logger.assert_not_called()
        notify_event.assert_called_once()

    def test_clean_immediate_helper_exit_remains_successful(self):
        handler = object.__new__(ProcessHandler)
        handler.init_attributes(Mock())
        process = Mock(
            returncode=0,
            stdout=io.StringIO(""),
            stderr=io.StringIO(""),
        )
        process.poll.return_value = 0

        success, error = handler._check_immediate_exit_and_log(
            process,
            "build_helper",
            timeout_seconds=0.1,
            interval_seconds=0.01,
        )

        self.assertTrue(success)
        self.assertIsNone(error)

    def test_startup_readiness_reports_pending_then_ready(self):
        handler = object.__new__(ProcessHandler)
        handler.init_attributes(Mock())
        with tempfile.TemporaryDirectory() as temp_dir:
            handler.startup_state_path = f"{temp_dir}/startup.json"
            handler.set_startup_expected_services(["Example"])
            self.assertEqual(
                handler.get_startup_status()["services"]["Example"]["state"],
                "pending",
            )

            process = Mock()
            process.poll.return_value = None
            handler.processes[1234] = {
                "name": "Example",
                "process_obj": process,
                "start_time": 1,
            }
            handler.get_process_health = Mock(
                return_value={
                    "status": "healthy",
                    "healthy": True,
                    "reason": None,
                    "details": {"probe": "application"},
                }
            )

            self.assertEqual(
                handler.get_startup_status()["services"]["Example"]["state"],
                "ready",
            )

    def test_starting_application_health_does_not_complete_startup_readiness(self):
        handler = object.__new__(ProcessHandler)
        handler.init_attributes(Mock())
        process = Mock()
        process.poll.return_value = None
        handler.processes[1234] = {
            "name": "InfiniDysk",
            "process_obj": process,
            "start_time": 1,
        }
        handler.get_process_health = Mock(
            return_value={
                "status": "starting",
                "healthy": True,
                "reason": "InfiniDysk reports migrating",
                "details": {
                    "probe": "InfiniDysk backend health",
                    "http_status": 503,
                },
            }
        )

        readiness = handler.get_service_readiness("InfiniDysk")

        self.assertEqual(readiness["state"], "starting")
        self.assertEqual(readiness["health_status"], "starting")
        self.assertIn("migrating", readiness["reason"])

    def test_starting_health_does_not_count_as_auto_restart_failure(self):
        handler = object.__new__(ProcessHandler)
        handler.get_process_health = Mock(
            return_value={
                "status": "starting",
                "healthy": True,
                "reason": "InfiniDysk reports migrating",
                "details": {"probe": "InfiniDysk backend health"},
            }
        )

        healthy, reason = handler._check_process_health("InfiniDysk", 1234)

        self.assertTrue(healthy)
        self.assertIn("migrating", reason)

    @patch("utils.processes.notify_event")
    def test_auto_restart_success_waits_for_verified_health(self, notify_event):
        handler = object.__new__(ProcessHandler)
        handler.init_attributes(Mock())
        state = handler._get_restart_state("Example")
        state["awaiting_recovery"] = True

        should_restart = handler._record_healthcheck_result(
            "Example", True, None, {"unhealthy_threshold": 3}
        )

        self.assertFalse(should_restart)
        self.assertEqual(state["restart_successes"], 1)
        self.assertFalse(state["awaiting_recovery"])
        notify_event.assert_called_once()
        self.assertEqual(
            notify_event.call_args.args[0], "service.auto_restart.succeeded"
        )

    def test_auto_restart_does_not_schedule_during_stack_startup(self):
        handler = object.__new__(ProcessHandler)
        handler.init_attributes(Mock())
        handler._get_service_restart_policy = Mock(return_value={"enabled": True})

        handler._maybe_schedule_restart("Example", "not ready")

        handler._get_service_restart_policy.assert_not_called()

    def test_auto_restart_uses_service_specific_grace_period(self):
        handler = object.__new__(ProcessHandler)
        handler.init_attributes(Mock())
        process = Mock()
        process.poll.return_value = None
        handler.process_names["Example"] = process
        handler.startup_complete_event.set()
        handler.wait_for_startup_complete = Mock(return_value=True)
        handler._get_auto_restart_config = Mock(
            return_value={"enabled": True, "grace_period_seconds": 30}
        )
        handler._get_service_restart_policy = Mock(
            return_value={
                "enabled": True,
                "restart_on_unhealthy": True,
                "grace_period_seconds": 300,
            }
        )
        handler._is_restart_disabled = Mock(return_value=False)
        observed_grace_periods = []

        def stop_after_observing(_process_name, grace_period, _monitor_started_at):
            observed_grace_periods.append(grace_period)
            handler.shutting_down = True
            return False

        handler._is_ready_for_healthcheck = stop_after_observing

        handler.start_auto_restart_monitor()
        handler.auto_restart_thread.join(timeout=1)

        self.assertEqual(observed_grace_periods, [300])

    def test_expected_service_monitoring_stays_paused_when_startup_is_degraded(self):
        handler = object.__new__(ProcessHandler)
        handler.init_attributes(Mock())
        handler.startup_phase = "degraded"
        handler.startup_complete_event.set()
        handler.startup_expected_services = {"Example Service"}

        self.assertFalse(handler.is_service_ready_for_monitoring("exampleservice"))
        self.assertTrue(handler.is_service_ready_for_monitoring("Unrelated"))


if __name__ == "__main__":
    unittest.main()
