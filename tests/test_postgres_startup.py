import builtins
import io
import os
import signal
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from utils.postgres import (
    initialize_postgres_config_dir_directory,
    stop_existing_postgres_for_data_directory,
)


class PostgresStartupSafetyTests(unittest.TestCase):
    def _write_pid(self, data_dir, pid=4321, recorded_dir=None):
        with open(
            os.path.join(data_dir, "postmaster.pid"), "w", encoding="utf-8"
        ) as handle:
            handle.write(f"{pid}\n")
            handle.write(f"{recorded_dir or data_dir}\n")

    def test_removes_pid_file_only_when_process_is_gone(self):
        with tempfile.TemporaryDirectory() as data_dir:
            self._write_pid(data_dir)
            with patch("utils.postgres.os.kill", side_effect=ProcessLookupError):
                success, error = stop_existing_postgres_for_data_directory(
                    data_dir, "DUMB"
                )

            self.assertTrue(success)
            self.assertIsNone(error)
            self.assertFalse(os.path.exists(os.path.join(data_dir, "postmaster.pid")))

    def test_stops_live_postgres_for_the_exact_data_directory(self):
        with tempfile.TemporaryDirectory() as data_dir:
            self._write_pid(data_dir)
            real_open = builtins.open

            def open_side_effect(path, *args, **kwargs):
                if str(path) == "/proc/4321/comm":
                    return io.StringIO("postgres\n")
                return real_open(path, *args, **kwargs)

            with (
                patch("utils.postgres.os.kill", return_value=None),
                patch("utils.postgres.open", side_effect=open_side_effect),
                patch(
                    "utils.postgres.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                ) as run,
            ):
                success, error = stop_existing_postgres_for_data_directory(
                    data_dir, "DUMB"
                )

            self.assertTrue(success)
            self.assertIsNone(error)
            command = run.call_args.args[0]
            self.assertEqual(command[:4], ["su", "-", "DUMB", "-s"])
            self.assertIn("pg_ctl -D", command[-1])
            self.assertIn("stop -m fast", command[-1])

    def test_refuses_to_signal_a_pid_not_owned_by_the_data_directory(self):
        with tempfile.TemporaryDirectory() as data_dir:
            self._write_pid(data_dir, recorded_dir="/different/data")
            with patch("utils.postgres.os.kill", return_value=None):
                success, error = stop_existing_postgres_for_data_directory(
                    data_dir, "DUMB"
                )

            self.assertFalse(success)
            self.assertIn("Refusing to stop PID", error)
            self.assertTrue(os.path.exists(os.path.join(data_dir, "postmaster.pid")))

    def test_stops_single_verified_orphan_when_pid_file_was_removed(self):
        with tempfile.TemporaryDirectory() as data_dir:
            real_open = builtins.open
            process_alive = True
            signals = []

            def open_side_effect(path, *args, **kwargs):
                if str(path) == "/proc/8765/cmdline":
                    return io.BytesIO(
                        b"/usr/lib/postgresql/16/bin/postgres\0-D\0"
                        + data_dir.encode()
                        + b"\0"
                    )
                return real_open(path, *args, **kwargs)

            def kill_side_effect(pid, sent_signal):
                nonlocal process_alive
                signals.append((pid, sent_signal))
                if sent_signal == signal.SIGINT:
                    process_alive = False
                    return None
                if sent_signal == 0 and not process_alive:
                    raise ProcessLookupError
                return None

            with (
                patch("utils.postgres.glob.glob", return_value=["/proc/8765/cmdline"]),
                patch("utils.postgres.open", side_effect=open_side_effect),
                patch("utils.postgres.os.kill", side_effect=kill_side_effect),
            ):
                success, error = stop_existing_postgres_for_data_directory(
                    data_dir, "DUMB"
                )

            self.assertTrue(success)
            self.assertIsNone(error)
            self.assertIn((8765, signal.SIGINT), signals)

    def test_refuses_ambiguous_orphan_processes(self):
        with tempfile.TemporaryDirectory() as data_dir:
            real_open = builtins.open

            def open_side_effect(path, *args, **kwargs):
                if str(path).startswith("/proc/"):
                    return io.BytesIO(b"postgres\0-D\0" + data_dir.encode() + b"\0")
                return real_open(path, *args, **kwargs)

            with (
                patch(
                    "utils.postgres.glob.glob",
                    return_value=["/proc/8765/cmdline", "/proc/8766/cmdline"],
                ),
                patch("utils.postgres.open", side_effect=open_side_effect),
            ):
                success, error = stop_existing_postgres_for_data_directory(
                    data_dir, "DUMB"
                )

            self.assertFalse(success)
            self.assertIn("multiple parent processes", error)

    def test_initdb_uses_utf8_locale_and_configured_arguments(self):
        process_handler = SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="",
            start_process=Mock(return_value=(True, None)),
            wait=Mock(),
        )

        with tempfile.TemporaryDirectory() as data_dir:
            success, error = initialize_postgres_config_dir_directory(
                process_handler,
                data_dir,
                "DUMB",
                "test-password",
                "--data-checksums --auth-local=scram-sha-256",
            )

        self.assertTrue(success)
        self.assertIsNone(error)
        command = process_handler.start_process.call_args.args[2]
        self.assertEqual(command[:6], ["su", "-", "DUMB", "-s", "/bin/bash", "-c"])
        initdb_command = command[-1]
        self.assertIn("--data-checksums", initdb_command)
        self.assertIn("--auth-local=scram-sha-256", initdb_command)
        self.assertIn("--encoding=UTF8", initdb_command)
        self.assertIn("--locale=C.UTF-8", initdb_command)

    def test_initdb_rejects_invalid_configured_arguments(self):
        process_handler = SimpleNamespace(start_process=Mock())

        with tempfile.TemporaryDirectory() as data_dir:
            success, error = initialize_postgres_config_dir_directory(
                process_handler,
                data_dir,
                "DUMB",
                "test-password",
                "--data-checksums 'unterminated",
            )

        self.assertFalse(success)
        self.assertIn("Invalid PostgreSQL initdb_args", error)
        process_handler.start_process.assert_not_called()

    def test_initdb_surfaces_nonzero_exit(self):
        process_handler = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="initdb failed",
            start_process=Mock(return_value=(True, None)),
            wait=Mock(),
        )

        with tempfile.TemporaryDirectory() as data_dir:
            success, error = initialize_postgres_config_dir_directory(
                process_handler,
                data_dir,
                "DUMB",
                "test-password",
            )

        self.assertFalse(success)
        self.assertEqual(error, "PostgreSQL initdb failed: initdb failed")


if __name__ == "__main__":
    unittest.main()
