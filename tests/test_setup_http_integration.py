"""Real FastAPI integration runs outside legacy global dependency stubs."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


class SetupHttpIntegrationTests(unittest.TestCase):
    def test_router_runtime_and_fresh_materialization_contracts(self):
        root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(root)
        result = subprocess.run(
            [sys.executable, str(root / "tests/support/setup_http_fixture.py")],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 15 tests", result.stderr)
        self.assertIn("OK", result.stderr)
