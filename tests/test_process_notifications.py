import signal
import unittest
from unittest.mock import Mock, patch

from utils.processes import ProcessHandler


class ProcessNotificationTests(unittest.TestCase):
    def _handler_with_process(
        self, pid=1234, process_name="Example", managed_service=True
    ):
        handler = object.__new__(ProcessHandler)
        handler.init_attributes(Mock())
        process = Mock()
        process.returncode = -15
        handler.processes[pid] = {
            "name": process_name,
            "internal_name": process_name,
            "description": process_name,
            "process_obj": process,
            "managed_service": managed_service,
        }
        handler.process_names[process_name] = process
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
            ProcessHandler._signal_process_group(process, signal.SIGTERM)

        killpg.assert_called_once_with(1234, signal.SIGTERM)
        process.terminate.assert_not_called()

    def test_managed_shutdown_falls_back_when_group_signalling_is_unavailable(self):
        process = Mock(pid=1234)

        with patch("utils.processes.os.getpgid", side_effect=PermissionError):
            ProcessHandler._signal_process_group(process, signal.SIGTERM)

        process.terminate.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
