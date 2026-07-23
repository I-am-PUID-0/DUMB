import sys
import types
import unittest
from logging import LogRecord


def _install_logger_stubs():
    config_loader = types.ModuleType("utils.config_loader")
    config_loader.CONFIG_MANAGER = types.SimpleNamespace(
        get=lambda key, default=None: default
    )
    sys.modules["utils.config_loader"] = config_loader

    colorlog = types.ModuleType("colorlog")
    colorlog.ColoredFormatter = object
    sys.modules["colorlog"] = colorlog


_install_logger_stubs()
sys.modules.pop("utils.logger", None)

from utils.logger import SensitiveDataFilter, redact_sensitive_log_data


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

    def test_preserves_non_sensitive_text(self):
        line = "INFO normal startup message"

        self.assertEqual(redact_sensitive_log_data(line), line)


if __name__ == "__main__":
    unittest.main()
