"""Fresh-process real FastAPI/ASGI integration with isolated application boundaries."""

import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from fastapi import FastAPI, HTTPException

ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = json.loads((ROOT / "utils/dumb_config.json").read_text())


class Manager:
    def __init__(self):
        self.config, self.snapshots = copy.deepcopy(DEFAULTS), []
        self.default_config_path = str(ROOT / "utils/dumb_config.json")

    def get(self, key, default=None):
        return self.config.get(key, default)

    def save_config(self):
        self.snapshots.append(copy.deepcopy(self.config))

    def reload(self):
        pass

    def find_key_for_process(self, process):
        for family, block in self.config.items():
            if not isinstance(block, dict):
                continue
            if block.get("process_name") == process:
                return family, None
            for instance, cfg in (block.get("instances") or {}).items():
                if isinstance(cfg, dict) and cfg.get("process_name") == process:
                    return family, instance
        return None, None


MANAGER = Manager()
LOGGER = mock.MagicMock()


def _stub(name, **members):
    module = types.ModuleType(name)
    for key, value in members.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _nothing(*args, **kwargs):
    return None


def _yes(*args, **kwargs):
    return True, None


# These are external application, diagnostics and runtime singleton boundaries.
# FastAPI, Pydantic, plan/state, runtime dispatch and onboarding orchestration are real.
_stub("utils.config_loader", CONFIG_MANAGER=MANAGER, find_service_config=_nothing)
_stub("utils.global_logger", logger=LOGGER)
_stub(
    "utils.dependencies",
    **{
        name: _nothing
        for name in (
            "get_process_handler",
            "get_logger",
            "get_api_state",
            "get_updater",
            "get_optional_current_user",
            "get_media_protection_manager",
        )
    },
)
_stub(
    "utils.ai_diagnostics",
    record_config_change=_nothing,
    record_diagnostic_event=_nothing,
)
_stub(
    "utils.setup",
    COMMIT_PIN_SERVICE_KEYS=set(),
    ensure_managed_postgres_database=_nothing,
    read_nzbdav_install_info=_nothing,
    setup_project=_nothing,
    _prepare_whisparr_profile_config=_nothing,
)
_stub("utils.versions", Versions=lambda: types.SimpleNamespace())
_stub("utils.install_cache", INSTALL_CACHE=types.SimpleNamespace())
_stub(
    "utils.infinidysk_migration",
    CLEANUP_CONFIRMATION="fixture",
    INFINIDYSK_MIGRATION_MANAGER=types.SimpleNamespace(),
    InfiniDyskMigrationError=RuntimeError,
)
_stub(
    "utils.service_reset",
    ServiceResetError=RuntimeError,
    build_service_reset_preview=_nothing,
    execute_service_reset=_nothing,
)
_stub("utils.decypharr_settings", patch_decypharr_config=_yes)
_stub("utils.nzbdav_settings", patch_nzbdav_config=_yes)
_stub("utils.altmount_settings", patch_altmount_arr_integration=_yes)
_stub(
    "utils.neutarr_settings",
    any_arr_uses_neutarr=lambda: False,
    patch_neutarr_config=_yes,
)
_stub(
    "utils.profilarr_settings",
    any_arr_uses_profilarr=lambda: False,
    patch_profilarr_config=_yes,
)
_stub("utils.prowlarr_settings", patch_prowlarr_apps=_yes)


class Health:
    def __init__(self, **kwargs):
        pass

    def check(self, *args):
        return {"status": "healthy"}


_stub("utils.service_health", ServiceHealthMonitor=Health)

from api.routers import process as process_router
from api.routers import setup as setup_router
from utils.arrangement_runtime import ArrangementRuntime
from utils.arrangement_state import SetupCoordinator


async def _request(app, method, path, payload=None, *, body_chunks=None):
    body = b"" if payload is None else json.dumps(payload).encode()
    chunks = [body] if body_chunks is None else list(body_chunks)
    index = 0
    messages = []

    async def receive():
        nonlocal index
        if index < len(chunks):
            chunk = chunks[index]
            index += 1
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(chunks),
            }
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
    }
    await app(scope, receive, send)
    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    raw = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, json.loads(raw) if raw else None


class SetupHttpContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        MANAGER.config, MANAGER.snapshots = copy.deepcopy(DEFAULTS), []
        self.started, self.running, self.checkpoints = [], set(), []
        self.patches = []

        # Point every native configuration path at this disposable fixture.
        def relocate(block):
            if isinstance(block, dict):
                for key, value in block.items():
                    if (
                        key in {"config_dir", "config_file", "install_dir", "log_file"}
                        and isinstance(value, str)
                        and value.startswith("/")
                    ):
                        block[key] = str(
                            Path(self.temp.name) / "apps" / value.lstrip("/")
                        )
                    else:
                        relocate(value)
            elif isinstance(block, list):
                for value in block:
                    relocate(value)

        relocate(MANAGER.config)
        self.defaults_path = Path(self.temp.name) / "defaults.json"
        self.defaults_path.write_text(json.dumps(MANAGER.config))
        MANAGER.default_config_path = str(self.defaults_path)
        state = types.SimpleNamespace(
            get_status=lambda name: "running" if name in self.running else "stopped"
        )

        def start(name, **kwargs):
            self.checkpoints.append((name, copy.deepcopy(MANAGER.config)))
            self.started.append(name)
            self.running.add(name)
            return object(), None

        updater = types.SimpleNamespace(auto_update=start)
        self.runtime = ArrangementRuntime(
            MANAGER,
            updater,
            state,
            LOGGER,
            ownership_directory=str(Path(self.temp.name) / "connections"),
        )
        for name, value in (
            ("infinidysk_namespace_migration_active", lambda: False),
            ("infinidysk_postgres_migration_active", lambda: False),
            ("build_conditional_dependency_map", lambda *args, **kwargs: {}),
            ("filter_conditional_deps_for_instance", lambda *args, **kwargs: {}),
            ("wait_for_process_running", lambda api, name: name in self.running),
            ("_seed_used_ports", _nothing),
            ("_reserve_config_port", _nothing),
            ("_reserve_port", lambda **kwargs: kwargs.get("desired")),
            ("ensure_arr_postgres_dependency_running", _nothing),
            ("_is_port_available", lambda *args, **kwargs: True),
        ):
            patcher = mock.patch.object(process_router, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Target API readiness is deliberately absent in this no-network fixture.
        patcher = mock.patch("utils.arrangement_runtime._arr_api", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.coordinator = SetupCoordinator(
            str(Path(self.temp.name) / "state"),
            configuration=lambda: MANAGER.config,
            defaults=self.runtime.defaults,
            start_services=self.runtime.start,
            inspect_action=self.runtime.inspect,
            apply_action=self.runtime.apply,
            discover_fields=lambda *args: [],
            autostart=False,
        )
        self.addCleanup(self.coordinator.shutdown)
        self.app = FastAPI()
        self.app.include_router(setup_router.setup_router, prefix="/process/setup")
        self.app.dependency_overrides[setup_router.get_setup_coordinator] = (
            lambda: self.coordinator
        )
        self.allowed = True

        async def authorize():
            if not self.allowed:
                raise HTTPException(401, "Authentication required")
            return "fixture-operator"

        self.app.dependency_overrides[setup_router.get_optional_current_user] = (
            authorize
        )

    def request(self, method, suffix, payload=None):
        return asyncio.run(
            _request(self.app, method, "/process/setup" + suffix, payload)
        )

    def payload(self, provider_first=True):
        arr = {
            "name": "radarr",
            "instance_name": "Fresh Movies",
            "service_options": {
                "radarr": {
                    "core_service": "infinidysk",
                    "use_neutarr": False,
                    "use_profilarr": False,
                }
            },
        }
        provider = {"name": "infinidysk"}
        return {
            "core_services": [provider, arr] if provider_first else [arr, provider],
            "optional_services": [],
        }

    def create_run(self, payload=None, identifier="fixture-request-stable"):
        status, plan = self.request("POST", "/plan", payload or self.payload())
        self.assertEqual(status, 200, plan)
        status, run = self.request(
            "POST", "/runs", {"plan_id": plan["plan_id"], "idempotency_key": identifier}
        )
        self.assertEqual(status, 200, run)
        return plan, run

    def test_provider_first_and_arr_first_materialize_full_arr_set_before_first_start(
        self,
    ):
        for provider_first in (True, False):
            with self.subTest(provider_first=provider_first):
                # A brand new manager configuration has no fresh Arr instance.
                MANAGER.config = json.loads(self.defaults_path.read_text())
                self.running.clear()
                self.started.clear()
                self.checkpoints.clear()
                plan, run = self.create_run(
                    self.payload(provider_first), f"order-request-{provider_first}"
                )
                self.coordinator.run_pending(run["run_id"])
                status, result = self.request("GET", f"/runs/{run['run_id']}")
                self.assertEqual(status, 200, result)
                self.assertTrue(self.checkpoints, result)
                first_config = self.checkpoints[0][1]
                fresh = first_config["radarr"]["instances"].get("Fresh Movies")
                self.assertIsNotNone(fresh, result)
                self.assertTrue(fresh["enabled"])
                self.assertEqual(fresh["core_service"], "infinidysk")
                self.assertIn("Radarr Fresh Movies", self.started)
                self.assertNotEqual(result["status"], "complete")
                provider_action = next(
                    action
                    for action in result["actions"]
                    if action["action"] == "provider.infinidysk"
                )
                self.assertEqual(provider_action["execution"], "waiting_dependency")

    def test_http_validation_and_authorization_never_echo_credential_inputs(self):
        self.allowed = False
        status, denied = self.request("POST", "/plan", self.payload())
        self.assertEqual(status, 401)
        self.assertFalse(MANAGER.snapshots)
        self.allowed = True
        malformed = self.payload()
        malformed["core_services"][0]["unexpected_secret"] = "fixture-private-secret"
        status, rejected = self.request("POST", "/plan", malformed)
        self.assertEqual(status, 422, rejected)
        self.assertNotIn("fixture-private-secret", json.dumps(rejected))
        for field in ("values", "intent"):
            for malformed_value in (
                ["fixture-private-secret"],
                "fixture-private-secret",
                None,
                False,
            ):
                with self.subTest(
                    field=field, body_type=type(malformed_value).__name__
                ):
                    malformed = self.payload()
                    malformed["arrangement"] = {field: malformed_value}
                    status, rejected = self.request("POST", "/plan", malformed)
                    self.assertEqual(status, 400, rejected)
                    self.assertNotIn("fixture-private-secret", json.dumps(rejected))
        self.assertFalse(MANAGER.snapshots)
        self.assertFalse(self.started)
        status, result = self.request("GET", "/runs/" + "f" * 32)
        self.assertEqual(status, 404)

    def test_malformed_action_operation_and_shape_do_not_echo_private_values(self):
        _, run = self.create_run()
        path = f"/runs/{run['run_id']}/actions/{run['actions'][0]['id']}"
        marker = "fixture-password-must-never-appear"
        for body in (
            {"operation": marker, "values": {"audiobookshelf::api_key": marker}},
            {"operation": "apply", "values": [marker]},
            [marker],
        ):
            with self.subTest(body_type=type(body).__name__):
                status, rejected = self.request("POST", path, body)
                self.assertEqual(status, 422, rejected)
                self.assertNotIn(marker, json.dumps(rejected))
        status, rejected = asyncio.run(
            _request(
                self.app,
                "POST",
                "/process/setup" + path,
                body_chunks=[
                    b'{"operation":"fixture-password-must-never-appear", broken'
                ],
            )
        )
        self.assertEqual(status, 422, rejected)
        self.assertNotIn(marker, json.dumps(rejected))
        self.assertFalse(self.started)

    def test_nested_validation_keys_never_echo_supplied_private_identifiers(self):
        marker = "fixture-private-credential-in-dynamic-key"
        payload = self.payload()
        payload["optional_service_options"] = {marker: []}
        status, rejected = self.request("POST", "/plan", payload)
        self.assertEqual(status, 422, rejected)
        self.assertNotIn(marker, json.dumps(rejected))
        self.assertEqual(
            rejected["detail"],
            "Check the setup request shape and advertised fields. Supplied values are omitted.",
        )
        self.assertFalse(MANAGER.snapshots)
        self.assertFalse(self.started)
        self.assertFalse(list((Path(self.temp.name) / "state").glob("plan-*.json")))

    def test_streamed_request_size_limit_rejects_without_buffering_or_mutation(self):
        marker = b"fixture-large-private-password"
        chunks = [marker + b"x" * (100000 - len(marker)), b"x" * 100000, b"x" * 62145]
        status, rejected = asyncio.run(
            _request(
                self.app,
                "POST",
                "/process/setup/plan",
                body_chunks=chunks,
            )
        )
        self.assertEqual(status, 413, rejected)
        self.assertNotIn(marker.decode(), json.dumps(rejected))
        self.assertFalse(MANAGER.snapshots)
        self.assertFalse(self.started)
        self.assertFalse(list((Path(self.temp.name) / "state").glob("plan-*.json")))
        # Exactly the allowed size reaches ordinary JSON validation rather than
        # an off-by-one size rejection. No content-length header is required.
        status, rejected = asyncio.run(
            _request(
                self.app,
                "POST",
                "/process/setup/plan",
                body_chunks=[b"x" * 131072] * 2,
            )
        )
        self.assertEqual(status, 422, rejected)

    def test_http_idempotency_recheck_partial_tasks_and_manual_confirmation(self):
        plan, run = self.create_run()
        status, duplicate = self.request(
            "POST",
            "/runs",
            {"plan_id": plan["plan_id"], "idempotency_key": "fixture-request-stable"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(duplicate["run_id"], run["run_id"])
        self.coordinator.run_pending(run["run_id"])
        before = list(self.started)
        status, checking = self.request("POST", f"/runs/{run['run_id']}/recheck")
        self.assertEqual(status, 200, checking)
        self.coordinator.run_pending(run["run_id"])
        self.assertEqual(self.started, before)
        _, result = self.request("GET", "/runs/latest")
        task = next(
            action
            for action in result["actions"]
            if action["automation"] == "manual" and action["required"]
        )
        status, confirmed = self.request(
            "POST",
            f"/runs/{run['run_id']}/actions/{task['id']}",
            {"operation": "confirm"},
        )
        self.assertEqual(status, 200, confirmed)
        saved = next(
            action for action in confirmed["actions"] if action["id"] == task["id"]
        )
        self.assertEqual(saved["verification"], "unavailable")
        self.assertEqual(saved["evidence"]["level"], "user_attested")
        self.assertEqual(
            confirmed["coverage"]["denominator"], result["coverage"]["denominator"]
        )

    def test_migration_denial_has_no_materialization_or_config_writes(self):
        before = copy.deepcopy(MANAGER.config)
        with mock.patch.object(
            process_router, "infinidysk_namespace_migration_active", return_value=True
        ):
            with self.assertRaises(HTTPException) as raised:
                process_router._run_startup(
                    process_router.UnifiedStartRequest.model_validate(self.payload()),
                    self.runtime.updater,
                    self.runtime.api_state,
                    LOGGER,
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(MANAGER.config, before)
        self.assertFalse(MANAGER.snapshots)
        self.assertFalse(self.started)

    def test_real_setup_guard_blocks_other_service_database_migration_before_writes(
        self,
    ):
        import uuid
        from utils.arr_postgres_migration import ArrPostgresMigrationManager

        postgres = ArrPostgresMigrationManager(Path(self.temp.name) / "postgres-jobs")
        postgres._create_job(
            uuid.UUID(hex="a" * 32),
            {
                "job_id": "a" * 32,
                "service_key": "sonarr",
                "process_name": "Fixture Sonarr",
                "status": "running",
                "worker_id": postgres._worker_id,
                "rollback_available": False,
            },
        )
        self.coordinator.mutation_guard = setup_router._mutation_guard
        before = copy.deepcopy(MANAGER.config)
        with mock.patch(
            "utils.arr_postgres_migration.POSTGRES_MIGRATION_MANAGER", postgres
        ):
            _, run = self.create_run()
            self.coordinator.run_pending(run["run_id"])
        saved = self.coordinator.get_run(run["run_id"])
        self.assertEqual(saved["status"], "interrupted", saved)
        self.assertFalse(self.started)
        self.assertFalse(MANAGER.snapshots)
        self.assertEqual(MANAGER.config, before)
        self.assertTrue(postgres.has_setup_conflicting_job())

    def test_riven_source_keys_materialize_directly_without_mount_objects(self):
        request = process_router.UnifiedStartRequest.model_validate(
            {
                "core_services": [
                    {
                        "name": "riven_backend",
                        "debrid_service": ["realdebrid"],
                        "debrid_key": ["fixture-provider-key"],
                        "service_options": {
                            "riven_backend": {"branch_enabled": True, "branch": "dev"}
                        },
                    }
                ],
                "optional_services": [],
            }
        )
        before_mounts = copy.deepcopy(MANAGER.config["rclone"])
        before_zurg = copy.deepcopy(MANAGER.config["zurg"])
        process_router._materialize_onboarding_resources(request, LOGGER)
        self.assertEqual(
            MANAGER.config["riven_backend"]["api_keys"]["realdebrid"],
            "fixture-provider-key",
        )
        self.assertEqual(MANAGER.config["rclone"], before_mounts)
        self.assertEqual(MANAGER.config["zurg"], before_zurg)
        self.assertTrue(MANAGER.config["riven_backend"]["enabled"])

    def test_cancel_pending_http_run_does_not_start_any_process(self):
        _, run = self.create_run()
        status, cancelled = self.request("POST", f"/runs/{run['run_id']}/cancel")
        self.assertEqual(status, 200, cancelled)
        self.coordinator.run_pending(run["run_id"])
        self.assertFalse(self.started)
        _, saved = self.request("GET", f"/runs/{run['run_id']}")
        self.assertEqual(saved["status"], "interrupted")

    def test_real_http_workflow_preview_authorization_and_structured_secret_contract(
        self,
    ):
        from tests.test_arrangement_workflow import NativeFixture

        _, run = self.create_run()
        source = "radarr.Fresh Movies"
        action_id = source + "::workflow_test"
        self.coordinator.run_pending(run["run_id"])
        self.started.clear()
        self.coordinator._save_action(
            run["run_id"],
            source + "::runtime",
            {"execution": "already_present", "verification": "passed"},
        )
        self.coordinator._save_action(
            run["run_id"],
            source + "::provider.infinidysk",
            {"execution": "already_present", "verification": "passed"},
        )
        native = NativeFixture()
        values = {
            source
            + "::workflow_probe": {
                "provider": "infinidysk",
                "media_server": "jellyfin",
                "item_id": 7,
                "source_url": "https://example.invalid/test.nzb?apikey=fixture-private-secret",
            }
        }
        path = f"/runs/{run['run_id']}/actions/{action_id}"
        with (
            mock.patch(
                "utils.arrangement_workflow.NativeWorkflow", return_value=native
            ),
            mock.patch(
                "utils.arrangement_workflow._nzb", return_value=(b"manifest", 400)
            ),
        ):
            status, queued = self.request(
                "POST", path, {"operation": "preview", "values": values}
            )
            self.assertEqual(status, 200, queued)
            self.coordinator.run_pending(run["run_id"])
            _, previewed = self.request("GET", f"/runs/{run['run_id']}")
            self.assertNotIn("fixture-private-secret", json.dumps(previewed))
            preview = next(a for a in previewed["actions"] if a["id"] == action_id)
            self.assertEqual(preview["evidence"]["preview_status"], "ready", preview)
            self.assertEqual(native.enqueues, 0)
            values[source + "::workflow_probe"]["authorize_download_import_scan"] = True
            values[source + "::workflow_probe"]["authorize_reserved_test_item"] = True
            values[source + "::workflow_preview_id"] = preview["evidence"]["preview_id"]
            status, queued = self.request(
                "POST", path, {"operation": "apply", "values": values}
            )
            self.assertEqual(status, 200, queued)
            self.coordinator.run_pending(run["run_id"])
            _, completed = self.request("GET", f"/runs/{run['run_id']}")
            result = next(a for a in completed["actions"] if a["id"] == action_id)
            self.assertEqual(result["verification"], "passed", result)
            self.assertEqual(native.enqueues, 1)
            self.assertNotIn("fixture-private-secret", json.dumps(completed))
        self.assertFalse(self.started)


if __name__ == "__main__":
    unittest.main()
