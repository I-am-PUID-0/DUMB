import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class ServiceUiMetadataTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parents[1] / "api" / "routers" / "config.py"
        source = ast.parse(path.read_text())
        endpoint = next(
            node
            for node in source.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "get_service_ui_links"
        )
        endpoint.decorator_list = []
        self.logger = Mock()
        self.rows = [{"name": "InfiniDysk", "runtime_generation": "2"}]
        self.ensure = Mock(return_value=self.rows)
        self.build = Mock(return_value=self.rows)
        self.namespace = {
            "Request": object,
            "Depends": lambda dependency: None,
            "get_logger": lambda: None,
            "get_optional_current_user": lambda: None,
            "build_ui_services": self.build,
            "ensure_ui_services_config": self.ensure,
            "get_traefik_config_dir": Mock(return_value=Path("/example/config")),
            "get_traefik_dynamic_config_dir": lambda: Path("/example/dynamic"),
            "_normalize_direct_url": lambda service, request: service,
            "CONFIG_MANAGER": SimpleNamespace(config={"traefik": {"enabled": True}}),
        }
        code = compile(ast.Module(body=[endpoint], type_ignores=[]), str(path), "exec")
        # Evaluate only the trusted checked-in endpoint inside this fixture.
        exec(code, self.namespace)  # noqa: S102

    def test_proxy_metadata_lookup_never_generates_or_writes_traefik_config(self):
        response = asyncio.run(
            self.namespace["get_service_ui_links"](
                object(), metadata_only=True, logger=self.logger
            )
        )
        self.assertEqual(response["services"], self.rows)
        self.assertTrue(response["enabled"])
        self.build.assert_called_once_with()
        self.ensure.assert_not_called()
        self.namespace["get_traefik_config_dir"].assert_not_called()
        self.logger.info.assert_not_called()

    def test_existing_service_ui_request_still_generates_configuration(self):
        response = asyncio.run(
            self.namespace["get_service_ui_links"](object(), logger=self.logger)
        )
        self.assertEqual(response["services"], self.rows)
        self.ensure.assert_called_once_with("/example/config")
        self.build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
