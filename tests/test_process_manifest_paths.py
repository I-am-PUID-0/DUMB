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


class ProcessManifestPathTests(unittest.TestCase):
    def test_empty_manifest_path_defaults_to_latest_snapshot(self):
        self.assertEqual(
            process_router._resolve_snapshot_manifest_path(""),
            "/config/symlink-repair/snapshots/latest.json",
        )

    def test_relative_manifest_path_resolves_under_snapshot_root(self):
        self.assertEqual(
            process_router._resolve_snapshot_manifest_path("radarr/latest.json"),
            "/config/symlink-repair/snapshots/radarr/latest.json",
        )

    def test_absolute_manifest_path_inside_snapshot_root_is_allowed(self):
        path = "/config/symlink-repair/snapshots/radarr/latest.json"

        self.assertEqual(process_router._resolve_snapshot_manifest_path(path), path)

    def test_manifest_path_traversal_is_rejected(self):
        with self.assertRaises(process_router.HTTPException) as ctx:
            process_router._resolve_snapshot_manifest_path("../secrets.json")

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("manifest_path must stay within", ctx.exception.detail)

    def test_absolute_manifest_path_outside_snapshot_root_is_rejected(self):
        with self.assertRaises(process_router.HTTPException):
            process_router._resolve_snapshot_manifest_path("/etc/passwd")

    def test_snapshot_filename_glob_uses_basename_only(self):
        glob_path = process_router._snapshot_filename_glob(
            "Radarr Main", "../../{process_slug}-{date}.json"
        )

        self.assertEqual(
            glob_path,
            "/config/symlink-repair/snapshots/radarr-main-*.json",
        )

    def test_is_path_within_handles_sibling_prefixes(self):
        self.assertFalse(
            process_router._is_path_within(
                "/config/symlink-repair/snapshots",
                "/config/symlink-repair/snapshots-old/latest.json",
            )
        )

    def test_backup_manifest_list_is_empty_when_snapshot_root_is_missing(self):
        with (
            patch.object(
                process_router.CONFIG_MANAGER,
                "config",
                {"decypharr": {"symlink_backup_path": ""}},
                create=True,
            ),
            patch.object(
                process_router,
                "find_service_config",
                return_value={"symlink_backup_path": ""},
            ),
            patch.object(
                process_router.os,
                "listdir",
                side_effect=FileNotFoundError,
            ),
        ):
            response = process_router.symlink_backup_manifests("Decypharr")

        self.assertEqual(response["process_name"], "Decypharr")
        self.assertEqual(response["manifests"], [])
        self.assertEqual(response["count"], 0)


if __name__ == "__main__":
    unittest.main()
