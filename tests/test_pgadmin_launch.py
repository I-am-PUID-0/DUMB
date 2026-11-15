"""Portable pgAdmin restart commands retain argv, identity and launch context."""

from contextlib import contextmanager, ExitStack
from copy import deepcopy
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from utils import postgres as postgres_utils
from utils.pgadmin_launch import pgadmin_command_template, pgadmin_launch_command


class PgAdminLaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.venv = self.root / "runtime with 'quotes'"
        (self.venv / "bin").mkdir(parents=True)
        (self.venv / "bin/python3").symlink_to(sys.executable)
        self.script = self.venv / "lib/python3.11/site-packages/pgadmin4/pgAdmin4.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text(
            "import time\nfrom pathlib import Path\n"
            "Path('started').write_text('ready')\ntime.sleep(30)\n"
        )
        self.directory = self.root / "configuration with spaces"
        self.directory.mkdir()
        runtime = patch("utils.pgadmin_launch.PGADMIN_VENV", str(self.venv))
        runtime.start()
        self.addCleanup(runtime.stop)
        runtime = patch.object(postgres_utils, "PGADMIN_VENV", str(self.venv))
        runtime.start()
        self.addCleanup(runtime.stop)
        self.template = pgadmin_command_template()
        self.resolved = [self.template[0], str(self.script)]

    def test_portable_string_and_list_resolve_one_managed_token_with_literal_args(self):
        arguments = ["argument with spaces", "*.conf", "{literal}", "quote'value"]
        portable = self.template + arguments
        for command in (portable, tuple(portable), shlex.join(portable)):
            with self.subTest(command_type=type(command).__name__):
                self.assertEqual(
                    pgadmin_launch_command(command), self.resolved + arguments
                )
        self.assertIn("python*", portable[1])
        self.assertEqual(portable, self.template + arguments)

    def test_custom_python_commands_and_explicit_shell_commands_remain_literal(self):
        commands = [
            ["/custom/python3", self.template[1], "*.literal"],
            [self.template[0], "/custom/web.py", "argument with spaces"],
            ["sh", "-c", "exec " + shlex.join(self.template)],
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(pgadmin_launch_command(command), command)
                self.assertIsNot(pgadmin_launch_command(command), command)
                self.assertEqual(pgadmin_launch_command(shlex.join(command)), command)

    def test_missing_or_ambiguous_managed_runtime_is_refused(self):
        self.script.unlink()
        with self.assertRaisesRegex(ValueError, "unique web entrypoint"):
            pgadmin_launch_command(self.template)
        self.script.write_text("pass\n")
        other = self.venv / "lib/python3.12/site-packages/pgadmin4/pgAdmin4.py"
        other.parent.mkdir(parents=True)
        other.write_text("pass\n")
        with self.assertRaisesRegex(ValueError, "unique web entrypoint"):
            pgadmin_launch_command(self.template)
        other.unlink()
        (self.venv / "bin/python3").unlink()
        with self.assertRaisesRegex(ValueError, "Python runtime"):
            pgadmin_launch_command(self.template)

    @contextmanager
    def handler(self, *, key="pgadmin", template=None, real=False):
        from utils.processes import ProcessHandler

        handler = object.__new__(ProcessHandler)
        handler.init_attributes(Mock())
        handler.setup_tracker.add("pgAdmin4")
        handler._update_running_processes_file = Mock()
        if not real:
            handler._check_immediate_exit_and_log = Mock(return_value=(True, None))
        configuration = {
            "process_name": "pgAdmin4",
            "config_dir": str(self.directory),
            "command": deepcopy(self.template if template is None else template),
            "env": {"PROFILE_CONTEXT": "present", "OVERRIDE": "profile"},
        }
        before = deepcopy(configuration)
        children = []
        native_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            child = native_popen(*args, **kwargs)
            children.append(child)
            return child

        def config_get(field, default=None):
            if field == "puid":
                return os.geteuid()
            if field == "pgid":
                return os.getegid()
            return default if default is not None else {}

        with ExitStack() as stack:
            for context in [
                patch(
                    "utils.processes.CONFIG_MANAGER.find_key_for_process",
                    return_value=(key, None),
                ),
                patch(
                    "utils.processes.CONFIG_MANAGER.get_instance",
                    return_value=configuration,
                ),
                patch("utils.processes.CONFIG_MANAGER.get", side_effect=config_get),
                patch(
                    "utils.infinidysk_migration_admission.infinidysk_recovery_blocks_service",
                    return_value=None,
                ),
                patch("utils.processes.SubprocessLogger"),
                patch("utils.processes.notify_event"),
                patch(
                    "utils.setup.setup_project",
                    side_effect=AssertionError("Restart must not reinstall pgAdmin"),
                ),
            ]:
                stack.enter_context(context)
            if real:
                popen = stack.enter_context(
                    patch("utils.processes.subprocess.Popen", side_effect=spawn)
                )
            else:
                process = Mock(pid=1234)
                process.poll.return_value = None
                popen = stack.enter_context(
                    patch("utils.processes.subprocess.Popen", return_value=process)
                )
            try:
                yield handler, configuration, popen, children
                self.assertEqual(configuration, before)
            finally:
                # Only these dummy children belong to this fixture. Reap every
                # owned process/group even if a startup assertion raises.
                for child in children:
                    try:
                        if child.poll() is None:
                            os.killpg(child.pid, signal.SIGTERM)
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait(timeout=5)
                    finally:
                        for stream in (child.stdout, child.stderr):
                            if stream:
                                stream.close()
                    self.assertIsNotNone(child.poll())
                    with self.assertRaises(ProcessLookupError):
                        os.killpg(child.pid, 0)

    def test_name_only_legacy_restart_resolves_without_changing_saved_template(self):
        arguments = ["argument with spaces", "*.literal"]
        portable = shlex.join(self.template + arguments)
        with self.handler(template=portable) as (handler, config, popen, _):
            success, error = handler.start_process("pgAdmin4")
            self.assertTrue(success, error)
            self.assertEqual(popen.call_args.args[0], self.resolved + arguments)
            self.assertEqual(popen.call_args.kwargs["cwd"], config["config_dir"])
            self.assertFalse(popen.call_args.kwargs.get("shell", False))
            self.assertEqual(config["command"], portable)

    def test_missing_directory_keeps_explicit_argv_and_requested_environment(self):
        command = ["/custom/pgadmin-python", "/custom/app.py", "*.literal"]
        with self.handler() as (handler, config, popen, _):
            success, error = handler.start_process(
                "pgAdmin4",
                command=command,
                env={"REQUEST_CONTEXT": "present", "OVERRIDE": "request"},
            )
            self.assertTrue(success, error)
            self.assertEqual(popen.call_args.args[0], command)
            self.assertEqual(popen.call_args.kwargs["cwd"], config["config_dir"])
            environment = popen.call_args.kwargs["env"]
            self.assertEqual(environment["REQUEST_CONTEXT"], "present")
            self.assertEqual(environment["PROFILE_CONTEXT"], "present")
            self.assertEqual(environment["OVERRIDE"], "request")

    def test_explicit_directory_and_other_service_commands_remain_unchanged(self):
        override = str(self.root / "explicit working directory")
        with self.handler() as (handler, _config, popen, _):
            success, error = handler.start_process("pgAdmin4", config_dir=override)
            self.assertTrue(success, error)
            self.assertEqual(popen.call_args.args[0], self.resolved)
            self.assertEqual(popen.call_args.kwargs["cwd"], override)
        with self.handler(key="example") as (handler, config, popen, _):
            success, error = handler.start_process("pgAdmin4")
            self.assertTrue(success, error)
            self.assertEqual(popen.call_args.args[0], config["command"])

    def test_unavailable_runtime_never_starts_a_child(self):
        self.script.unlink()
        with self.handler() as (handler, _config, popen, _):
            success, error = handler.start_process("pgAdmin4")
            self.assertFalse(success)
            self.assertIn("unique web entrypoint", error)
            popen.assert_not_called()

    def test_initial_setup_and_later_restart_run_the_same_real_entrypoint(self):
        # This is a disposable Python stub, not an installed pgAdmin service or
        # a database client. The context owns, terminates and reaps both children.
        with self.handler(template=shlex.join(self.template), real=True) as (
            handler,
            config,
            popen,
            children,
        ):
            with (
                patch.object(
                    postgres_utils,
                    "create_pgadmin_config",
                    return_value=str(self.directory / "config_local.py"),
                ),
                patch.object(postgres_utils.config, "set") as saved,
                patch.dict(os.environ, {}, clear=False),
            ):
                success, error = postgres_utils.start_pgadmin(
                    handler,
                    str(self.directory),
                    str(self.directory / "config_local.py"),
                    "postgresql://example.invalid/pgadmin",
                    "operator@example.com",
                    "example-password",
                    "pgAdmin4",
                    5050,
                    "127.0.0.1",
                )
            self.assertTrue(success, error)
            saved.assert_called_once_with(
                "pgadmin", "command", shlex.join(self.template)
            )
            self.assertEqual(popen.call_args.args[0], self.resolved)
            self.assertEqual((self.directory / "started").read_text(), "ready")
            first = children[0]
            os.killpg(first.pid, signal.SIGTERM)
            first.wait(timeout=5)
            handler.process_names.clear()
            handler.processes.clear()
            (self.directory / "started").unlink()

            success, error = handler.start_process("pgAdmin4")
            self.assertTrue(success, error)
            self.assertEqual(len(children), 2)
            self.assertIsNone(children[1].poll())
            self.assertEqual((self.directory / "started").read_text(), "ready")
            for call in popen.call_args_list:
                self.assertEqual(call.args[0], self.resolved)
                self.assertEqual(call.kwargs["cwd"], config["config_dir"])
                self.assertFalse(call.kwargs.get("shell", False))


if __name__ == "__main__":
    unittest.main()
