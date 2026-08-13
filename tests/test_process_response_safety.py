import json
import sys
import threading
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
    pydantic.Field = lambda default=None, **kwargs: default
    sys.modules["pydantic"] = pydantic

    dependencies = types.ModuleType("utils.dependencies")
    for name in (
        "get_process_handler",
        "get_logger",
        "get_api_state",
        "get_updater",
        "get_optional_current_user",
        "get_media_protection_manager",
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
    setup.read_nzbdav_install_info = lambda *args, **kwargs: None
    setup.setup_project = lambda *args, **kwargs: None
    sys.modules["utils.setup"] = setup

    core_services = types.ModuleType("utils.core_services")
    core_services.has_core_service = lambda *args, **kwargs: False
    core_services.get_core_services = lambda *args, **kwargs: {}
    sys.modules["utils.core_services"] = core_services

    dependency_map = types.ModuleType("utils.dependency_map")
    dependency_map.build_conditional_dependency_map = lambda *args, **kwargs: {}
    dependency_map.filter_conditional_deps_for_instance = lambda *args, **kwargs: {}
    sys.modules["utils.dependency_map"] = dependency_map

    versions = types.ModuleType("utils.versions")
    versions.Versions = lambda *args, **kwargs: types.SimpleNamespace(
        display_version=lambda _key, version: version,
    )
    sys.modules["utils.versions"] = versions

    install_cache = types.ModuleType("utils.install_cache")
    install_cache.INSTALL_CACHE = types.SimpleNamespace(
        status=lambda: {},
        verify=lambda: {},
        prune=lambda *args: {},
        clear_artifacts=lambda *args: {},
        cleanup=lambda *args: {},
    )
    sys.modules["utils.install_cache"] = install_cache

    psutil = types.ModuleType("psutil")
    psutil.pid_exists = lambda pid: False
    sys.modules.setdefault("psutil", psutil)


_install_process_router_stubs()
sys.modules.pop("api.routers.process", None)


from api.routers import process as process_router


class ProcessResponseSanitizerTests(unittest.TestCase):
    def test_nzbdav_project_links_include_maintained_fork_sponsor(self):
        repo_url, sponsorship_url = process_router._service_project_urls(
            "infinidysk",
            {"repo_owner": "infinidysk", "repo_name": "infinidysk"},
        )

        self.assertEqual(repo_url, "https://github.com/infinidysk/infinidysk")
        self.assertEqual(sponsorship_url, "https://buymeacoffee.com/hoivikaj")

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
    def test_runtime_log_level_routes_use_the_shared_api_logger(self):
        logger = object()
        expected = {
            "effective_level": "DEBUG",
            "override_active": True,
        }
        request = types.SimpleNamespace(debug_enabled=True)

        with (
            patch.object(
                process_router,
                "get_runtime_log_level_state",
                return_value=expected,
            ) as get_state,
            patch.object(
                process_router,
                "set_runtime_debug_logging",
                return_value=expected,
            ) as set_debug,
        ):
            self.assertEqual(
                process_router.runtime_log_level(logger=logger, current_user=None),
                expected,
            )
            self.assertEqual(
                process_router.update_runtime_log_level(
                    request,
                    logger=logger,
                    current_user=None,
                ),
                expected,
            )

        get_state.assert_called_once_with(logger)
        set_debug.assert_called_once_with(True, logger)

    async def test_install_cache_maintenance_is_serialized_with_updates(self):
        process_handler = types.SimpleNamespace(
            get_startup_status=lambda: {"phase": "ready"}
        )
        update_lock = threading.Lock()
        updater = types.SimpleNamespace(updating=update_lock)

        result = await process_router._run_install_cache_maintenance(
            lambda: "complete",
            process_handler=process_handler,
            updater=updater,
        )

        self.assertEqual(result, "complete")
        self.assertFalse(update_lock.locked())

        update_lock.acquire()
        try:
            with self.assertRaises(process_router.HTTPException) as raised:
                await process_router._run_install_cache_maintenance(
                    lambda: "unsafe",
                    process_handler=process_handler,
                    updater=updater,
                )
            self.assertEqual(raised.exception.status_code, 409)
        finally:
            update_lock.release()

    async def test_install_cache_maintenance_waits_for_startup(self):
        process_handler = types.SimpleNamespace(
            get_startup_status=lambda: {"phase": "preinstalling"}
        )

        with self.assertRaises(process_router.HTTPException) as raised:
            await process_router._run_install_cache_maintenance(
                lambda: "unsafe",
                process_handler=process_handler,
                updater=types.SimpleNamespace(updating=threading.Lock()),
            )

        self.assertEqual(raised.exception.status_code, 409)

    async def test_full_infinidysk_migration_requires_independent_backup(self):
        request = types.SimpleNamespace(
            mode="full_namespace",
            rename_attached_services=True,
            confirmation="MIGRATE TO INFINIDYSK",
            preflight_token="preflight-token",
            acknowledge_downtime=True,
            acknowledge_library_scan=True,
            acknowledge_rollback_limits=True,
            acknowledge_external_backup=False,
        )

        with self.assertRaises(process_router.HTTPException) as raised:
            await process_router.apply_infinidysk_migration(
                request,
                process_handler=types.SimpleNamespace(
                    get_startup_status=lambda: {"phase": "ready"}
                ),
                updater=types.SimpleNamespace(updating=threading.Lock()),
                logger=types.SimpleNamespace(),
                current_user=None,
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("independent backup", raised.exception.detail)

    async def test_compatibility_infinidysk_migration_requires_independent_backup(self):
        request = types.SimpleNamespace(
            mode="retain_legacy_namespace",
            rename_attached_services=True,
            confirmation="MIGRATE TO INFINIDYSK",
            preflight_token=None,
            acknowledge_downtime=False,
            acknowledge_library_scan=False,
            acknowledge_rollback_limits=False,
            acknowledge_external_backup=False,
        )

        with self.assertRaises(process_router.HTTPException) as raised:
            await process_router.apply_infinidysk_migration(
                request,
                process_handler=types.SimpleNamespace(
                    get_startup_status=lambda: {"phase": "ready"}
                ),
                updater=types.SimpleNamespace(updating=threading.Lock()),
                logger=types.SimpleNamespace(),
                current_user=None,
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("independent backup", raised.exception.detail)

    async def test_infinidysk_playback_stop_requires_explicit_confirmation(self):
        request = types.SimpleNamespace(
            job_id="a" * 32,
            confirmation="stop",
        )

        with self.assertRaises(process_router.HTTPException) as raised:
            await process_router.stop_playback_for_infinidysk_migration(
                request,
                current_user=None,
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("STOP ACTIVE PLAYBACK", raised.exception.detail)

    async def test_infinidysk_playback_stop_forwards_confirmed_job(self):
        request = types.SimpleNamespace(
            job_id="a" * 32,
            confirmation="STOP ACTIVE PLAYBACK",
        )
        job = {"job_id": request.job_id, "playback_stop_requested": True}

        with patch.object(
            process_router.INFINIDYSK_MIGRATION_MANAGER,
            "request_playback_stop",
            return_value=job,
        ) as stop_playback:
            result = await process_router.stop_playback_for_infinidysk_migration(
                request,
                current_user=None,
            )

        self.assertEqual({"job": job}, result)
        stop_playback.assert_called_once_with(request.job_id)

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
        self.assertTrue(capabilities["transactional_service_installs"])
        self.assertTrue(capabilities["install_cache_management"])
        self.assertTrue(capabilities["install_cache_cleanup"])
        self.assertTrue(capabilities["install_cache_limit_settings"])
        self.assertTrue(capabilities["service_reset"])
        self.assertTrue(capabilities["runtime_api_log_level"])
        self.assertTrue(capabilities["nzbdav_install_info"])

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
