import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import Mock, patch

from utils import cli_debrid_runtime as runtime, processes, setup
from utils.dependency_map import build_conditional_dependency_map


class CliDebridRuntimeTests(unittest.TestCase):
    def _source(self, root, topology):
        package = root / "cli_battery" / "app"
        package.mkdir(parents=True, exist_ok=True)
        entry = root / "cli_battery" / "main.py"
        if entry.exists():
            entry.unlink()
        if topology == "standalone":
            entry.write_text("# standalone\n", encoding="utf-8")
        (package / "__init__.py").write_text(
            (
                "from .direct_api import DirectAPI\n__all__ = ['DirectAPI']\n"
                if topology == "embedded"
                else "def create_app(): pass\n"
            ),
            encoding="utf-8",
        )
        (package / "direct_api.py").write_text(
            "class DirectAPI: pass\n", encoding="utf-8"
        )

    def _configs(self, root):
        cli = {"enabled": True, "process_name": "CLI Debrid", "config_dir": str(root)}
        battery = {
            "enabled": True,
            "process_name": "CLI Battery",
            "config_dir": str(root),
            "command": [str(root / "venv/bin/python"), "cli_battery/main.py"],
        }
        return cli, battery

    def test_topology_uses_installed_contract_not_configured_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli, battery = self._configs(root)
            cli.update(release_version_enabled=True, release_version="v0.7.81")
            self._source(root, "standalone")
            self.assertFalse(runtime.cli_battery_is_embedded(cli, battery))
            self._source(root, "embedded")
            cli["release_version"] = "v0.7.23"
            self.assertTrue(runtime.cli_battery_is_embedded(cli, battery))
            self._source(root, "broken-legacy")
            self.assertEqual("unknown", runtime.cli_battery_topology(cli))
            self.assertFalse(runtime.cli_battery_is_embedded(cli, battery))

    def test_custom_battery_command_or_source_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli, battery = self._configs(Path(tmp))
            self._source(Path(tmp), "embedded")
            for custom in (
                {**battery, "command": ["python", "operator_battery.py"]},
                {**battery, "command": ["/custom/python", "cli_battery/main.py"]},
                {**battery, "config_dir": str(Path(tmp, "separate"))},
            ):
                self.assertFalse(runtime.cli_battery_is_embedded(cli, custom))

    def test_embedded_contract_wins_over_retained_entrypoint_but_legacy_factory_does_not(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli, battery = self._configs(root)
            self._source(root, "embedded")
            (root / "cli_battery/main.py").write_text(
                "from app import create_app\n", encoding="utf-8"
            )
            self.assertTrue(runtime.cli_battery_is_embedded(cli, battery))
            (root / "cli_battery/app/__init__.py").write_text(
                "from .direct_api import DirectAPI\ndef create_app(): pass\n",
                encoding="utf-8",
            )
            self.assertEqual("standalone", runtime.cli_battery_topology(cli))

    def test_dependency_map_changes_after_source_upgrade_and_downgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli, battery = self._configs(root)
            config = {"cli_debrid": cli, "cli_battery": battery}
            for topology in ("standalone", "embedded", "standalone", "unknown"):
                self._source(root, topology)
                deps = build_conditional_dependency_map(lambda key: config.get(key, {}))
                self.assertEqual(
                    topology != "embedded",
                    "cli_battery" in deps.get("cli_debrid", set()),
                )

    def test_startup_skips_embedded_child_without_changing_enabled_setting(self):
        tree = ast.parse(
            Path(setup.__file__).parents[1].joinpath("main.py").read_text()
        )
        wanted = {
            "_collect_preinstall_targets",
            "_collect_expected_process_names",
            "start_configured_process",
        }
        selected = ast.Module(
            body=[
                n
                for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name in wanted
            ],
            type_ignores=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            cli, battery = self._configs(Path(tmp))
            self._source(Path(tmp), "embedded")
            config = {"cli_debrid": cli, "cli_battery": battery}
            manager = SimpleNamespace(
                config=config, get=lambda key, default=None: config.get(key, default)
            )
            namespace = {
                "cli_battery_is_embedded": runtime.cli_battery_is_embedded,
                "config": manager,
                "logger": Mock(),
            }
            exec(compile(selected, "main.py", "exec"), namespace)  # noqa: S102
            self.assertEqual(
                [("cli_debrid", "CLI Debrid")],
                namespace["_collect_preinstall_targets"](manager),
            )
            self.assertEqual(
                ["CLI Debrid"],
                namespace["_collect_expected_process_names"](manager, list(config)),
            )
            updater = Mock()
            self.assertTrue(
                namespace["start_configured_process"](battery, updater, "cli_battery")
            )
            updater.auto_update.assert_not_called()
            self.assertTrue(battery["enabled"])

    def test_manual_battery_launch_rejects_embedded_and_broken_source_before_setup(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli, battery = self._configs(root)
            config = {"cli_debrid": cli, "cli_battery": battery}
            handler = object.__new__(processes.ProcessHandler)
            handler._start_process_admitted = Mock()
            for topology in ("embedded", "unknown"):
                self._source(root, topology)
                with (
                    patch.object(
                        processes.CONFIG_MANAGER,
                        "find_key_for_process",
                        return_value=("cli_battery", None),
                    ),
                    patch.object(
                        processes.CONFIG_MANAGER,
                        "get",
                        side_effect=lambda key, default=None: config.get(key, default),
                    ),
                ):
                    success, error = handler.start_process("CLI Battery")
                self.assertFalse(success)
                self.assertIn(
                    "embedded" if topology == "embedded" else "could not be verified",
                    error,
                )
            handler._start_process_admitted.assert_not_called()

    def test_cli_launch_reconciles_running_legacy_battery_after_upgrade_and_downgrade(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli, battery = self._configs(root)
            handler = object.__new__(processes.ProcessHandler)
            handler._prefixed_name = lambda name: name
            handler.process_names = {"CLI Battery": 123}
            handler.stop_process = Mock(
                side_effect=lambda name: handler.process_names.pop(name)
            )
            handler.start_process = Mock(return_value=(True, None))
            with patch.object(processes.CONFIG_MANAGER, "get", return_value=battery):
                self._source(root, "embedded")
                self.assertEqual(
                    (True, None), handler._reconcile_cli_battery_runtime(cli)
                )
                handler.stop_process.assert_called_once_with("CLI Battery")
                self._source(root, "standalone")
                self.assertEqual(
                    (True, None), handler._reconcile_cli_battery_runtime(cli)
                )
                handler.start_process.assert_called_once_with("CLI Battery")
                battery["enabled"] = False
                handler.start_process.reset_mock()
                self.assertEqual(
                    (True, None), handler._reconcile_cli_battery_runtime(cli)
                )
                handler.start_process.assert_not_called()

    def test_api_graph_omits_embedded_child_but_onboarding_keeps_legacy_option(self):
        tree = ast.parse(
            Path(setup.__file__)
            .parents[1]
            .joinpath("api/routers/process.py")
            .read_text()
        )
        functions = {"_effective_core_dependencies", "_onboarding_core_dependencies"}
        body = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in functions
        ]
        core = next(
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "CORE_SERVICE_DEPENDENCIES"
                for target in node.targets
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            cli, battery = self._configs(Path(tmp))
            config = {"cli_debrid": cli, "cli_battery": battery}
            namespace = {
                "Optional": Optional,
                "Dict": Dict,
                "Any": Any,
                "List": List,
                "CORE_SERVICE_DEPENDENCIES": ast.literal_eval(core),
                "CONFIG_MANAGER": SimpleNamespace(
                    get=lambda key, default=None: config.get(key, default)
                ),
                "cli_battery_is_embedded": runtime.cli_battery_is_embedded,
            }
            exec(  # noqa: S102
                compile(ast.Module(body=body, type_ignores=[]), "process.py", "exec"),
                namespace,
            )
            for topology in ("standalone", "embedded", "unknown"):
                self._source(Path(tmp), topology)
                self.assertEqual(
                    topology != "embedded",
                    "cli_battery"
                    in namespace["_effective_core_dependencies"]("cli_debrid", cli),
                )
                self.assertIn(
                    "cli_battery",
                    namespace["_onboarding_core_dependencies"]("cli_debrid", cli),
                )

    def test_prelaunch_repairs_data_after_setup_tracker_skip_and_refuses_redirect(self):
        for redirected in (False, True):
            with (
                self.subTest(redirected=redirected),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root, persistent = Path(tmp, "runtime"), Path(tmp, "persistent")
                root.mkdir()
                persistent.mkdir()
                if redirected:
                    foreign = Path(tmp, "foreign")
                    foreign.mkdir()
                    (root / "data").symlink_to(foreign)
                config = {
                    **self._data_config(root),
                    "process_name": "CLI Debrid",
                    "command": [sys.executable, "main.py"],
                    "suppress_logging": True,
                }
                values = {
                    "puid": os.getuid(),
                    "pgid": os.getgid(),
                    "data_root": str(persistent),
                    "cli_debrid": config,
                    "cli_battery": {},
                }
                handler = object.__new__(processes.ProcessHandler)
                with patch.object(processes.ProcessHandler, "_write_startup_state"):
                    handler.init_attributes(Mock())
                handler.setup_tracker.add("CLI Debrid")

                def popen_probe(*args, **kwargs):
                    self.assertEqual(
                        persistent / "cli_debrid", (root / "data").resolve()
                    )
                    self.assertTrue((root / "data/config").is_dir())
                    raise RuntimeError("probe stopped before spawning")

                with (
                    patch.object(
                        processes.CONFIG_MANAGER,
                        "find_key_for_process",
                        return_value=("cli_debrid", None),
                    ),
                    patch.object(
                        processes.CONFIG_MANAGER, "get_instance", return_value=config
                    ),
                    patch.object(
                        processes.CONFIG_MANAGER,
                        "get",
                        side_effect=lambda key, default=None: values.get(key, default),
                    ),
                    patch.object(
                        processes.subprocess, "Popen", side_effect=popen_probe
                    ) as popen,
                    patch.object(processes, "notify_event"),
                ):
                    success, error = handler._start_process_admitted("CLI Debrid")
                self.assertFalse(success)
                if redirected:
                    self.assertIn("Refusing redirected", error)
                    popen.assert_not_called()
                else:
                    self.assertIn("probe stopped before spawning", error)
                    popen.assert_called_once()

    def _data_config(self, root):
        return {
            "config_dir": str(root),
            "config_file": str(root / "data/config/config.json"),
            "log_file": str(root / "data/logs/debug.log"),
            "env": {
                "USER_CONFIG": str(root / "data/config"),
                "USER_LOGS": str(root / "data/logs"),
                "USER_DB_CONTENT": str(root / "data/db_content"),
            },
        }

    def test_missing_data_alias_is_repaired_and_generated_assets_are_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            outer.chmod(0o755)
            root, persistent = outer / "runtime", outer / "persistent"
            (root / "utilities").mkdir(parents=True)
            (root / "database").mkdir()
            (root / "database/collection_poster_renderer.py").write_text("# renderer\n")
            persistent.mkdir()
            existing_files = [
                persistent / "cli_debrid/config/config.json",
                persistent / "cli_debrid/logs/debug.log",
                persistent / "cli_debrid/db_content/library.db",
                root / "static/img/collection_poster_previews/default.jpg",
                root / "utilities/probe",
            ]
            for path in existing_files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("root-owned-before-repair")
                path.chmod(0o600)
            foreign = outer / "foreign"
            foreign.write_text("keep")
            before = foreign.stat()
            (root / "utilities/foreign-link").symlink_to(foreign)
            config = self._data_config(root)
            uid, gid = (1000, 1000) if os.geteuid() == 0 else (os.getuid(), os.getgid())
            success, error = runtime.prepare_cli_debrid_runtime(
                config, str(persistent), uid, gid
            )
            self.assertTrue(success, error)
            self.assertEqual(
                (persistent / "cli_debrid").resolve(), (root / "data").resolve()
            )
            self.assertEqual(
                (before.st_uid, before.st_gid),
                (foreign.stat().st_uid, foreign.stat().st_gid),
            )
            paths = existing_files
            argv = [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; [Path(p).write_text('writable') for p in sys.argv[1:]]",
                *map(str, paths),
            ]
            identity = (
                {"user": uid, "group": gid, "extra_groups": []}
                if os.geteuid() == 0
                else {}
            )
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=10, **identity
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(all(path.read_text() == "writable" for path in paths))

    def test_redirected_managed_data_children_are_refused_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, persistent, foreign = [
                Path(tmp, name) for name in ("runtime", "persistent", "foreign")
            ]
            root.mkdir()
            foreign.mkdir()
            (foreign / "state").write_text("keep")
            leaf = persistent / "cli_debrid/config"
            leaf.mkdir(parents=True)
            (leaf / "foreign-link").symlink_to(foreign, target_is_directory=True)
            with patch.object(runtime.os, "chown") as chown:
                success, error = runtime.prepare_cli_debrid_runtime(
                    self._data_config(root), str(persistent), os.getuid(), os.getgid()
                )
            self.assertFalse(success)
            self.assertIn("Refusing redirected", error)
            self.assertFalse((root / "data").exists())
            self.assertEqual("keep", (foreign / "state").read_text())
            chown.assert_not_called()

    def test_writable_runtime_tree_skips_file_mounts_without_owning_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            leaf = root / "utilities"
            leaf.mkdir()
            mounted = leaf / "mounted.py"
            mounted.write_text("foreign")
            with (
                patch.object(
                    runtime.os.path,
                    "ismount",
                    side_effect=lambda path: Path(path) == mounted,
                ),
                patch.object(runtime, "_own_entry") as own,
            ):
                success, error = runtime.prepare_cli_debrid_runtime(
                    {"config_dir": str(root)}, "/unused", os.getuid(), os.getgid()
                )
            self.assertTrue(success, error)
            self.assertNotIn(mounted, [call.args[0] for call in own.call_args_list])

    def test_foreign_data_locations_are_refused_without_replacement_or_chown(self):
        for topology in ("real-data", "redirect"):
            with self.subTest(topology=topology), tempfile.TemporaryDirectory() as tmp:
                root, persistent, foreign = [
                    Path(tmp, name) for name in ("runtime", "persistent", "foreign")
                ]
                root.mkdir()
                persistent.mkdir()
                foreign.mkdir()
                (foreign / "state").write_text("keep")
                alias = root / "data"
                if topology == "redirect":
                    alias.symlink_to(foreign, target_is_directory=True)
                else:
                    alias.mkdir()
                    (alias / "state").write_text("keep")
                with patch.object(runtime.os, "chown") as chown:
                    success, error = runtime.prepare_cli_debrid_runtime(
                        self._data_config(root),
                        str(persistent),
                        os.getuid(),
                        os.getgid(),
                    )
                self.assertFalse(success)
                self.assertIn("Refusing", error)
                self.assertEqual("keep", (alias / "state").read_text())
                self.assertFalse((persistent / "cli_debrid").exists())
                chown.assert_not_called()

    def test_explicit_custom_paths_leave_unused_runtime_data_redirect_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, custom = Path(tmp, "runtime"), Path(tmp, "custom")
            root.mkdir()
            custom.mkdir()
            (root / "data").symlink_to(custom)
            config = {
                "config_dir": str(root),
                "env": {
                    "USER_CONFIG": str(custom / "config"),
                    "USER_LOGS": str(custom / "logs"),
                    "USER_DB_CONTENT": str(custom / "db"),
                },
            }
            success, error = runtime.prepare_cli_debrid_runtime(
                config, str(Path(tmp, "unused")), os.getuid(), os.getgid()
            )
            self.assertTrue(success, error)
            self.assertTrue((root / "data").is_symlink())
            self.assertEqual(custom, (root / "data").resolve())
            self.assertTrue(
                all((custom / name).is_dir() for name in ("config", "logs", "db"))
            )

    def test_post_install_preparation_repairs_alias_after_release_and_branch_extracts(
        self,
    ):
        for source_kind in ("release", "branch"):
            with (
                self.subTest(source_kind=source_kind),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root, persistent = Path(tmp, "runtime"), Path(tmp, "persistent")
                root.mkdir()
                persistent.mkdir()
                target = persistent / "cli_debrid"
                target.mkdir()
                (target / "state").write_text("keep")
                (root / "data").symlink_to(target)
                config = {
                    **self._data_config(root),
                    "repo_owner": "example",
                    "repo_name": "cli-fork",
                    "release_version": "v0.7.81",
                    "platforms": ["python"],
                    "branch": "main",
                    "branch_enabled": source_kind == "branch",
                    "clear_on_update": False,
                }

                def extract(*args, **kwargs):
                    (root / "data").unlink()
                    return True, None

                with (
                    patch.object(
                        setup, "_validate_cli_debrid_source", return_value=(True, None)
                    ),
                    patch.object(setup, "setup_environment", return_value=(True, None)),
                    patch.object(setup, "user_id", os.getuid()),
                    patch.object(setup, "group_id", os.getgid()),
                    patch.object(
                        setup.CONFIG_MANAGER, "get", return_value=str(persistent)
                    ),
                    patch.object(
                        setup.downloader,
                        "download_release_version",
                        side_effect=extract,
                    ),
                    patch.object(
                        setup.downloader,
                        "get_branch",
                        return_value=("https://example.test/source.zip", "source"),
                    ),
                    patch.object(
                        setup.downloader,
                        "get_ref_commit_sha",
                        return_value=(None, "offline"),
                    ),
                    patch.object(
                        setup.downloader, "download_and_extract", side_effect=extract
                    ),
                    patch.object(setup.versions, "version_write"),
                ):
                    install = (
                        setup.setup_release_version
                        if source_kind == "release"
                        else setup.setup_branch_version
                    )
                    success, error = install(Mock(), config, "CLI Debrid", "cli_debrid")
                self.assertTrue(success, error)
                self.assertEqual(target, (root / "data").resolve())
                self.assertEqual("keep", (root / "data/state").read_text())


if __name__ == "__main__":
    unittest.main()
