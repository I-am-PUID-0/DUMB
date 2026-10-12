import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import unittest

TINI = shutil.which("tini")
PROBE = Path(__file__).resolve().parents[1] / "scripts" / "check_container_init.py"


@unittest.skipUnless(sys.platform.startswith("linux") and TINI, "requires Linux Tini")
class ContainerInitTests(unittest.TestCase):
    def cleanup_probe(self, process):
        # Check the exclusive probe group even when its init parent exited:
        # a timeout or failed assertion may leave descendants in that group.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.stdout.close()
        process.stderr.close()

    def run_probe(self, command):
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=20)
            return subprocess.CompletedProcess(
                command, process.returncode, stdout, stderr
            )
        finally:
            self.cleanup_probe(process)

    def run_under_init(self, *arguments):
        # Subreaper mode gives this disposable init ownership of its own
        # orphans without changing the test runner or requiring root/PID 1.
        return self.run_probe([TINI, "-s", "--", sys.executable, "-B", *arguments])

    def test_init_reaps_orphans_without_stealing_concurrent_child_statuses(self):
        result = self.run_under_init(str(PROBE))
        self.assertEqual(0, result.returncode, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual("tini", observed["init_parent"])
        self.assertEqual(16, observed["orphans_reaped"])
        self.assertTrue(observed["adopted_by_target_init"])
        self.assertEqual([0, 7, 19, 31] * 4, observed["child_exit_codes"])

    def test_probe_rejects_reaping_by_an_outer_init(self):
        result = self.run_probe(
            [
                TINI,
                "-s",
                "--",
                TINI,
                "--",
                sys.executable,
                "-B",
                str(PROBE),
                "--iterations",
                "1",
            ]
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("Orphan adoption bypassed the target init", result.stderr)

    def test_init_preserves_the_controller_exit_status(self):
        result = self.run_under_init("-c", "raise SystemExit(29)")
        self.assertEqual(29, result.returncode, result.stderr)

    def test_init_forwards_sigterm_to_the_controller(self):
        child = """
import signal


def stop(signum, frame):
    print(f"received:{signum}", flush=True)
    raise SystemExit(37)


signal.signal(signal.SIGTERM, stop)
print("ready", flush=True)
signal.pause()
"""
        process = subprocess.Popen(
            [TINI, "-s", "--", sys.executable, "-B", "-c", child],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            readable, _, _ = select.select([process.stdout], [], [], 10)
            self.assertTrue(readable, "Controller did not become ready")
            self.assertEqual("ready\n", process.stdout.readline())
            process.terminate()
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(37, process.returncode, stderr)
            self.assertEqual(f"received:{signal.SIGTERM}\n", stdout)
        finally:
            self.cleanup_probe(process)


if __name__ == "__main__":
    unittest.main()
