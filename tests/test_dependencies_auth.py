import asyncio
import importlib
import sys
import types
import unittest
from unittest.mock import patch

_MISSING = object()


def _save_modules(module_names):
    return {name: sys.modules.get(name, _MISSING) for name in module_names}


def _restore_modules(saved_modules):
    for name, module in saved_modules.items():
        if module is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _install_dependency_stubs():
    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code=500, detail=None, headers=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail
            self.headers = headers or {}

    class WebSocketException(Exception):
        def __init__(self, code=None, reason=None):
            super().__init__(reason)
            self.code = code
            self.reason = reason

    fastapi.Depends = lambda dependency=None, *args, **kwargs: None
    fastapi.WebSocket = object
    fastapi.WebSocketException = WebSocketException
    fastapi.HTTPException = HTTPException
    fastapi.status = types.SimpleNamespace(WS_1008_POLICY_VIOLATION=1008)
    sys.modules["fastapi"] = fastapi

    security = types.ModuleType("fastapi.security")

    class HTTPBearer:
        def __init__(self, *args, **kwargs):
            pass

    class HTTPAuthorizationCredentials:
        def __init__(self, credentials):
            self.credentials = credentials

    security.HTTPBearer = HTTPBearer
    security.HTTPAuthorizationCredentials = HTTPAuthorizationCredentials
    sys.modules["fastapi.security"] = security

    api_state = types.ModuleType("api.api_state")

    class APIState:
        def __init__(self, process_handler=None, logger=None):
            self.process_handler = process_handler
            self.logger = logger

    api_state.APIState = APIState
    sys.modules["api.api_state"] = api_state

    metrics = types.ModuleType("utils.metrics")

    class MetricsCollector:
        def __init__(self, process_handler=None, logger=None):
            self.process_handler = process_handler
            self.logger = logger

    metrics.MetricsCollector = MetricsCollector
    sys.modules["utils.metrics"] = metrics

    processes = types.ModuleType("utils.processes")
    processes.ProcessHandler = type("ProcessHandler", (), {})
    sys.modules["utils.processes"] = processes

    connection_manager = types.ModuleType("api.connection_manager")
    connection_manager.ConnectionManager = type("ConnectionManager", (), {})
    sys.modules["api.connection_manager"] = connection_manager


_IMPORT_STUBS = (
    "fastapi",
    "fastapi.security",
    "api.api_state",
    "utils.metrics",
    "utils.processes",
    "api.connection_manager",
    "utils.dependencies",
)

_saved_import_modules = _save_modules(_IMPORT_STUBS)
_install_dependency_stubs()
sys.modules.pop("utils.dependencies", None)
from utils import dependencies

sys.modules.pop("utils.dependencies", None)
_restore_modules(_saved_import_modules)


def _install_runtime_auth_stubs(auth_config, auth_module):
    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code=500, detail=None, headers=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail
            self.headers = headers or {}

    fastapi.HTTPException = HTTPException
    sys.modules["fastapi"] = fastapi

    auth_config_module = types.ModuleType("utils.auth_config")
    auth_config_module.AuthConfigManager = lambda: auth_config
    sys.modules["utils.auth_config"] = auth_config_module
    sys.modules["utils.auth"] = auth_module

    return fastapi


class _User:
    def __init__(self, disabled=False):
        self.disabled = disabled


class _AuthConfig:
    enabled = True
    user = _User(disabled=False)

    def is_auth_enabled(self):
        return self.enabled

    def get_user(self, username):
        return self.user


class _Payload:
    def __init__(self, sub="alice", token_type="access"):
        self.sub = sub
        self.type = token_type


class _Credentials:
    def __init__(self, credentials):
        self.credentials = credentials


class _WebSocket:
    def __init__(self, query_params):
        self.query_params = query_params
        self.closed = False

    async def close(self, *args, **kwargs):
        self.closed = True


class DependencyAuthTests(unittest.TestCase):
    def setUp(self):
        self.saved_runtime_modules = _save_modules(
            ("fastapi", "utils.auth_config", "utils.auth")
        )
        self.auth_config = _AuthConfig()
        self.auth_config.enabled = True
        self.auth_config.user = _User(disabled=False)

        self.auth_module = types.ModuleType("utils.auth")
        self.auth_module.decode_token = lambda token: _Payload()
        self.fastapi = _install_runtime_auth_stubs(self.auth_config, self.auth_module)

    def tearDown(self):
        _restore_modules(self.saved_runtime_modules)

    def test_optional_current_user_returns_none_when_auth_disabled(self):
        self.auth_config.enabled = False

        self.assertIsNone(dependencies.get_optional_current_user())

    def test_optional_current_user_requires_credentials_when_auth_enabled(self):
        with self.assertRaises(self.fastapi.HTTPException) as ctx:
            dependencies.get_optional_current_user(credentials=None)

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "Authentication required")
        self.assertEqual(ctx.exception.headers, {"WWW-Authenticate": "Bearer"})

    def test_optional_current_user_rejects_invalid_or_wrong_type_token(self):
        self.auth_module.decode_token = lambda token: _Payload(token_type="refresh")

        with self.assertRaises(self.fastapi.HTTPException) as ctx:
            dependencies.get_optional_current_user(_Credentials("token"))

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "Invalid or expired token")

    def test_optional_current_user_rejects_disabled_user(self):
        self.auth_config.user = _User(disabled=True)

        with self.assertRaises(self.fastapi.HTTPException) as ctx:
            dependencies.get_optional_current_user(_Credentials("token"))

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(
            ctx.exception.detail, "User account is disabled or does not exist"
        )

    def test_optional_current_user_returns_valid_username(self):
        self.assertEqual(
            dependencies.get_optional_current_user(_Credentials("token")), "alice"
        )

    def test_websocket_current_user_returns_none_when_auth_disabled(self):
        self.auth_config.enabled = False

        result = asyncio.run(dependencies.get_websocket_current_user(_WebSocket({})))

        self.assertIsNone(result)

    def test_websocket_current_user_requires_token_without_closing_socket(self):
        websocket = _WebSocket({})

        with self.assertRaises(dependencies.WebSocketException) as ctx:
            asyncio.run(dependencies.get_websocket_current_user(websocket))

        self.assertEqual(ctx.exception.code, 1008)
        self.assertEqual(ctx.exception.reason, "Authentication required")
        self.assertFalse(websocket.closed)

    def test_websocket_current_user_rejects_invalid_token(self):
        self.auth_module.decode_token = lambda token: None

        with self.assertRaises(dependencies.WebSocketException) as ctx:
            asyncio.run(
                dependencies.get_websocket_current_user(_WebSocket({"token": "bad"}))
            )

        self.assertEqual(ctx.exception.code, 1008)
        self.assertEqual(ctx.exception.reason, "Invalid or expired token")

    def test_websocket_current_user_returns_valid_username(self):
        result = asyncio.run(
            dependencies.get_websocket_current_user(_WebSocket({"token": "good"}))
        )

        self.assertEqual(result, "alice")


class DependencyWiringTests(unittest.TestCase):
    def setUp(self):
        dependencies._shared_instances.clear()

    def tearDown(self):
        dependencies._shared_instances.clear()

    def test_initialize_dependencies_stores_shared_instances_and_builds_helpers(self):
        process_handler = object()
        updater = object()
        websocket_manager = object()
        metrics_manager = object()
        status_manager = object()
        logger = object()

        with (
            patch.object(dependencies, "APIState") as api_state_cls,
            patch.object(dependencies, "MetricsCollector") as metrics_collector_cls,
        ):
            api_state_cls.return_value = types.SimpleNamespace(
                process_handler=process_handler,
                logger=logger,
            )
            metrics_collector_cls.return_value = types.SimpleNamespace(
                process_handler=process_handler,
                logger=logger,
            )

            dependencies.initialize_dependencies(
                process_handler,
                updater,
                websocket_manager,
                metrics_manager,
                status_manager,
                logger,
            )

        api_state_cls.assert_called_once_with(
            process_handler=process_handler, logger=logger
        )
        metrics_collector_cls.assert_called_once_with(
            process_handler=process_handler, logger=logger
        )

        self.assertIs(dependencies.get_process_handler(), process_handler)
        self.assertIs(dependencies.get_updater(), updater)
        self.assertIs(dependencies.get_websocket_manager(), websocket_manager)
        self.assertIs(dependencies.get_metrics_manager(), metrics_manager)
        self.assertIs(dependencies.get_status_manager(), status_manager)
        self.assertIs(dependencies.get_logger(), logger)

        api_state = dependencies.get_api_state()
        self.assertIs(api_state.process_handler, process_handler)
        self.assertIs(api_state.logger, logger)

        metrics_collector = dependencies.get_metrics_collector()
        self.assertIs(metrics_collector.process_handler, process_handler)
        self.assertIs(metrics_collector.logger, logger)

    def test_shared_instance_getters_raise_key_error_before_initialization(self):
        with self.assertRaises(KeyError):
            dependencies.get_process_handler()

        with self.assertRaises(KeyError):
            dependencies.get_api_state()


if __name__ == "__main__":
    unittest.main()
