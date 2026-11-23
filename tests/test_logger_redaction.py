import importlib.util
import io
import logging
import os
import sys
import types
import unittest
from logging import LogRecord
from pathlib import Path
from unittest.mock import Mock, patch


def _load_logger_with_scoped_stubs():
    config_loader = types.ModuleType("utils.config_loader")
    config_loader.CONFIG_MANAGER = types.SimpleNamespace(
        get=lambda key, default=None: default
    )
    colorlog = types.ModuleType("colorlog")
    colorlog.ColoredFormatter = object
    spec = importlib.util.spec_from_file_location(
        "logger_redaction_fixture",
        Path(__file__).resolve().parents[1] / "utils" / "logger.py",
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {"utils.config_loader": config_loader, "colorlog": colorlog},
    ):
        spec.loader.exec_module(module)
    return module


_logger = _load_logger_with_scoped_stubs()
SensitiveDataFilter = _logger.SensitiveDataFilter
SubprocessLogger = _logger.SubprocessLogger
redact_sensitive_log_data = _logger.redact_sensitive_log_data


class LoggerRedactionTests(unittest.TestCase):
    def test_redacts_cloudflared_tunnel_tokens(self):
        line = "CLOUDFLARED_TUNNEL_TOKEN=super-secret-token"

        self.assertEqual(
            redact_sensitive_log_data(line),
            "CLOUDFLARED_TUNNEL_TOKEN=[REDACTED]",
        )

    def test_redacts_url_query_credentials(self):
        line = "GET /callback?token=abc123&api_key=def456&safe=value"

        self.assertEqual(
            redact_sensitive_log_data(line),
            "GET /callback?token=[REDACTED]&api_key=[REDACTED]&safe=value",
        )

    def test_redacts_assignment_credentials(self):
        line = "password: hunter2 apiKey=abc123 secret=topsecret token=plain"

        self.assertEqual(
            redact_sensitive_log_data(line),
            "password: [REDACTED] apiKey=[REDACTED] secret=[REDACTED] token=[REDACTED]",
        )

    def test_redacts_secret_key_variants(self):
        line = (
            "SECRET_KEY=first secret-key: second "
            '"secret key": "third" /config?secret_key=fourth'
        )

        self.assertEqual(
            redact_sensitive_log_data(line),
            "SECRET_KEY=[REDACTED] secret-key: [REDACTED] "
            '"secret key": "[REDACTED]" /config?secret_key=[REDACTED]',
        )

    def test_redacts_aiostreams_auth_credentials(self):
        lines = "\n".join(
            [
                "AIOSTREAMS_AUTH=admin:correct-horse-battery-staple",
                "AIOSTREAMS_AUTH_PERMISSIONS=admin=admin",
                "Overriding 'auth_password' = 'correct-horse-battery-staple' in service config",
            ]
        )

        redacted = redact_sensitive_log_data(lines)

        self.assertNotIn("correct-horse-battery-staple", redacted)
        self.assertEqual(
            "\n".join(
                [
                    "AIOSTREAMS_AUTH=[REDACTED]",
                    "AIOSTREAMS_AUTH_PERMISSIONS=[REDACTED]",
                    "Overriding 'auth_password' = '[REDACTED]' in service config",
                ]
            ),
            redacted,
        )

    def test_redacts_mediastorm_generated_credentials(self):
        webdav_line = "WebDAV credentials: novastream / generated-password"
        homepage_line = "Homepage API key: generated-api-key"

        self.assertEqual(
            redact_sensitive_log_data(webdav_line),
            "WebDAV credentials: novastream / [REDACTED]",
        )
        self.assertEqual(
            redact_sensitive_log_data(homepage_line),
            "Homepage API key: [REDACTED]",
        )

    def test_redacts_http_credentials_and_session_cookies(self):
        lines = "\n".join(
            [
                "Cookie: auth-session=example; service-session=example",
                "Authorization: Bearer example-token",
                "X-Plex-Token: example-plex-token",
                'request headers: {"cookie": "session=example"}',
            ]
        )

        self.assertEqual(
            redact_sensitive_log_data(lines),
            "\n".join(
                [
                    "Cookie: [REDACTED]",
                    "Authorization: [REDACTED]",
                    "X-Plex-Token: [REDACTED]",
                    'request headers: {"cookie": "[REDACTED]"}',
                ]
            ),
        )

    def test_redacts_extended_query_and_json_credentials(self):
        line = (
            "GET /callback?auth_token=one&access_token=two&code=three "
            '{"client_secret": "four", "refresh_token": "five"}'
        )

        self.assertEqual(
            redact_sensitive_log_data(line),
            "GET /callback?auth_token=[REDACTED]&access_token=[REDACTED]"
            '&code=[REDACTED] {"client_secret": "[REDACTED]", '
            '"refresh_token": "[REDACTED]"}',
        )

    def test_redacts_plex_account_and_server_identifiers(self):
        server_id = "a" * 40
        text = "\n".join(
            [
                "MyPlex: username is ExampleUser, login is user@example.com, home is 1",
                "Request completed GZIP Signed-in Token (ExampleUser)",
                f"CrashUploader --serverUuid={server_id} --userId=user@example.com",
                f"GET /devices/{server_id}?machineIdentifier={server_id}",
                f"MyPlex: Got response for {server_id} ~ registered",
            ]
        )

        redacted = redact_sensitive_log_data(text)

        self.assertNotIn("ExampleUser", redacted)
        self.assertNotIn("user@example.com", redacted)
        self.assertNotIn(server_id, redacted)
        self.assertIn("MyPlex: username is [REDACTED]", redacted)
        self.assertIn("Signed-in Token ([REDACTED])", redacted)
        self.assertIn("--serverUuid=[REDACTED]", redacted)
        self.assertIn("/devices/[REDACTED]", redacted)

    def test_logging_filter_redacts_formatted_records(self):
        record = LogRecord(
            "test",
            20,
            __file__,
            1,
            "request Cookie: session=%s",
            ("example",),
            None,
        )

        self.assertTrue(SensitiveDataFilter().filter(record))
        self.assertEqual(record.getMessage(), "request Cookie: [REDACTED]")

    def test_redacts_fields_split_by_terminal_styling(self):
        line = (
            "\x1b[3mtoken\x1b[0m\x1b[2m=\x1b[0mexample-token "
            "\x1b[3mapi_key\x1b[0m\x1b[2m=\x1b[0mexample-api-key "
            "statement=\"SELECT 'WARN'\\nFROM example\""
        )

        self.assertEqual(
            redact_sensitive_log_data(line),
            "token=[REDACTED] api_key=[REDACTED] "
            "statement=\"SELECT 'WARN'\\nFROM example\"",
        )

    def test_filter_neutralizes_styling_before_redacting_formatted_fields(self):
        record = LogRecord(
            "test",
            logging.WARNING,
            __file__,
            1,
            "native warning %s",
            ("\x1b[3mtoken\x1b[0m\x1b[2m=\x1b[0mexample-token",),
            None,
        )

        self.assertTrue(SensitiveDataFilter().filter(record))
        self.assertEqual(record.getMessage(), "native warning token=[REDACTED]")
        self.assertEqual(record.args, ())

    def test_csi_removal_preserves_plain_and_literal_escape_text(self):
        line = "\x1b[?25l\x1b[2mexample statement\x1b[0m\\nnext row\x1b[0K"

        self.assertEqual(
            redact_sensitive_log_data(line), "example statement\\nnext row"
        )

    def test_preserves_non_sensitive_text(self):
        line = "INFO normal startup message"

        self.assertEqual(redact_sensitive_log_data(line), line)


class InfiniDyskTracingLogTests(unittest.TestCase):
    def test_native_header_controls_severity_without_stripping_payload(self):
        for native_level, expected in (
            ("TRACE", "DEBUG"),
            ("DEBUG", "DEBUG"),
            ("INFO", "INFO"),
            ("WARN", "WARNING"),
            ("ERROR", "ERROR"),
        ):
            with self.subTest(level=native_level):
                line = (
                    "\x1b[2m2026-01-02T03:04:05.123456Z\x1b[0m "
                    f"\x1b[33m{native_level:>5}\x1b[0m "
                    "\x1b[2msqlx::query:\x1b[0m statement=\"SELECT 'INFO ERROR'\""
                )

                level, message = SubprocessLogger.parse_log_level_and_message(
                    line, "Custom download service", service_key="infinidysk"
                )

                self.assertEqual(level, expected)
                self.assertEqual(message, line)

    def test_native_timestamp_accepts_offsets_and_optional_fraction(self):
        for timestamp in (
            "2026-01-02T03:04:05Z",
            "2026-01-02T03:04:05.1+02:00",
            "2026-01-02T03:04:05.123456-05:30",
        ):
            with self.subTest(timestamp=timestamp):
                line = f"{timestamp}  WARN infinidysk::sab: example warning"

                self.assertEqual(
                    SubprocessLogger.parse_log_level_and_message(line, "InfiniDysk"),
                    ("WARNING", line),
                )

    def test_literal_sql_newlines_and_level_words_are_preserved(self):
        line = (
            "2026-01-02T03:04:05.123456Z  WARN sqlx::query: "
            "statement=\"SELECT 'ERROR'\\nFROM example WHERE value = 'DEBUG'\""
        )

        self.assertEqual(
            SubprocessLogger.parse_log_level_and_message(
                line, "InfiniDysk", service_key="infinidysk"
            ),
            ("WARNING", line),
        )

    def test_native_csi_sequences_do_not_hide_the_tracing_header(self):
        line = (
            "\x1b[?25l2026-01-02T03:04:05Z WARN "
            "\x1b[1;3msqlx::query:\x1b[0m example statement\x1b[0K"
        )

        self.assertEqual(
            SubprocessLogger.parse_log_level_and_message(
                line, "InfiniDysk", service_key="infinidysk"
            ),
            ("WARNING", line),
        )

    def test_ansi_in_unrecognized_and_other_service_lines_is_untouched(self):
        styled = "\x1b[3mlabel\x1b[0m\x1b[2m=\x1b[0mexample"
        for line, process_name, service_key in (
            (f"example {styled}", "InfiniDysk", "infinidysk"),
            (
                f"2026-01-02T03:04:05Z WARN sqlx::query: {styled}",
                "Other service",
                "other",
            ),
        ):
            with self.subTest(process_name=process_name):
                self.assertEqual(
                    SubprocessLogger.parse_log_level_and_message(
                        line, process_name, service_key=service_key
                    ),
                    ("UNKNOWN", line),
                )

    def test_only_an_anchored_header_is_recognized(self):
        for prefix in ("example ", 'statement="'):
            with self.subTest(prefix=prefix):
                line = prefix + "2026-01-02T03:04:05Z WARN sqlx::query: example"

                self.assertEqual(
                    SubprocessLogger.parse_log_level_and_message(
                        line, "InfiniDysk", service_key="infinidysk"
                    ),
                    ("UNKNOWN", line),
                )

    def test_exact_legacy_alias_and_known_key_support_custom_names(self):
        line = "2026-01-02T03:04:05Z WARN infinidysk_mount: example"
        for process_name, service_key in (
            ("InfiniDysk", None),
            ("NzbDAV", None),
            ("Custom download service", "infinidysk"),
            ("Custom download service", "nzbdav"),
        ):
            with self.subTest(process_name=process_name, service_key=service_key):
                self.assertEqual(
                    SubprocessLogger.parse_log_level_and_message(
                        line, process_name, service_key=service_key
                    ),
                    ("WARNING", line),
                )

    def test_other_services_and_known_non_native_keys_keep_existing_parser(self):
        line = "2026-01-02T03:04:05Z WARN sqlx::query: example"
        for process_name, service_key in (
            ("Other service", None),
            ("Radarr InfiniDysk", None),
            ("InfiniDysk", "radarr"),
        ):
            with self.subTest(process_name=process_name, service_key=service_key):
                self.assertEqual(
                    SubprocessLogger.parse_log_level_and_message(
                        line, process_name, service_key=service_key
                    ),
                    ("UNKNOWN", line),
                )

    def test_v1_and_unrelated_log_formats_keep_existing_behavior(self):
        for line, process_name, expected in (
            (
                "[03:04:05 INF] starting",
                "InfiniDysk",
                ("UNKNOWN", "[03:04:05 INF] starting"),
            ),
            (
                "[03:04:05 WRN] warning",
                "InfiniDysk",
                ("UNKNOWN", "[03:04:05 WRN] warning"),
            ),
            (
                "2026-01-02 03:04:05 ERROR: failed",
                "PostgreSQL",
                ("ERROR", "failed"),
            ),
            ("INFO: ready", "rclone", ("INFO", "ready")),
        ):
            with self.subTest(line=line, process_name=process_name):
                self.assertEqual(
                    SubprocessLogger.parse_log_level_and_message(line, process_name),
                    expected,
                )

    def test_both_pipe_readers_preserve_header_redact_and_emit_warning(self):
        original = (
            "\x1b[2m2026-01-02T03:04:05.123456Z\x1b[0m "
            "\x1b[33m WARN\x1b[0m sqlx::query: "
            "\x1b[3mtoken\x1b[0m\x1b[2m=\x1b[0mexample-token "
            "\x1b[3mapi_key\x1b[0m\x1b[2m=\x1b[0mexample-api-key query was slow"
        )
        for pipe in ("stdout", "stderr"):
            with self.subTest(pipe=pipe):
                main_output, file_output = io.StringIO(), io.StringIO()
                main_logger = self._logger(main_output, logging.WARNING)
                file_logger = self._logger(file_output, logging.WARNING)
                subprocess_logger = SubprocessLogger(
                    main_logger,
                    "Custom download service",
                    file_logger=file_logger,
                    service_key="infinidysk",
                )

                if pipe == "stdout":
                    subprocess_logger.log_subprocess_output(
                        io.StringIO(original + "\n")
                    )
                else:
                    subprocess_logger.monitor_stderr(
                        types.SimpleNamespace(stderr=io.StringIO(original + "\n")),
                        "example",
                        "Custom download service",
                    )

                expected = (
                    "WARNING - Custom download service subprocess: "
                    "2026-01-02T03:04:05.123456Z  WARN sqlx::query: "
                    "token=[REDACTED] api_key=[REDACTED] query was slow\n"
                )
                self.assertNotIn("example-token", main_output.getvalue())
                self.assertNotIn("example-api-key", file_output.getvalue())
                self.assertEqual(main_output.getvalue(), expected)
                self.assertEqual(file_output.getvalue(), expected)

    def test_trace_uses_debug_filtering_and_preserves_native_level(self):
        line = "2026-01-02T03:04:05Z TRACE infinidysk: example trace"
        main_output, file_output = io.StringIO(), io.StringIO()
        subprocess_logger = SubprocessLogger(
            self._logger(main_output, logging.INFO),
            "InfiniDysk",
            file_logger=self._logger(file_output, logging.DEBUG),
            service_key="infinidysk",
        )

        subprocess_logger.log_subprocess_output(io.StringIO(line + "\n"))

        self.assertEqual(main_output.getvalue(), "")
        self.assertEqual(
            file_output.getvalue(), f"DEBUG - InfiniDysk subprocess: {line}\n"
        )

    def test_process_producer_passes_service_key_for_a_custom_name(self):
        from utils.processes import ProcessHandler

        handler = object.__new__(ProcessHandler)
        handler.init_attributes(Mock())
        name = "Custom download service"
        handler.setup_tracker.add(name)
        handler._update_running_processes_file = Mock()
        handler._check_immediate_exit_and_log = Mock(return_value=(True, None))
        configuration = {
            "process_name": name,
            "command": ["example-native-service"],
            "config_dir": "/reserved/example",
            "env": {},
        }
        process = Mock(pid=1234)
        process.poll.return_value = None

        def config_get(key, default=None):
            if key == "puid":
                return os.geteuid()
            if key == "pgid":
                return os.getegid()
            return default if default is not None else {}

        with (
            patch(
                "utils.processes.CONFIG_MANAGER.find_key_for_process",
                return_value=("infinidysk", None),
            ),
            patch(
                "utils.processes.CONFIG_MANAGER.get_instance",
                return_value=configuration,
            ),
            patch("utils.processes.CONFIG_MANAGER.get", side_effect=config_get),
            patch("utils.processes.subprocess.Popen", return_value=process),
            patch("utils.processes.SubprocessLogger") as constructor,
            patch("utils.infinidysk_v2_arr_sync.schedule_arr_sync"),
        ):
            success, error = handler._start_process_admitted(name)

        self.assertTrue(success, error)
        self.assertEqual(constructor.call_args.kwargs["service_key"], "infinidysk")
        self.assertEqual(constructor.call_args.args[1], name)
        constructor.return_value.start_logging_stdout.assert_called_once_with(process)
        constructor.return_value.start_monitoring_stderr.assert_called_once_with(
            process, None, name
        )

    @staticmethod
    def _logger(output, level):
        logger = logging.Logger("native-tracing-fixture", level)
        handler = logging.StreamHandler(output)
        handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))
        handler.addFilter(SensitiveDataFilter())
        logger.addHandler(handler)
        return logger


if __name__ == "__main__":
    unittest.main()
