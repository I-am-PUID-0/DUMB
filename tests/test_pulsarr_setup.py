import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import setup


class FakeProcessHandler:
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout
        self.started = []
        self.waited = []

    def start_process(self, *args, **kwargs):
        self.started.append((args, kwargs))
        return True, None

    def wait(self, process_name):
        self.waited.append(process_name)


class PulsarrSetupTests(unittest.TestCase):
    def _pulsarr_tree(self, root: Path):
        (root / "dist").mkdir(parents=True)
        (root / "migrations").mkdir()
        (root / "package.json").write_text("{}", encoding="utf-8")
        (root / "dist" / "server.js").write_text("", encoding="utf-8")
        (root / "migrations" / "migrate.ts").write_text("", encoding="utf-8")

    def _config(self, root: Path):
        return {
            "enabled": True,
            "process_name": "Pulsarr",
            "config_dir": str(root),
            "port": 3003,
            "platforms": ["bun"],
            "command": [],
            "env": {},
        }

    def test_configure_runs_idempotent_database_migrations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._pulsarr_tree(root)
            config = self._config(root)
            process_handler = FakeProcessHandler()

            with (
                patch.object(setup.CONFIG_MANAGER, "get", return_value=config),
                patch.object(setup.CONFIG_MANAGER, "save_config"),
                patch.object(setup, "_chown_recursive_if_needed"),
                patch.dict(os.environ, {"BUN_INSTALL": "/test/bun"}),
            ):
                success, error = setup.setup_pulsarr(
                    process_handler, configure_only=True
                )

            self.assertTrue(success, error)
            self.assertEqual(["bun_migrate"], process_handler.waited)
            args, kwargs = process_handler.started[0]
            self.assertEqual("bun_migrate", args[0])
            self.assertEqual(str(root), args[1])
            self.assertEqual(
                [
                    "/test/bun/bin/bun",
                    "run",
                    "--bun",
                    "migrations/migrate.ts",
                ],
                args[2],
            )
            self.assertEqual(
                str(root / "data" / "db" / "pulsarr.db"), kwargs["env"]["dbPath"]
            )

    def test_configure_surfaces_migration_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._pulsarr_tree(root)
            config = self._config(root)
            process_handler = FakeProcessHandler(
                returncode=1, stderr="migration 001 failed"
            )

            with (
                patch.object(setup.CONFIG_MANAGER, "get", return_value=config),
                patch.object(setup.CONFIG_MANAGER, "save_config"),
                patch.object(setup, "_chown_recursive_if_needed"),
                patch.dict(os.environ, {"BUN_INSTALL": "/test/bun"}),
            ):
                success, error = setup.setup_pulsarr(
                    process_handler, configure_only=True
                )

            self.assertFalse(success)
            self.assertIn("Pulsarr database migration failed", error)
            self.assertIn("migration 001 failed", error)


if __name__ == "__main__":
    unittest.main()
