import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import setup


class NzbDAVSetupTests(unittest.TestCase):
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
