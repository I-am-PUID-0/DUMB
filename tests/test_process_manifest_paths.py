import asyncio
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
    config_loader.CONFIG_MANAGER = types.SimpleNamespace(
        get=lambda *args, **kwargs: {}, config={}
    )
    config_loader.find_service_config = lambda *args, **kwargs: (None, None)
    sys.modules["utils.config_loader"] = config_loader

    setup = types.ModuleType("utils.setup")
    setup.COMMIT_PIN_SERVICE_KEYS = set()
    setup.ensure_managed_postgres_database = lambda *args, **kwargs: None
    setup.read_nzbdav_install_info = lambda *args, **kwargs: {}
    setup.setup_project = lambda *args, **kwargs: None
    setup._prepare_whisparr_profile_config = lambda *args, **kwargs: None
    sys.modules["utils.setup"] = setup

    core_services = types.ModuleType("utils.core_services")
    core_services.get_core_services = lambda *args, **kwargs: []
    core_services.has_core_service = lambda *args, **kwargs: False
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

    def test_profile_manifest_browser_uses_registered_namespace_not_raw_directory(self):
        reference = "dumb-backup://nas/symlink-decypharr/" + "a" * 32
        with (
            patch.object(
                process_router,
                "find_service_config",
                return_value={"backup_destination_id": "nas"},
            ),
            patch(
                "utils.symlink_repair.list_symlink_backup_manifests",
                return_value={
                    "directory": "dumb-backup://nas/symlink-decypharr",
                    "files": [{"path": reference, "sha256": "b" * 64}],
                    "count": 1,
                },
            ) as listing,
            patch.object(
                process_router.os, "listdir", side_effect=AssertionError("raw browse")
            ),
        ):
            result = process_router.symlink_backup_manifests("Decypharr")
        listing.assert_called_once_with("nas", "Decypharr", scheduled_only=True)
        self.assertEqual(result["manifests"][0]["path"], reference)
        self.assertEqual(result["count"], 1)

    def test_unavailable_profile_browser_does_not_fall_back_to_local_snapshots(self):
        with (
            patch.object(
                process_router,
                "find_service_config",
                return_value={"backup_destination_id": "nas"},
            ),
            patch(
                "utils.symlink_repair.list_symlink_backup_manifests",
                side_effect=process_router.BackupStorageError("Mount unavailable"),
            ),
            patch.object(process_router.os, "listdir") as legacy_list,
        ):
            with self.assertRaises(process_router.HTTPException) as error:
                process_router.symlink_backup_manifests("Decypharr")
        self.assertEqual(error.exception.status_code, 409)
        legacy_list.assert_not_called()

    def test_manual_profile_browser_is_catalog_only(self):
        response = {
            "directory": "dumb-backup://nas/symlink-manual",
            "files": [],
            "count": 0,
        }
        with patch(
            "utils.symlink_repair.list_symlink_backup_manifests", return_value=response
        ) as listing:
            result = process_router.symlink_manifest_files(
                "/etc/passwd", backup_destination_id="nas", process_name="Manual"
            )
        listing.assert_called_once_with("nas", "Manual", reference=None)
        self.assertEqual(result, response)

    def test_manual_backup_explicit_profile_is_additive_and_legacy_path_is_kept(self):
        request = types.SimpleNamespace(
            roots=[],
            process_name="Manual",
            backup_path="/chosen/custom.json",
            backup_destination_id="nas",
            include_broken=True,
        )
        response = {"backup_manifest": "dumb-backup://nas/symlink-manual/" + "a" * 32}
        with patch(
            "utils.symlink_repair.backup_symlink_manifest", return_value=response
        ) as writer:
            result = asyncio.run(process_router.symlink_manifest_backup(request))
        self.assertEqual(result, response)
        self.assertEqual(writer.call_args.args, ([], "/chosen/custom.json", True))
        self.assertEqual(
            writer.call_args.kwargs,
            {"backup_destination_id": "nas", "process_name": "Manual"},
        )
        request.backup_destination_id = None
        with patch(
            "utils.symlink_repair.backup_symlink_manifest", return_value={}
        ) as writer:
            asyncio.run(process_router.symlink_manifest_backup(request))
        writer.assert_called_once_with([], "/chosen/custom.json", True)

    def test_manual_backup_without_path_or_explicit_destination_is_rejected(self):
        request = types.SimpleNamespace(
            roots=[],
            process_name="Manual",
            backup_path=None,
            backup_destination_id=None,
            include_broken=True,
        )
        with patch("utils.symlink_repair.backup_symlink_manifest") as writer:
            with self.assertRaises(process_router.HTTPException) as error:
                asyncio.run(process_router.symlink_manifest_backup(request))
        self.assertEqual(error.exception.status_code, 400)
        writer.assert_not_called()

    def test_managed_restore_explicit_old_profile_overrides_current_service_choice(
        self,
    ):
        reference = "dumb-backup://old/symlink-manual/" + "a" * 32
        with patch.object(
            process_router,
            "find_service_config",
            return_value={"backup_destination_id": "new"},
        ) as find:
            options = process_router._symlink_restore_options(
                "Manual", "old", reference
            )
        self.assertEqual(
            options, {"process_name": "Manual", "backup_destination_id": "old"}
        )
        find.assert_not_called()

    def test_managed_restore_infers_saved_profile_only_when_context_is_supplied(self):
        reference = "dumb-backup://nas/symlink-manual/" + "a" * 32
        with patch.object(
            process_router,
            "find_service_config",
            return_value={"backup_destination_id": "nas"},
        ):
            options = process_router._symlink_restore_options("Manual", None, reference)
            global_options = process_router._symlink_restore_options(
                None, None, reference
            )
            legacy_options = process_router._symlink_restore_options(
                "Manual", None, "/chosen/custom.json"
            )
        self.assertEqual(options["backup_destination_id"], "nas")
        self.assertEqual(
            global_options, {"process_name": None, "backup_destination_id": None}
        )
        self.assertEqual(legacy_options, {})

    def test_async_manual_profile_backup_carries_selection_into_background_worker(self):
        request = types.SimpleNamespace(
            roots=[],
            process_name="Manual",
            backup_path=None,
            backup_destination_id="nas",
            include_broken=True,
        )
        state = types.SimpleNamespace(
            create_symlink_job=lambda **_kwargs: {"job_id": "job"},
            update_symlink_job=types.SimpleNamespace(),
        )
        from unittest.mock import Mock

        state.update_symlink_job = Mock()
        reference = "dumb-backup://nas/symlink-manual/" + "a" * 32

        def inline_thread(*_args, **kwargs):
            return types.SimpleNamespace(start=kwargs["target"])

        with (
            patch.object(process_router.threading, "Thread", side_effect=inline_thread),
            patch(
                "utils.symlink_repair.backup_symlink_manifest",
                return_value={"backup_manifest": reference},
            ) as writer,
        ):
            result = asyncio.run(
                process_router.symlink_manifest_backup_async(request, api_state=state)
            )
        self.assertEqual(result["job_id"], "job")
        self.assertEqual(
            writer.call_args.kwargs,
            {"backup_destination_id": "nas", "process_name": "Manual"},
        )
        self.assertEqual(
            state.update_symlink_job.call_args.args[1]["result"]["backup_manifest"],
            reference,
        )

    def test_async_managed_restore_carries_explicit_old_profile_and_process(self):
        request = types.SimpleNamespace(
            process_name="Manual",
            backup_destination_id="old",
            manifest_path="dumb-backup://old/symlink-manual/" + "a" * 32,
            dry_run=True,
            overwrite_existing=False,
            restore_broken=True,
        )
        from unittest.mock import Mock

        state = types.SimpleNamespace(
            create_symlink_job=lambda **_kwargs: {"job_id": "job"},
            update_symlink_job=Mock(),
        )

        def inline_thread(*_args, **kwargs):
            return types.SimpleNamespace(start=kwargs["target"])

        with (
            patch.object(process_router.threading, "Thread", side_effect=inline_thread),
            patch(
                "utils.symlink_repair.restore_symlink_manifest",
                return_value={"restored": 0},
            ) as restore,
        ):
            result = asyncio.run(
                process_router.symlink_manifest_restore_async(request, api_state=state)
            )
        self.assertEqual(result["job_id"], "job")
        self.assertEqual(
            restore.call_args.kwargs,
            {"process_name": "Manual", "backup_destination_id": "old"},
        )
        self.assertEqual(
            state.update_symlink_job.call_args.args[1]["status"], "completed"
        )


if __name__ == "__main__":
    unittest.main()
