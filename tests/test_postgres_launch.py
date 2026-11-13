"""PostgreSQL restart argv resolution without rewriting retained configuration."""

from copy import deepcopy
import os
import unittest
from unittest.mock import Mock, patch

from utils.postgres_launch import postgres_launch_command


class PostgresCommandTests(unittest.TestCase):
    def test_string_is_tokenized_before_path_substitution(self):
        directory = "/reserved/data with spaces and 'quotes'"
        config = '/reserved/config with "quotes"/postgresql.conf'
        self.assertEqual(
            postgres_launch_command(
                "postgres -D {postgres_config_dir} -c config_file={postgres_config_file}",
                directory,
                config,
            ),
            ["postgres", "-D", directory, "-c", "config_file=" + config],
        )

    def test_argv_is_copied_and_only_two_known_tokens_are_replaced_once(self):
        command = [
            "postgres",
            "-D",
            "{postgres_config_dir}",
            "-c",
            "config_file={postgres_config_file}",
            "-c",
            "custom={unrelated}",
        ]
        before = deepcopy(command)
        directory = "/reserved/{postgres_config_file}/literal"
        expected = [
            "postgres",
            "-D",
            directory,
            "-c",
            "config_file=/reserved/postgresql.conf",
            "-c",
            "custom={unrelated}",
        ]
        self.assertEqual(
            postgres_launch_command(command, directory, "/reserved/postgresql.conf"),
            expected,
        )
        self.assertEqual(command, before)
        self.assertEqual(
            postgres_launch_command(
                tuple(command), directory, "/reserved/postgresql.conf"
            ),
            expected,
        )

    def test_literal_custom_commands_need_no_unused_token_values(self):
        command = ["custom-postgres", "-D", "/reserved/literal directory", "{other}"]
        self.assertEqual(postgres_launch_command(command, None, None), command)
        self.assertEqual(
            postgres_launch_command(
                "custom-postgres -D '/reserved/literal directory'", None, None
            ),
            command[:3],
        )

    def test_missing_referenced_path_fails_without_disclosing_command_values(self):
        secret = "reserved-private-value"
        for value in (None, "", 3, [], "invalid\x00path"):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError) as caught:
                    postgres_launch_command(
                        ["postgres", secret, "{postgres_config_file}"],
                        "/reserved/data",
                        value,
                    )
                self.assertNotIn(secret, str(caught.exception))
                self.assertIn("configured nonempty path", str(caught.exception))

    def test_invalid_command_shapes_fail_without_raw_values(self):
        for command in (
            None,
            3,
            {},
            [],
            ["postgres", None],
            ["postgres", "invalid\x00value"],
            "'unterminated",
        ):
            with self.subTest(command_type=type(command).__name__):
                with self.assertRaises(ValueError) as caught:
                    postgres_launch_command(command, "/reserved/data", "/reserved/conf")
                self.assertEqual(
                    str(caught.exception),
                    "The PostgreSQL launch command must be a quoted string or string argument list.",
                )


class ManagedPostgresLaunchTests(unittest.TestCase):
    def launch(
        self,
        *,
        key="postgres",
        command=None,
        directory=None,
        repeat=False,
        prepared=False,
    ):
        from utils.processes import ProcessHandler

        handler = object.__new__(ProcessHandler)
        handler.init_attributes(Mock())
        name = "Fixture PostgreSQL"
        if not prepared:
            handler.setup_tracker.add(name)
        handler._update_running_processes_file = Mock()
        if prepared:
            handler._reconcile_cli_battery_runtime = Mock(
                side_effect=AssertionError(
                    "Prepared recovery must not start or stop an unreviewed Battery service"
                )
            )
        handler._check_immediate_exit_and_log = Mock(return_value=(True, None))
        config = {
            "process_name": name,
            "config_dir": "/reserved/data with 'quotes'",
            "config_file": '/reserved/config with "quotes"/postgresql.conf',
            "command": [
                "postgres",
                "-D",
                "{postgres_config_dir}",
                "-c",
                "config_file={postgres_config_file}",
            ],
            "env": {},
        }
        before = deepcopy(config)
        process = Mock(pid=1234)
        process.poll.return_value = None

        def config_get(field, default=None):
            if field == "puid":
                return os.geteuid()
            if field == "pgid":
                return os.getegid()
            return default if default is not None else {}

        with (
            patch(
                "utils.processes.CONFIG_MANAGER.find_key_for_process",
                return_value=(key, None),
            ),
            patch("utils.processes.CONFIG_MANAGER.get_instance", return_value=config),
            patch("utils.processes.CONFIG_MANAGER.get", side_effect=config_get),
            patch(
                "utils.infinidysk_migration_admission.infinidysk_recovery_blocks_service",
                return_value=None,
            ),
            patch("utils.processes.subprocess.Popen", return_value=process) as popen,
            patch("utils.processes.SubprocessLogger"),
            patch(
                "utils.processes.prepare_cli_debrid_runtime", return_value=(True, None)
            ),
            patch("utils.processes.notify_event"),
            patch(
                "utils.setup.setup_project",
                side_effect=AssertionError("Do not repeat PostgreSQL setup"),
            ),
        ):
            if repeat:
                resolved = postgres_launch_command(
                    config["command"], config["config_dir"], config["config_file"]
                )
                success, error = handler.start_process(
                    name, config["config_dir"], resolved
                )
                self.assertTrue(success, error)
                handler.process_names.clear()
                handler.processes.clear()
            success, error = (
                handler.start_prepared_process(name)
                if prepared
                else handler.start_process(name, directory, command)
            )
        self.assertTrue(success, error)
        self.assertEqual(config, before)
        return config, popen

    def test_cold_recovery_prepared_start_uses_installed_command_without_setup(self):
        config, popen = self.launch(prepared=True)
        self.assertEqual(
            popen.call_args.args[0],
            postgres_launch_command(
                config["command"], config["config_dir"], config["config_file"]
            ),
        )
        self.assertEqual(popen.call_count, 1)

    def test_prepared_cli_start_does_not_reconcile_unretained_battery(self):
        config, popen = self.launch(key="cli_debrid", prepared=True)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(popen.call_args.args[0], config["command"])

    def test_name_only_restart_after_setup_uses_same_resolved_argv_and_template_is_unchanged(
        self,
    ):
        config, popen = self.launch(repeat=True)
        expected = postgres_launch_command(
            config["command"], config["config_dir"], config["config_file"]
        )
        self.assertEqual(popen.call_count, 2)
        for call in popen.call_args_list:
            self.assertEqual(call.args[0], expected)
            self.assertEqual(call.kwargs["cwd"], config["config_dir"])

    def test_explicit_working_directory_remains_effective_for_configured_template(self):
        override = "/reserved/explicit directory with 'quotes'"
        config, popen = self.launch(directory=override)
        self.assertEqual(popen.call_args.args[0][2], override)
        self.assertEqual(popen.call_args.kwargs["cwd"], override)
        self.assertNotEqual(override, config["config_dir"])

    def test_explicit_literal_argv_survives_missing_working_directory(self):
        command = ["custom-postgres", "-D", "/reserved/literal directory", "{other}"]
        config, popen = self.launch(command=command)
        self.assertEqual(popen.call_args.args[0], command)
        self.assertIsNot(popen.call_args.args[0], command)
        self.assertEqual(popen.call_args.kwargs["cwd"], config["config_dir"])

    def test_non_postgres_launch_keeps_previous_literal_command_behavior(self):
        config, popen = self.launch(key="example")
        self.assertEqual(popen.call_args.args[0], config["command"])
        self.assertIn("{postgres_config_dir}", popen.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
