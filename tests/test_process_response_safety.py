import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


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


class OptionalProcessStartupTests(unittest.TestCase):
    def setUp(self):
        for helper in (
            "infinidysk_postgres_migration_active",
            "infinidysk_namespace_migration_active",
        ):
            active_patch = patch.object(process_router, helper, return_value=False)
            active_patch.start()
            self.addCleanup(active_patch.stop)

    def test_optional_start_rejects_process_that_never_becomes_ready(self):
        updater = MagicMock()
        updater.auto_update.return_value = (object(), "readiness probe failed")

        with (
            patch.object(
                process_router,
                "wait_for_process_running",
                side_effect=(False, False),
            ),
            self.assertRaises(process_router.HTTPException) as raised,
        ):
            process_router._ensure_optional_process_running(
                "pgAdmin4",
                False,
                updater,
                object(),
            )

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(
            raised.exception.detail,
            "pgAdmin4 failed to start. readiness probe failed",
        )
        updater.auto_update.assert_called_once_with(
            "pgAdmin4",
            enable_update=False,
        )

    def test_optional_start_skips_update_when_already_running(self):
        updater = MagicMock()

        with patch.object(
            process_router,
            "wait_for_process_running",
            return_value=True,
        ):
            process_router._ensure_optional_process_running(
                "Bazarr",
                True,
                updater,
                object(),
            )

        updater.auto_update.assert_not_called()

    def test_custom_named_infinidysk_onboarding_rejects_postgres_before_mutation(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "db.sqlite").write_bytes(b"existing sqlite")
            service = {
                "enabled": False,
                "process_name": "Custom InfiniDysk",
                "postgres_enabled": False,
                "postgres_database": "infinidysk",
                "config_dir": temp_dir,
                "env": {
                    "CONFIG_PATH": temp_dir,
                    "DATABASE_PROVIDER": "sqlite",
                },
            }
            config_manager = types.SimpleNamespace(
                config={
                    "infinidysk": service,
                    "postgres": {
                        "host": "127.0.0.1",
                        "port": 5432,
                        "user": "DUMB",
                        "password": "postgres",
                        "config_dir": "/postgres_data",
                    },
                },
                find_key_for_process=lambda name: (
                    ("infinidysk", None)
                    if name == "Custom InfiniDysk"
                    else (None, None)
                ),
                save_config=MagicMock(),
            )
            request = types.SimpleNamespace(
                core_services=[
                    types.SimpleNamespace(
                        name="Custom InfiniDysk",
                        service_options={"infinidysk": {"postgres_enabled": True}},
                    )
                ],
                optional_services=[],
                optional_service_options={},
            )
            updater = MagicMock()

            with (
                patch.object(process_router, "CONFIG_MANAGER", config_manager),
                patch.object(
                    process_router,
                    "infinidysk_postgres_migration_active",
                    return_value=False,
                ),
                self.assertRaises(process_router.HTTPException) as raised,
            ):
                process_router._run_startup(
                    request,
                    updater,
                    MagicMock(),
                    MagicMock(),
                )

            self.assertEqual(raised.exception.status_code, 400)
            self.assertFalse(service["enabled"])
            self.assertFalse(service["postgres_enabled"])
            config_manager.save_config.assert_not_called()
            updater.auto_update.assert_not_called()

    def test_infinidysk_onboarding_holds_admission_through_startup(self):
        service = {"process_name": "Custom Disk"}
        config_manager = types.SimpleNamespace(
            config={"infinidysk": service},
            find_key_for_process=lambda name: (
                ("infinidysk", None) if name == "Custom Disk" else (None, None)
            ),
        )
        request = types.SimpleNamespace(
            core_services=[types.SimpleNamespace(name="Custom Disk")],
            optional_services=[],
            optional_service_options={},
        )
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def admitted(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=2)
            return {"status": "complete"}

        def run_startup():
            try:
                process_router._run_startup(
                    request,
                    MagicMock(),
                    MagicMock(),
                    MagicMock(),
                )
            except Exception as error:  # pragma: no cover - assertion below
                errors.append(error)

        with (
            patch.object(process_router, "CONFIG_MANAGER", config_manager),
            patch.object(
                process_router,
                "_run_startup_admitted",
                side_effect=admitted,
            ),
        ):
            worker = threading.Thread(target=run_startup)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=1))
                acquired = process_router.INFINIDYSK_MIGRATION_ADMISSION_LOCK.acquire(
                    blocking=False
                )
                if acquired:
                    process_router.INFINIDYSK_MIGRATION_ADMISSION_LOCK.release()
                self.assertFalse(acquired)
            finally:
                release.set()
                worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_postgres_optional_onboarding_is_blocked_by_each_active_job(self):
        request = types.SimpleNamespace(
            core_services=[],
            optional_services=["pgadmin"],
            optional_service_options={},
        )
        for active_helper in (
            "infinidysk_postgres_migration_active",
            "infinidysk_namespace_migration_active",
        ):
            with self.subTest(active_helper=active_helper):
                config_manager = types.SimpleNamespace(
                    config={"postgres": {"enabled": False}},
                    save_config=MagicMock(),
                )
                with (
                    patch.object(process_router, "CONFIG_MANAGER", config_manager),
                    patch.object(process_router, active_helper, return_value=True),
                    patch.object(process_router, "_run_startup_admitted") as admitted,
                    self.assertRaises(process_router.HTTPException) as raised,
                ):
                    process_router._run_startup(
                        request,
                        MagicMock(),
                        MagicMock(),
                        MagicMock(),
                    )

                self.assertEqual(raised.exception.status_code, 409)
                self.assertFalse(config_manager.config["postgres"]["enabled"])
                config_manager.save_config.assert_not_called()
                admitted.assert_not_called()

    def test_namespace_migration_blocks_unrelated_onboarding_before_mutation(self):
        request = types.SimpleNamespace(
            core_services=[types.SimpleNamespace(name="Plex")],
            optional_services=[],
            optional_service_options={},
        )
        config_manager = types.SimpleNamespace(
            config={"plex": {"enabled": False}},
            save_config=MagicMock(),
            find_key_for_process=MagicMock(return_value=("plex", None)),
        )
        with (
            patch.object(process_router, "CONFIG_MANAGER", config_manager),
            patch.object(
                process_router,
                "infinidysk_namespace_migration_active",
                return_value=True,
            ),
            patch.object(process_router, "_run_startup_admitted") as admitted,
            self.assertRaises(process_router.HTTPException) as raised,
        ):
            process_router._run_startup(
                request,
                MagicMock(),
                MagicMock(),
                MagicMock(),
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertFalse(config_manager.config["plex"]["enabled"])
        config_manager.save_config.assert_not_called()
        admitted.assert_not_called()

    def test_postgres_core_and_dependent_optional_require_admission(self):
        postgres_request = types.SimpleNamespace(
            core_services=[types.SimpleNamespace(name="PostgreSQL")],
            optional_services=[],
        )
        optional_request = types.SimpleNamespace(
            core_services=[],
            optional_services=["pgadmin"],
        )

        self.assertTrue(
            process_router._startup_requires_migration_admission(postgres_request)
        )
        self.assertTrue(
            process_router._startup_requires_migration_admission(optional_request)
        )

    def test_postgres_service_options_are_blocked_by_each_active_job(self):
        for active_helper in (
            "infinidysk_postgres_migration_active",
            "infinidysk_namespace_migration_active",
        ):
            with self.subTest(active_helper=active_helper):
                postgres = {"process_name": "PostgreSQL", "password": "old"}
                config_manager = types.SimpleNamespace(
                    config={"infinidysk": {}, "postgres": postgres},
                    save_config=MagicMock(),
                )
                with (
                    patch.object(process_router, "CONFIG_MANAGER", config_manager),
                    patch.object(process_router, active_helper, return_value=True),
                    self.assertRaises(process_router.HTTPException) as raised,
                ):
                    process_router.apply_service_options(
                        postgres,
                        {"password": "new"},
                        MagicMock(),
                        service_key="postgres",
                    )

                self.assertEqual(raised.exception.status_code, 409)
                self.assertEqual(postgres["password"], "old")
                config_manager.save_config.assert_not_called()

    def test_lifecycle_admission_blocks_before_any_mutation(self):
        callback = MagicMock(return_value={"status": "unexpected"})
        config_manager = types.SimpleNamespace(
            find_key_for_process=MagicMock(return_value=("infinidysk", None))
        )
        with (
            patch.object(process_router, "CONFIG_MANAGER", config_manager),
            patch.object(
                process_router,
                "infinidysk_postgres_migration_active",
                return_value=True,
            ),
            patch.object(
                process_router,
                "infinidysk_namespace_migration_active",
                return_value=False,
            ),
            self.assertRaises(process_router.HTTPException) as raised,
        ):
            process_router._run_migration_lifecycle_admitted(
                "InfiniDysk", "restart", callback
            )

        self.assertEqual(raised.exception.status_code, 409)
        callback.assert_not_called()

        config_manager.find_key_for_process.return_value = ("sonarr", "TV")
        with (
            patch.object(process_router, "CONFIG_MANAGER", config_manager),
            patch.object(
                process_router,
                "infinidysk_postgres_migration_active",
                return_value=False,
            ),
            patch.object(
                process_router,
                "infinidysk_namespace_migration_active",
                return_value=True,
            ),
            self.assertRaises(process_router.HTTPException),
        ):
            process_router._run_migration_lifecycle_admitted(
                "Sonarr TV", "reset", callback
            )

        callback.assert_not_called()

    def test_postgres_service_options_validate_before_mutation(self):
        postgres = {
            "process_name": "PostgreSQL",
            "password": "old",
            "config_dir": "/postgres_data",
        }
        config_manager = types.SimpleNamespace(
            config={"infinidysk": {}, "postgres": postgres},
            save_config=MagicMock(),
        )
        with (
            patch.object(process_router, "CONFIG_MANAGER", config_manager),
            patch.object(
                process_router,
                "_validate_postgres_service_options",
                side_effect=process_router.HTTPException(
                    status_code=400, detail="target binding changed"
                ),
            ),
            self.assertRaises(process_router.HTTPException) as raised,
        ):
            process_router.apply_service_options(
                postgres,
                {"config_dir": "/other-cluster"},
                MagicMock(),
                service_key="postgres",
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(postgres["config_dir"], "/postgres_data")
        config_manager.save_config.assert_not_called()

        with (
            patch.object(process_router, "CONFIG_MANAGER", config_manager),
            patch.object(
                process_router,
                "_validate_postgres_service_options",
                return_value=None,
            ),
        ):
            process_router.apply_service_options(
                postgres,
                {"password": "rotated"},
                MagicMock(),
                service_key="postgres",
            )

        self.assertEqual(postgres["password"], "rotated")
        config_manager.save_config.assert_called_once_with()


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

    async def test_compatibility_cutover_takes_admission_before_update_lock(self):
        events = []

        class AdmissionLock:
            def __enter__(self):
                events.append("admission_enter")
                return self

            def __exit__(self, *_args):
                events.append("admission_exit")

        class UpdateLock:
            def acquire(self, blocking=False):
                self.assert_admitted = events[-1] == "admission_enter"
                events.append("update_acquire")
                return True

            def release(self):
                events.append("update_release")

        request = types.SimpleNamespace(
            mode="retain_legacy_namespace",
            rename_attached_services=True,
            confirmation="MIGRATE TO INFINIDYSK",
            preflight_token=None,
            acknowledge_downtime=False,
            acknowledge_library_scan=False,
            acknowledge_rollback_limits=False,
            acknowledge_external_backup=True,
        )
        update_lock = UpdateLock()

        def apply(*_args):
            events.append("apply")
            return {"status": "completed"}

        with (
            patch.object(
                process_router,
                "INFINIDYSK_MIGRATION_ADMISSION_LOCK",
                AdmissionLock(),
            ),
            patch.object(
                process_router.INFINIDYSK_MIGRATION_MANAGER,
                "apply_brand_cutover",
                side_effect=apply,
            ),
        ):
            result = await process_router.apply_infinidysk_migration(
                request,
                process_handler=types.SimpleNamespace(
                    get_startup_status=lambda: {"phase": "ready"}
                ),
                updater=types.SimpleNamespace(updating=update_lock),
                logger=types.SimpleNamespace(),
                current_user=None,
            )

        self.assertTrue(update_lock.assert_admitted)
        self.assertEqual(
            [
                "admission_enter",
                "update_acquire",
                "apply",
                "update_release",
                "admission_exit",
            ],
            events,
        )
        self.assertEqual("completed", result["status"])

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

    async def test_infinidysk_job_status_normalizes_an_empty_job_to_null(self):
        with patch.object(
            process_router.INFINIDYSK_MIGRATION_MANAGER,
            "get_job",
            return_value={},
        ):
            result = await process_router.get_infinidysk_migration_job_status(
                job_id=None, current_user=None
            )

        self.assertEqual({"job": None}, result)

    async def test_infinidysk_cleanup_preview_forwards_manager_response(self):
        preview = {
            "available": True,
            "preview_token": "preview-token",
            "deletion": {
                "files": 2,
                "directories": 1,
                "bytes": 10,
                "categories": ["Migration state details"],
            },
        }
        with patch.object(
            process_router.INFINIDYSK_MIGRATION_MANAGER,
            "cleanup_preview",
            return_value=preview,
        ) as cleanup_preview:
            result = await process_router.preview_infinidysk_migration_cleanup(
                current_user=None
            )

        self.assertEqual(preview, result)
        cleanup_preview.assert_called_once_with()

    async def test_infinidysk_cleanup_requires_exact_confirmation_and_acknowledgements(
        self,
    ):
        base = {
            "preview_token": "preview-token",
            "confirmation": "REMOVE INFINIDYSK MIGRATION DATA",
            "acknowledge_validation": True,
            "acknowledge_rollback_loss": True,
        }
        invalid_requests = (
            ({**base, "confirmation": "remove"}, "REMOVE INFINIDYSK"),
            (
                {**base, "confirmation": " REMOVE INFINIDYSK MIGRATION DATA"},
                "REMOVE INFINIDYSK",
            ),
            ({**base, "acknowledge_validation": False}, "health"),
            ({**base, "acknowledge_validation": 1}, "health"),
            ({**base, "acknowledge_rollback_loss": False}, "irreversible"),
        )
        for values, detail in invalid_requests:
            with self.subTest(detail=detail):
                with self.assertRaises(process_router.HTTPException) as raised:
                    await process_router.cleanup_infinidysk_migration(
                        types.SimpleNamespace(**values), current_user=None
                    )
                self.assertEqual(400, raised.exception.status_code)
                self.assertIn(detail, raised.exception.detail)

    async def test_infinidysk_cleanup_forwards_verified_preview_token(self):
        request = types.SimpleNamespace(
            preview_token="preview-token",
            confirmation="REMOVE INFINIDYSK MIGRATION DATA",
            acknowledge_validation=True,
            acknowledge_rollback_loss=True,
        )
        cleaned = {
            "status": "completed",
            "cleanup_finalized": True,
            "deleted": {
                "files": 2,
                "directories": 1,
                "bytes": 10,
                "categories": ["Job history"],
            },
        }
        with patch.object(
            process_router.INFINIDYSK_MIGRATION_MANAGER,
            "cleanup",
            return_value=cleaned,
        ) as cleanup:
            result = await process_router.cleanup_infinidysk_migration(
                request, current_user=None
            )

        self.assertEqual(cleaned, result)
        cleanup.assert_called_once_with(
            "preview-token",
            "REMOVE INFINIDYSK MIGRATION DATA",
            True,
            True,
        )

    async def test_infinidysk_cleanup_conflict_is_safe(self):
        request = types.SimpleNamespace(
            preview_token="preview-token",
            confirmation="REMOVE INFINIDYSK MIGRATION DATA",
            acknowledge_validation=True,
            acknowledge_rollback_loss=True,
        )
        with (
            patch.object(
                process_router.INFINIDYSK_MIGRATION_MANAGER,
                "cleanup",
                side_effect=process_router.InfiniDyskMigrationError(
                    "Run the cleanup preview again."
                ),
            ),
            self.assertRaises(process_router.HTTPException) as raised,
        ):
            await process_router.cleanup_infinidysk_migration(
                request, current_user=None
            )

        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual("Run the cleanup preview again.", raised.exception.detail)

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
        self.assertTrue(capabilities["infinidysk_migration_cleanup"])

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
