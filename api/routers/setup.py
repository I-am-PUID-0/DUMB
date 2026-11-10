"""Additive setup-plan/run API. Legacy onboarding endpoints remain available."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from utils.arrangement_state import SetupConflict, SetupCoordinator
from utils.dependencies import get_optional_current_user


class PrivateSetupRoute(APIRoute):
    """Bound inputs and prevent framework validation from echoing credentials."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def private_handler(request):
            if request.method == "POST":
                chunks, length = [], 0
                async for chunk in request.stream():
                    length += len(chunk)
                    if length > 262144:
                        raise HTTPException(
                            413, detail="The setup request exceeds its size limit."
                        )
                    chunks.append(chunk)
                request._body = b"".join(chunks)
            try:
                return await original(request)
            except RequestValidationError:
                raise HTTPException(
                    422,
                    detail="Check the setup request shape and advertised fields. Supplied values are omitted.",
                ) from None

        return private_handler


setup_router = APIRouter(route_class=PrivateSetupRoute)
_coordinator = None
_coordinator_lock = threading.RLock()


@contextmanager
def _mutation_guard():
    from utils.arr_postgres_migration import POSTGRES_MIGRATION_MANAGER
    from utils.infinidysk_migration_admission import (
        INFINIDYSK_MIGRATION_ADMISSION_LOCK,
        InfiniDyskMigrationAdmissionError,
        infinidysk_v2_recovery_pending,
        infinidysk_namespace_recovery_pending,
        infinidysk_postgres_recovery_pending,
        release_infinidysk_external_mutation,
        reserve_infinidysk_external_mutation,
    )

    with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
        if (
            POSTGRES_MIGRATION_MANAGER.has_setup_conflicting_job()
            or infinidysk_namespace_recovery_pending()
            or infinidysk_postgres_recovery_pending()
            or infinidysk_v2_recovery_pending()
        ):
            raise SetupConflict(
                "A managed database/namespace migration or recovery needs attention before setup can change applications."
            )
        try:
            token = reserve_infinidysk_external_mutation("Managed arrangement setup")
        except InfiniDyskMigrationAdmissionError:
            raise SetupConflict(
                "A guarded native migration, provider change or v1 retirement owns managed applications."
            ) from None
    try:
        yield
    finally:
        release_infinidysk_external_mutation(token)


def get_setup_coordinator(_user=Depends(get_optional_current_user)):
    global _coordinator
    with _coordinator_lock:
        if _coordinator is None:
            from utils.arrangement_runtime import ArrangementRuntime
            from utils.config_loader import CONFIG_MANAGER
            from utils.dependencies import (
                get_api_state,
                get_logger,
                get_process_handler,
                get_updater,
            )

            runtime = ArrangementRuntime(
                CONFIG_MANAGER,
                get_updater(),
                get_api_state(),
                get_logger(),
                get_process_handler(),
            )
            _coordinator = SetupCoordinator(
                "/config/.dumb-setup",
                configuration=lambda: CONFIG_MANAGER.config,
                defaults=runtime.defaults,
                start_services=runtime.start,
                inspect_action=runtime.inspect,
                apply_action=runtime.apply,
                preflight=runtime.preflight,
                discover_fields=runtime.schema_fields,
                mutation_guard=_mutation_guard,
            )
            runtime.cancel_check = _coordinator._cancelled
        return _coordinator


def shutdown_setup_coordinator():
    with _coordinator_lock:
        if _coordinator is not None:
            _coordinator.shutdown()


class StartSetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(pattern="^[a-f0-9]{32}$")
    idempotency_key: str = Field(pattern="^[A-Za-z0-9_-]{8,128}$")


class ActionSetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["apply", "recheck", "preview", "defer", "confirm"] = "recheck"
    values: dict = Field(default_factory=dict)


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except FileNotFoundError:
        raise HTTPException(
            404, detail="The requested setup record was not found."
        ) from None
    except SetupConflict as error:
        raise HTTPException(409, detail=str(error)) from None
    except ValueError as error:
        # Validation messages are authored locally and contain no supplied values.
        raise HTTPException(400, detail=str(error)) from None
    except (PermissionError, OSError):
        raise HTTPException(
            503,
            detail="Private setup state is unavailable. Check controller storage permissions.",
        ) from None


@setup_router.post("/plan")
def create_setup_plan(
    payload: dict = Body(...),
    coordinator=Depends(get_setup_coordinator),
    _user=Depends(get_optional_current_user),
):
    from api.routers.process import UnifiedStartRequest

    if len(json.dumps(payload).encode()) > 262144:
        raise HTTPException(413, detail="The setup request exceeds its size limit.")
    raw = dict(payload)
    arrangement = raw.pop("arrangement", {})
    if not isinstance(arrangement, dict) or set(arrangement) - {"values", "intent"}:
        raise HTTPException(
            400,
            detail="Arrangement must contain advertised values and optional workflow intent.",
        )
    if any(
        key in arrangement and not isinstance(arrangement[key], dict)
        for key in ("values", "intent")
    ):
        raise HTTPException(
            400,
            detail="Arrangement values and workflow intent must be objects.",
        )
    try:
        validated = UnifiedStartRequest.model_validate(raw).model_dump()
    except ValidationError:
        raise HTTPException(
            422,
            detail="Check the setup request shape and advertised fields. Supplied values are omitted.",
        ) from None
    validated["arrangement"] = arrangement
    return _call(coordinator.plan, validated)


@setup_router.post("/runs")
def start_setup_run(
    request: StartSetupRequest,
    coordinator=Depends(get_setup_coordinator),
    _user=Depends(get_optional_current_user),
):
    return _call(coordinator.start, request.plan_id, request.idempotency_key)


@setup_router.get("/runs/latest")
def latest_setup_run(
    coordinator=Depends(get_setup_coordinator), _user=Depends(get_optional_current_user)
):
    result = _call(coordinator.latest)
    if result is None:
        raise HTTPException(404, detail="No setup run has been created.")
    return result


@setup_router.get("/runs/{run_id}")
def get_setup_run(
    run_id: str,
    coordinator=Depends(get_setup_coordinator),
    _user=Depends(get_optional_current_user),
):
    return _call(coordinator.get_run, run_id)


@setup_router.post("/runs/{run_id}/retry")
def retry_setup_run(
    run_id: str,
    coordinator=Depends(get_setup_coordinator),
    _user=Depends(get_optional_current_user),
):
    return _call(coordinator.operate, run_id, "retry")


@setup_router.post("/runs/{run_id}/recheck")
def recheck_setup_run(
    run_id: str,
    coordinator=Depends(get_setup_coordinator),
    _user=Depends(get_optional_current_user),
):
    return _call(coordinator.operate, run_id, "recheck")


@setup_router.post("/runs/{run_id}/cancel")
def cancel_setup_run(
    run_id: str,
    coordinator=Depends(get_setup_coordinator),
    _user=Depends(get_optional_current_user),
):
    return _call(coordinator.operate, run_id, "cancel")


@setup_router.post("/runs/{run_id}/abandon")
def abandon_setup_run(
    run_id: str,
    coordinator=Depends(get_setup_coordinator),
    _user=Depends(get_optional_current_user),
):
    return _call(coordinator.operate, run_id, "abandon")


@setup_router.post("/runs/{run_id}/actions/{action_id}")
def operate_setup_action(
    run_id: str,
    action_id: str,
    request: ActionSetupRequest,
    coordinator=Depends(get_setup_coordinator),
    _user=Depends(get_optional_current_user),
):
    return _call(
        coordinator.operate,
        run_id,
        request.operation,
        action_id=action_id,
        values=request.values,
    )
