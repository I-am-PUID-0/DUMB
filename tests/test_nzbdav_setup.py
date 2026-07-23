import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils import setup


class NzbDAVSetupTests(unittest.TestCase):
    def test_commit_pin_requires_full_sha_and_normalizes_case(self):
        commit_sha = "A" * 40

        normalized, error = setup._normalize_commit_sha(commit_sha)
        short_value, short_error = setup._normalize_commit_sha("abc1234")

        self.assertEqual("a" * 40, normalized)
        self.assertIsNone(error)
        self.assertIsNone(short_value)
        self.assertIn("40-character hexadecimal", short_error)

    def test_commit_pin_takes_precedence_over_release_and_branch(self):
        commit_sha = "a" * 40
        config = {
            "enabled": True,
            "process_name": "NzbDAV",
            "config_dir": "/nzbdav",
            "release_version_enabled": True,
            "release_version": "latest",
            "commit_sha": commit_sha,
            "branch_enabled": True,
            "branch": "main",
            "env": {},
        }
        process_handler = Mock()
        process_handler.setup_tracker = set()
        process_handler.setup_tracker_lock = threading.Lock()

        with (
            patch.object(
                setup.CONFIG_MANAGER,
                "find_key_for_process",
                return_value=("nzbdav", None),
            ),
            patch.object(setup.CONFIG_MANAGER, "get_instance", return_value=config),
            patch.object(
                setup, "setup_branch_version", return_value=(True, None)
            ) as install_source,
            patch.object(setup, "setup_release_version") as install_release,
            patch.object(setup, "setup_nzbdav", return_value=(True, None)),
        ):
            success, error = setup.install_project(process_handler, "NzbDAV")

        self.assertTrue(success, error)
        install_source.assert_called_once_with(
            process_handler, config, "NzbDAV", "nzbdav"
        )
        install_release.assert_not_called()

    def _write_start_script(self, root: Path) -> Path:
        frontend_dir = root / "frontend"
        frontend_dir.mkdir()
        with patch.object(setup, "chown_single"):
            script_path = setup._write_nzbdav_start_script(
                str(root),
                [str(root / "backend")],
                str(frontend_dir),
                8080,
            )
        return Path(script_path)

    def test_start_script_runs_frontend_before_blocking_migration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script = self._write_start_script(Path(tmpdir)).read_text(encoding="utf-8")

        frontend_start = script.index("node dist-node/server.js &")
        migration_start = script.index(" --db-migration")
        backend_start = script.index(" &\nBACKEND_PID=$!", migration_start)

        self.assertLess(script.index("trap terminate TERM INT"), migration_start)
        self.assertLess(frontend_start, migration_start)
        self.assertLess(migration_start, backend_start)
        self.assertIn("MIGRATION_EXIT_CODE=$?", script)
        self.assertIn('exit "$MIGRATION_EXIT_CODE"', script)

    def test_migration_failure_stops_frontend_and_preserves_exit_code(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state_dir = root / "state"
            state_dir.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()

            backend = root / "backend"
            backend.write_text(
                """#!/bin/sh
if [ "${1:-}" = "--db-migration" ]; then
  count=0
  while [ ! -f "$TEST_STATE/frontend.started" ] && [ "$count" -lt 100 ]; do
    sleep 0.01
    count=$((count + 1))
  done
  [ -f "$TEST_STATE/frontend.started" ] || exit 99
  touch "$TEST_STATE/migration.started"
  exit 23
fi
touch "$TEST_STATE/backend.started"
""",
                encoding="utf-8",
            )
            backend.chmod(0o755)

            node = bin_dir / "node"
            node.write_text(
                """#!/bin/sh
touch "$TEST_STATE/frontend.started"
trap 'touch "$TEST_STATE/frontend.stopped"; exit 0' TERM INT
while :; do sleep 0.05; done
""",
                encoding="utf-8",
            )
            node.chmod(0o755)
            script = self._write_start_script(root)

            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["TEST_STATE"] = str(state_dir)
            result = subprocess.run(
                ["/bin/sh", str(script)],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(23, result.returncode, result.stderr)
            self.assertTrue((state_dir / "migration.started").exists())
            self.assertTrue((state_dir / "frontend.stopped").exists())
            self.assertFalse((state_dir / "backend.started").exists())


if __name__ == "__main__":
    unittest.main()
