import ast
import asyncio
from functools import wraps
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import re


def isolated_asgi_test(method):
    """Keep real FastAPI out of the process used by router-double tests."""

    @wraps(method)
    def run(self):
        if os.environ.get("DUMB_METADATA_REAL_ASGI_TEST") == "1":
            self._set_up_real_asgi()
            return method(self)
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "unittest",
                f"tests.test_service_ui_metadata.{type(self).__name__}.{method.__name__}",
            ],
            cwd=Path(__file__).parents[1],
            env={**os.environ, "DUMB_METADATA_REAL_ASGI_TEST": "1"},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    return run


class EmbeddedUiRoutingTests(unittest.TestCase):
    def _set_up_real_asgi(self):
        from fastapi import (
            APIRouter,
            Depends,
            FastAPI,
            HTTPException,
            Request,
            Response,
        )

        path = Path(__file__).parents[1] / "api" / "routers" / "config.py"
        source = ast.parse(path.read_text())
        endpoints = [
            node
            for node in source.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name in {"get_embedded_ui_routing", "get_service_ui_links"}
        ]
        self.auth_enabled = True
        self.auth_checks = 0

        def private_user():
            self.auth_checks += 1
            if self.auth_enabled:
                raise HTTPException(status_code=401, detail="Authentication required")

        self.rows = [
            {
                "name": "InfiniDysk",
                "config_key": "infinidysk",
                "runtime_generation": "2",
                "embedded_ui_protocol": 1,
                "host": "private.example.invalid",
                "port": 3000,
                "direct_url": "https://private.example.invalid/",
                "path": "/private/configuration",
                "credentials": "must-not-leave-private-inventory",
            }
        ]
        self.logger = Mock()
        self.build = Mock(return_value=self.rows)
        self.ensure = Mock(return_value=self.rows)
        self.router = APIRouter()
        namespace = {
            "config_router": self.router,
            "Request": Request,
            "Response": Response,
            "Depends": Depends,
            "HTTPException": HTTPException,
            "re": re,
            "get_logger": lambda: self.logger,
            "get_optional_current_user": private_user,
            "build_ui_services": self.build,
            "ensure_ui_services_config": self.ensure,
            "get_traefik_config_dir": Mock(),
            "get_traefik_dynamic_config_dir": lambda: Path("/example/dynamic"),
            "_normalize_direct_url": lambda service, request: service,
            "CONFIG_MANAGER": SimpleNamespace(config={"traefik": {"enabled": True}}),
        }
        code = compile(ast.Module(body=endpoints, type_ignores=[]), str(path), "exec")
        # Evaluate only the trusted checked-in endpoints, including dependencies.
        exec(code, namespace)  # noqa: S102
        app = FastAPI()
        app.include_router(self.router, prefix="/config")
        self.app = app

    def get(self, path):
        async def request():
            route, _, query = path.partition("?")
            messages = []
            complete = asyncio.Event()
            delivered = False

            async def receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await complete.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                messages.append(message)
                if message["type"] == "http.response.body" and not message.get(
                    "more_body"
                ):
                    complete.set()

            # Exercise the actual HTTP route/dependency wiring with only locked
            # ASGI dependencies, rather than an optional HTTP client package.
            await self.app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.4"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": route,
                    "raw_path": route.encode(),
                    "query_string": query.encode(),
                    "root_path": "",
                    "server": ("reserved.example", 8000),
                    "client": ("127.0.0.1", 1),
                    "headers": [],
                },
                receive,
                send,
            )
            start = next(
                row for row in messages if row["type"] == "http.response.start"
            )
            raw = b"".join(
                row.get("body", b"")
                for row in messages
                if row["type"] == "http.response.body"
            )
            return SimpleNamespace(
                status_code=start["status"],
                headers={
                    key.decode(): value.decode() for key, value in start["headers"]
                },
                text=raw.decode(),
                json=lambda: json.loads(raw),
            )

        return asyncio.run(request())

    @isolated_asgi_test
    def test_storage_only_dashboard_auth_does_not_protect_minimal_routing_proof(self):
        response = self.get("/config/embedded-ui-routing/infinidysk")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "name": "InfiniDysk",
                "config_key": "infinidysk",
                "runtime_generation": "2",
                "embedded_ui_protocol": 1,
            },
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.auth_checks, 0)
        self.ensure.assert_not_called()
        private = self.get("/config/service-ui?metadata_only=true")
        self.assertEqual(private.status_code, 401)
        self.assertEqual(self.auth_checks, 1)
        self.assertNotIn("must-not-leave", response.text)

    @isolated_asgi_test
    def test_private_inventory_auth_disabled_behavior_is_unchanged(self):
        self.auth_enabled = False
        response = self.get("/config/service-ui?metadata_only=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["services"], self.rows)
        self.ensure.assert_not_called()

    @isolated_asgi_test
    def test_only_enabled_matching_infinidysk_routes_receive_proof(self):
        for rows in [
            [],
            [{**self.rows[0], "name": "Other"}],
            [{**self.rows[0], "config_key": "sonarr"}],
            self.rows * 2,
        ]:
            with self.subTest(rows=rows):
                self.build.return_value = rows
                response = self.get("/config/embedded-ui-routing/infinidysk")
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("private.example.invalid", response.text)
        self.assertEqual(self.auth_checks, 0)

    @isolated_asgi_test
    def test_route_names_follow_existing_service_url_normalization(self):
        self.build.return_value = [{**self.rows[0], "name": "InfiniDysk / Lab"}]
        self.assertEqual(
            self.get("/config/embedded-ui-routing/infinidysk___lab").status_code,
            200,
        )
        self.assertEqual(
            self.get("/config/embedded-ui-routing/InfiniDysk").status_code,
            404,
        )

    @isolated_asgi_test
    def test_legacy_runtime_proof_does_not_claim_a_native_protocol(self):
        row = {
            "name": "InfiniDysk",
            "config_key": "infinidysk",
            "runtime_generation": "1",
        }
        self.build.return_value = [row]
        self.assertEqual(self.get("/config/embedded-ui-routing/infinidysk").json(), row)

    @isolated_asgi_test
    def test_failed_inventory_build_refuses_routing_without_private_exception_details(
        self,
    ):
        self.build.side_effect = RuntimeError("private.example.invalid")
        response = self.get("/config/embedded-ui-routing/infinidysk")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private.example.invalid", response.text)


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
