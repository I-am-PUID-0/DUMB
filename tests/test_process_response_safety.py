import json
import sys
import types
import unittest
from unittest.mock import patch


def _install_process_router_stubs():
    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code=500, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class APIRouter:
        def get(self, *args, **kwargs):
            return lambda func: func

        def post(self, *args, **kwargs):
            return lambda func: func

    fastapi.APIRouter = APIRouter
    fastapi.HTTPException = HTTPException
    fastapi.Depends = lambda *args, **kwargs: None
    fastapi.Query = lambda default=None, *args, **kwargs: default
    fastapi.WebSocket = type("WebSocket", (), {})
    sys.modules["fastapi"] = fastapi

    fastapi_concurrency = types.ModuleType("fastapi.concurrency")

    async def run_in_threadpool(func, *args, **kwargs):
        return func(*args, **kwargs)

    fastapi_concurrency.run_in_threadpool = run_in_threadpool
    sys.modules["fastapi.concurrency"] = fastapi_concurrency

    pydantic = types.ModuleType("pydantic")

    class BaseModel:
        def __init_subclass__(cls, **kwargs):
            return super().__init_subclass__()

    pydantic.BaseModel = BaseModel
    pydantic.ConfigDict = lambda **kwargs: dict(kwargs)
    sys.modules["pydantic"] = pydantic

    dependencies = types.ModuleType("utils.dependencies")
    for name in (
        "get_process_handler",
        "get_logger",
        "get_api_state",
        "get_updater",
        "get_optional_current_user",
    ):
        setattr(dependencies, name, lambda *args, **kwargs: None)
    sys.modules["utils.dependencies"] = dependencies

    config_loader = types.ModuleType("utils.config_loader")
    config_loader.CONFIG_MANAGER = types.SimpleNamespace(get=lambda *args, **kwargs: {})
    config_loader.find_service_config = lambda *args, **kwargs: (None, None)
    sys.modules["utils.config_loader"] = config_loader

    setup = types.ModuleType("utils.setup")
    setup.COMMIT_PIN_SERVICE_KEYS = set()
    setup.ensure_managed_postgres_database = lambda *args, **kwargs: None
    setup.setup_project = lambda *args, **kwargs: None
    sys.modules["utils.setup"] = setup

    core_services = types.ModuleType("utils.core_services")
    core_services.has_core_service = lambda *args, **kwargs: False
    sys.modules["utils.core_services"] = core_services

    dependency_map = types.ModuleType("utils.dependency_map")
    dependency_map.build_conditional_dependency_map = lambda *args, **kwargs: {}
    dependency_map.filter_conditional_deps_for_instance = lambda *args, **kwargs: {}
    sys.modules["utils.dependency_map"] = dependency_map

    versions = types.ModuleType("utils.versions")
    versions.Versions = lambda *args, **kwargs: types.SimpleNamespace()
    sys.modules["utils.versions"] = versions

    psutil = types.ModuleType("psutil")
    psutil.pid_exists = lambda pid: False
    sys.modules.setdefault("psutil", psutil)


_install_process_router_stubs()
sys.modules.pop("api.routers.process", None)


from api.routers import process as process_router


class ProcessResponseSanitizerTests(unittest.TestCase):
    def test_sanitizes_traceback_keys_recursively(self):
        payload = {
            "status": "error",
            "details": {
                "traceback": "Traceback (most recent call last):\nsecret",
                "nested": [{"stack_trace": 'File "x.py", line 1'}],
            },
            "message": "kept",
        }

        cleaned = process_router._sanitize_stacktrace_payload(payload)

        self.assertEqual(cleaned["details"]["traceback"], "Internal error")
        self.assertEqual(
            cleaned["details"]["nested"][0]["stack_trace"], "Internal error"
        )
        self.assertEqual(cleaned["message"], "kept")

    def test_sanitizes_stacktrace_like_strings_in_lists_and_tuples(self):
        payload = [
            "normal",
            "Traceback (most recent call last): boom",
            ('File "worker.py", line 22', "still normal"),
        ]

        cleaned = process_router._sanitize_stacktrace_payload(payload)

        self.assertEqual(cleaned[0], "normal")
        self.assertEqual(cleaned[1], "Internal error")
        self.assertEqual(cleaned[2][0], "Internal error")
        self.assertEqual(cleaned[2][1], "still normal")

    def test_safe_api_response_serializes_non_json_values_after_sanitizing(self):
        payload = {
            "items": {"alpha", "beta"},
            "error": "Traceback (most recent call last): secret",
        }

        response = process_router._safe_api_response(payload)

        self.assertIsInstance(response["items"], str)
        self.assertEqual(response["error"], "Internal error")


class MediaStormCredentialResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_credential_response_is_no_store_and_capability_gated(self):
        with (
            patch.object(
                process_router.CONFIG_MANAGER,
                "get",
                return_value={"config_dir": "/mediastorm"},
            ),
            patch.object(
                process_router,
                "read_initial_admin_password",
                return_value="generated-secret",
            ),
        ):
            response = await process_router.get_mediastorm_initial_admin_password(None)

        self.assertEqual(response.headers["cache-control"], "no-store, private")
        self.assertEqual(response.headers["pragma"], "no-cache")
        self.assertEqual(
            json.loads(response.body),
            {
                "available": True,
                "username": "admin",
                "password": "generated-secret",
                "credential_kind": "installation_specific",
            },
        )

        capabilities = await process_router.get_capabilities(None)
        self.assertTrue(capabilities["mediastorm_initial_admin_password"])

    async def test_missing_credential_does_not_return_a_password(self):
        with (
            patch.object(
                process_router.CONFIG_MANAGER,
                "get",
                return_value={"config_dir": "/mediastorm"},
            ),
            patch.object(
                process_router,
                "read_initial_admin_password",
                return_value=None,
            ),
        ):
            response = await process_router.get_mediastorm_initial_admin_password(None)

        self.assertEqual(
            json.loads(response.body),
            {
                "available": False,
                "username": "admin",
                "password": None,
                "credential_kind": None,
            },
        )

    async def test_default_credential_is_identified(self):
        with (
            patch.object(
                process_router.CONFIG_MANAGER,
                "get",
                return_value={"config_dir": "/mediastorm"},
            ),
            patch.object(
                process_router,
                "read_initial_admin_password",
                return_value="admin",
            ),
        ):
            response = await process_router.get_mediastorm_initial_admin_password(None)

        self.assertEqual(
            json.loads(response.body),
            {
                "available": True,
                "username": "admin",
                "password": "admin",
                "credential_kind": "default",
            },
        )


if __name__ == "__main__":
    unittest.main()
