import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import setup


class FakeProcessHandler:
    def __init__(self):
        self.returncode = 0
        self.stderr = ""
        self.stdout = ""
        self.started = []
        self.waited = []

    def start_process(self, *args, **kwargs):
        self.started.append((args, kwargs))
        return True, None

    def wait(self, process_name):
        self.waited.append(process_name)


class MaintainerrSetupTests(unittest.TestCase):
    def _source_tree(self, root: Path):
        yarn_dir = root / ".yarn" / "releases"
        ui_dist = root / "apps" / "ui" / "dist"
        server_dist = root / "apps" / "server" / "dist"
        server_assets = root / "apps" / "server" / "assets"
        yarn_dir.mkdir(parents=True)
        ui_dist.mkdir(parents=True)
        server_dist.mkdir(parents=True)
        server_assets.mkdir(parents=True)
        (root / "package.json").write_text(
            '{"name":"maintainerr","version":"3.18.0"}', encoding="utf-8"
        )
        (root / "apps" / "server" / "package.json").write_text(
            '{"version":"3.18.0"}', encoding="utf-8"
        )
        (yarn_dir / "yarn-4.17.1.cjs").write_text("", encoding="utf-8")
        (ui_dist / "index.html").write_text(
            '<script src="/__PATH_PREFIX__/assets/app.js"></script>', encoding="utf-8"
        )
        (server_dist / "main.js").write_text(
            "const migrations = '/opt/app/apps/server/dist/database/migrations/**/*.js';"
            " const data = '/opt/data';",
            encoding="utf-8",
        )
        (server_assets / "font.txt").write_text("font", encoding="utf-8")

    def _config(self, root: Path):
        return {
            "enabled": True,
            "process_name": "Maintainerr",
            "config_dir": str(root),
            "port": 6246,
            "log_level": "INFO",
            "branch_enabled": False,
            "exclude_dirs": [],
            "command": [],
            "env": {},
        }

    def test_source_build_uses_pinned_yarn_and_stages_runtime_assets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._source_tree(root)
            process_handler = FakeProcessHandler()
            yarn_cache_root = root / ".yarn-cache"

            with (
                patch.dict(
                    os.environ,
                    {"DUMB_YARN_CACHE_ROOT": str(yarn_cache_root)},
                ),
                patch.object(setup, "_chown_recursive_if_needed"),
                patch.object(
                    setup, "chown_recursive", return_value=(True, None)
                ) as chown_recursive,
            ):
                success, error = setup._build_maintainerr(process_handler, str(root))

            self.assertTrue(success, error)
            chown_recursive.assert_called_once_with(
                str(root), setup.user_id, setup.group_id
            )
            self.assertEqual(
                [
                    "maintainerr_yarn_install",
                    "maintainerr_yarn_build",
                    "maintainerr_yarn_focus",
                    "maintainerr_yarn_rebuild_canvas",
                ],
                process_handler.waited,
            )
            commands = [entry[0][2][2:] for entry in process_handler.started]
            self.assertEqual(
                [
                    ["install", "--immutable"],
                    ["run", "build"],
                    ["workspaces", "focus", "--all", "--production"],
                    ["rebuild", "canvas"],
                ],
                commands,
            )
            rebuild_env = process_handler.started[-1][1]["env"]
            self.assertEqual("true", rebuild_env["npm_config_build_from_source"])
            self.assertEqual(
                str(yarn_cache_root / "maintainerr"),
                rebuild_env["YARN_GLOBAL_FOLDER"],
            )
            staged_html = (
                root / "apps" / "server" / "dist" / "ui" / "index.html"
            ).read_text(encoding="utf-8")
            self.assertNotIn("/__PATH_PREFIX__", staged_html)
            self.assertTrue(
                (root / "apps" / "server" / "dist" / "assets" / "font.txt").is_file()
            )
            server_main = (root / "apps" / "server" / "dist" / "main.js").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(
                "/opt/app/apps/server/dist/database/migrations", server_main
            )
            self.assertIn(
                str(root / "apps" / "server" / "dist" / "database" / "migrations"),
                server_main,
            )
            self.assertNotIn("/opt/data", server_main)
            self.assertIn(str(root / "data"), server_main)

    def test_configure_preserves_data_and_sets_runtime_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._source_tree(root)
            config = self._config(root)
            process_handler = FakeProcessHandler()

            with (
                patch.object(setup.CONFIG_MANAGER, "get", return_value=config),
                patch.object(setup.CONFIG_MANAGER, "save_config") as save_config,
                patch.object(setup, "_chown_recursive_if_needed"),
                patch.object(setup, "_build_maintainerr") as build_maintainerr,
            ):
                success, error = setup.setup_maintainerr(
                    process_handler, configure_only=True
                )

            self.assertTrue(success, error)
            build_maintainerr.assert_not_called()
            self.assertEqual(str(root / "data"), config["env"]["DATA_DIR"])
            self.assertEqual("6246", config["env"]["UI_PORT"])
            self.assertEqual("", config["env"]["BASE_PATH"])
            self.assertEqual("3.18.0", config["env"]["npm_package_version"])
            self.assertEqual(["node", "apps/server/dist/main.js"], config["command"])
            self.assertIn(str(root / "data"), config["exclude_dirs"])
            self.assertTrue((root / "data" / "logs").is_dir())
            self.assertEqual("v3.18.0", (root / "version.txt").read_text())
            save_config.assert_called_once_with("Maintainerr")

    def test_source_tree_does_not_trigger_duplicate_release_bootstrap(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "package.json").write_text("{}", encoding="utf-8")

            self.assertFalse(setup._needs_riven_bootstrap("maintainerr", str(root)))

            (root / "package.json").unlink()
            self.assertTrue(setup._needs_riven_bootstrap("maintainerr", str(root)))


if __name__ == "__main__":
    unittest.main()
