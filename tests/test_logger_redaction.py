import sys
import types
import unittest


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

from utils.logger import redact_sensitive_log_data


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

    def test_preserves_non_sensitive_text(self):
        line = "INFO normal startup message"

        self.assertEqual(redact_sensitive_log_data(line), line)


if __name__ == "__main__":
    unittest.main()
