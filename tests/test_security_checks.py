import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

try:
    import api.api_service as api_service
except Exception:  # pragma: no cover
    api_service = None


def _load_security_scan_module():
    spec = importlib.util.spec_from_file_location(
        "security_scan_module",
        Path(__file__).resolve().parents[1] / "scripts" / "security_scan.py",
    )
    if spec is None or spec.loader is None:
        raise ImportError("Could not load security_scan.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


security_scan = _load_security_scan_module()


class _StubLogger:
    def info(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


@unittest.skipIf(api_service is None, "api.api_service dependencies are not available")
class APICorsSecurityTests(unittest.TestCase):
    def setUp(self):
        self._orig_config_manager = api_service.CONFIG_MANAGER
        self._orig_get_logger = api_service.get_logger
        self.logger = _StubLogger()
        self.logger_patcher = patch.object(
            api_service, "get_logger", return_value=self.logger
        )
        self.logger_patcher.start()

    def tearDown(self):
        self.logger_patcher.stop()
        api_service.CONFIG_MANAGER = self._orig_config_manager
        api_service.get_logger = self._orig_get_logger

    def _build_cors_middleware_options(self, origins):
        api_service.CONFIG_MANAGER = SimpleNamespace(
            config={"dumb": {"frontend": {"origins": origins}}}
        )
        app = api_service.create_app()
        cors = next(
            middleware
            for middleware in app.user_middleware
            if middleware.cls.__name__ == "CORSMiddleware"
        )
        if hasattr(cors, "options"):
            return cors.options
        if hasattr(cors, "kwargs"):
            return cors.kwargs
        if (
            hasattr(cors, "args")
            and len(cors.args) > 1
            and isinstance(cors.args[1], dict)
        ):
            return cors.args[1]
        return {}

    def test_cors_respects_explicit_frontend_origins(self):
        options = self._build_cors_middleware_options(["https://dumbarr.test"])
        self.assertEqual(options["allow_origins"], ["https://dumbarr.test"])
        self.assertTrue(options["allow_credentials"])

    def test_cors_supports_string_origin_config(self):
        options = self._build_cors_middleware_options("https://single-origin.test")
        self.assertEqual(options["allow_origins"], ["https://single-origin.test"])
        self.assertTrue(options["allow_credentials"])

    def test_cors_wildcard_disables_credentials_and_falls_back_to_default(self):
        options = self._build_cors_middleware_options(["*"])
        self.assertEqual(
            options["allow_origins"], ["http://localhost", "http://localhost:8000"]
        )
        self.assertFalse(options["allow_credentials"])

    def test_cors_defaults_and_deduplicates_origins(self):
        options = self._build_cors_middleware_options(None)
        self.assertEqual(
            options["allow_origins"], ["http://localhost", "http://localhost:8000"]
        )
        self.assertTrue(options["allow_credentials"])

        options = self._build_cors_middleware_options(
            ["https://a.test", "https://a.test", "https://b.test"]
        )
        self.assertEqual(options["allow_origins"], ["https://a.test", "https://b.test"])
        self.assertTrue(options["allow_credentials"])


class SecretScanTests(unittest.TestCase):
    def test_scan_detects_obvious_secret_literal(self):
        with TemporaryDirectory() as tmpdir:
            temp_path = Path(tmpdir)
            fixture = temp_path / "service.env"
            fixture.write_text(
                'API_TOKEN="supersecretkeyvalue_1234567890"', encoding="utf-8"
            )

            findings = security_scan.scan_content_for_secrets(
                fixture.read_text(encoding="utf-8"),
                fixture,
            )

            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].key, "API_TOKEN")

    def test_scan_ignores_placeholder_values(self):
        with TemporaryDirectory() as tmpdir:
            temp_path = Path(tmpdir)
            fixture = temp_path / "service.env"
            fixture.write_text('API_TOKEN="changeme"', encoding="utf-8")

            findings = security_scan.scan_content_for_secrets(
                fixture.read_text(encoding="utf-8"),
                fixture,
            )

            self.assertEqual(findings, [])

    def test_scan_only_checks_candidate_extensions_and_ignores_dotfiles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "good"
            target.mkdir()
            target.joinpath("bad.md").write_text(
                "API_TOKEN=supersecretkeyvalue_1234567890", encoding="utf-8"
            )
            target.joinpath("good.env").write_text(
                "API_TOKEN=supersecretkeyvalue_1234567890", encoding="utf-8"
            )

            findings = security_scan.find_secrets(root)
            self.assertEqual(len(findings), 1)
            self.assertTrue(findings[0].path.name == "good.env")

    def test_scan_ignores_runtime_volume_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime_config = root / "data" / "altmount"
            runtime_config.mkdir(parents=True)
            runtime_config.joinpath("config.yaml").write_text(
                "api_key: supersecretkeyvalue_1234567890\n",
                encoding="utf-8",
            )
            root.joinpath("service.env").write_text(
                "API_TOKEN=supersecretkeyvalue_1234567890\n",
                encoding="utf-8",
            )

            findings = security_scan.find_secrets(root)

            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].path.name, "service.env")


if __name__ == "__main__":
    unittest.main()
