"""Disposable helpers prove long-work, output, cancellation and group ownership."""

from itertools import count
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from utils import infinidysk_v2_subprocess as watched


class Cancelled(Exception):
    pass


class MigrationProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def command(self, body):
        return [sys.executable, "-B", "-c", body]

    def test_quiet_long_work_warns_but_is_allowed_to_finish(self):
        ticks = count(0, 3600)
        reports = []
        result = watched.run_migration_process(
            self.command("import time; time.sleep(.08); print('done')"),
            idle_warning_seconds=30,
            poll_interval=0.01,
            progress=reports.append,
            clock=lambda: next(ticks),
        )
        self.assertEqual(result.stdout, b"done\n")
        self.assertTrue(any(row["stall_warning"] for row in reports))
        self.assertGreater(reports[-1]["elapsed_seconds"], 1800)

    def test_outputs_are_drained_with_bounded_memory(self):
        result = watched.run_migration_process(
            self.command(
                "import os; os.write(2, b'x'*150000); os.write(1,b'verified')"
            ),
            stderr_limit=4096,
            poll_interval=0.01,
        )
        self.assertEqual(result.stdout, b"verified")
        self.assertEqual(result.stderr, b"x" * 4096)

    def test_oversized_stdout_is_refused_and_worker_reaped(self):
        processes = []

        def spawn(*args, **kwargs):
            process = subprocess.Popen(*args, **kwargs)
            processes.append(process)
            return process

        with self.assertRaisesRegex(watched.MigrationProcessError, "output budget"):
            watched.run_migration_process(
                self.command("import os,time; os.write(1,b'x'*65536); time.sleep(60)"),
                stdout_limit=32,
                poll_interval=0.01,
                popen=spawn,
            )
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(
            watched.migration_process_cleanup_complete(
                {"process_group": processes[0].pid}
            )
        )

    def test_cancel_before_spawn_preserves_original_exception(self):
        cancelled = Cancelled("cancel requested")
        spawn = Mock()
        with self.assertRaises(Cancelled) as caught:
            watched.run_migration_process(
                self.command("raise AssertionError('must not spawn')"),
                check_cancel=Mock(side_effect=cancelled),
                popen=spawn,
            )
        self.assertIs(caught.exception, cancelled)
        spawn.assert_not_called()

    def test_cancel_terminates_owned_helper_and_its_descendant(self):
        marker = self.root / "child.pid"
        body = (
            "import subprocess,sys,time,pathlib;"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
            f"pathlib.Path({str(marker)!r}).write_text(str(child.pid));"
            "time.sleep(60)"
        )
        processes = []
        cancelled = Cancelled("cancel requested")

        def spawn(*args, **kwargs):
            process = subprocess.Popen(*args, **kwargs)
            processes.append(process)
            return process

        def check():
            if marker.exists():
                raise cancelled

        with self.assertRaises(Cancelled) as caught:
            watched.run_migration_process(
                self.command(body),
                check_cancel=check,
                popen=spawn,
                poll_interval=0.01,
            )
        self.assertIs(caught.exception, cancelled)
        self.assertTrue(marker.exists())
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(
            watched.migration_process_cleanup_complete(
                {"process_group": processes[0].pid}
            )
        )

    def test_success_also_terminates_a_stranded_descendant(self):
        result = watched.run_migration_process(
            self.command(
                "import subprocess,sys;"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
                "print(child.pid,flush=True)"
            ),
            poll_interval=0.01,
        )
        child = int(result.stdout)
        path = Path("/proc") / str(child) / "stat"
        try:
            state = path.read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            state = None
        self.assertIn(state, (None, "Z"))

    def test_failure_never_exposes_command_output(self):
        with self.assertRaises(watched.MigrationProcessError) as caught:
            watched.run_migration_process(
                self.command(
                    "import sys;print('reserved-private-fixture',file=sys.stderr);sys.exit(1)"
                ),
                poll_interval=0.01,
            )
        self.assertNotIn("reserved-private-fixture", str(caught.exception))

    def test_activity_observes_literal_file_metadata_without_following_links(self):
        regular = self.root / "progress"
        link = self.root / "alias"
        regular.write_text("one")
        link.symlink_to(regular)
        first = watched._activity((regular, link))
        regular.write_text("more progress")
        second = watched._activity((regular, link))
        self.assertNotEqual(first[0], second[0])
        self.assertIsNone(first[1])
        self.assertIsNone(second[1])

    def test_unreadable_membership_is_not_treated_as_cleanup_proof(self):
        entry, detail = MagicMock(), Mock()
        entry.name = "123"
        entry.__truediv__ = Mock(return_value=detail)
        detail.read_bytes.side_effect = PermissionError("fixture access denied")
        with patch.object(watched.Path, "iterdir", return_value=[entry]):
            self.assertFalse(
                watched.migration_process_cleanup_complete({"process_group": 123})
            )

    def test_recovery_cleanup_probe_is_read_only_and_fail_closed(self):
        with patch.object(watched.os, "killpg") as signal:
            for evidence in (None, {}, {"process_group": -1}, {"process_group": True}):
                self.assertFalse(watched.migration_process_cleanup_complete(evidence))
            with patch.object(watched, "_live_group", return_value=[123]):
                self.assertFalse(
                    watched.migration_process_cleanup_complete({"process_group": 123})
                )
            with patch.object(watched, "_live_group", return_value=[]):
                self.assertTrue(
                    watched.migration_process_cleanup_complete({"process_group": 123})
                )
            with patch.object(watched, "_live_group", side_effect=OSError):
                self.assertFalse(
                    watched.migration_process_cleanup_complete({"process_group": 123})
                )
            signal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
