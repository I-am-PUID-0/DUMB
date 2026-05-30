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


class FakeAPIState:
    def __init__(self):
        self.statuses = []
        self.notices = {"applied": [], "info": []}

    def get_update_statuses(self):
        return self.statuses

    def get_update_notices(self):
        return self.notices


class ProcessUpdateNoticeHelperTests(unittest.TestCase):
    def test_update_notes_target_prefers_compare_for_branch_markers(self):
        url, label = process_router._update_notes_target(
            "https://github.com/example/service", "beta-abc1234", "beta-def5678"
        )

        self.assertEqual(
            url, "https://github.com/example/service/compare/abc1234...def5678"
        )
        self.assertEqual(label, "Compare commits")

    def test_update_notes_target_links_dumb_dev_build_release(self):
        url, label = process_router._update_notes_target(
            "https://github.com/I-am-PUID-0/DUMB", None, "2.5.0-dev.1"
        )

        self.assertEqual(
            url, "https://github.com/I-am-PUID-0/DUMB/releases/tag/dev-build"
        )
        self.assertEqual(label, "View dev build")

    def test_update_notes_target_links_release_tag(self):
        url, label = process_router._update_notes_target(
            "https://github.com/example/service", "1.0.0", "1.1.0"
        )

        self.assertEqual(url, "https://github.com/example/service/releases/tag/1.1.0")
        self.assertEqual(label, "Release notes")

    def test_build_update_notice_entry_preserves_explicit_release_url(self):
        entry = process_router._build_update_notice_entry(
            {
                "process_name": "Radarr",
                "status": "update_available",
                "current_version": "1.0.0",
                "available_version": "1.1.0",
                "release_url": "https://example.test/custom",
                "notes_label": "Custom notes",
            },
            {
                "process_name": "Radarr",
                "name": "Radarr Main",
                "repo_url": "https://github.com/example/radarr",
            },
        )

        self.assertEqual(entry["type"], "available")
        self.assertEqual(entry["display_name"], "Radarr Main")
        self.assertEqual(entry["release_url"], "https://example.test/custom")
        self.assertEqual(entry["notes_label"], "Custom notes")

    def test_update_notices_filters_project_scope_to_dumb_processes(self):
        api_state = FakeAPIState()
        api_state.statuses = [
            {
                "process_name": "DUMB API",
                "status": "update_available",
                "available_version": "2.6.0",
            },
            {
                "process_name": "Radarr",
                "status": "update_available",
                "available_version": "1.1.0",
            },
        ]
        process_entries = [
            {
                "process_name": "DUMB API",
                "name": "DUMB API",
                "repo_url": "https://github.com/I-am-PUID-0/DUMB",
            },
            {
                "process_name": "Radarr",
                "name": "Radarr",
                "repo_url": "https://github.com/example/radarr",
            },
        ]

        with patch.object(
            process_router, "_collect_process_entries", return_value=process_entries
        ):
            result = process_router.update_notices(scope="project", api_state=api_state)

        self.assertEqual(result["scope"], "project")
        self.assertEqual(
            [item["process_name"] for item in result["available"]], ["DUMB API"]
        )

    def test_update_notices_all_scope_includes_non_dumb_processes(self):
        api_state = FakeAPIState()
        api_state.statuses = [
            {
                "process_name": "Radarr",
                "status": "update_available",
                "available_version": "1.1.0",
            },
        ]

        with patch.object(process_router, "_collect_process_entries", return_value=[]):
            result = process_router.update_notices(scope="all", api_state=api_state)

        self.assertEqual(
            [item["process_name"] for item in result["available"]], ["Radarr"]
        )


if __name__ == "__main__":
    unittest.main()
