"""Versioned, independent major-upgrade and native PostgreSQL workflows."""

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from utils.config_loader import CONFIG_MANAGER
from utils.dependencies import (
    get_optional_current_user,
    get_process_handler,
    get_updater,
)
from utils.infinidysk_migration_admission import (
    InfiniDyskMigrationAdmissionError,
    admit_v2_job,
)
from utils.infinidysk_v2_migration import (
    INFINIDYSK_V2_MIGRATION_MANAGER,
    InfiniDyskV2MigrationError,
)
from utils.infinidysk_v2_postgres import INFINIDYSK_V2_POSTGRES_MANAGER
from utils.infinidysk_v2_support import API_MINIMUM, api_version_supported
from utils.project_metadata import get_project_version
from utils.runtime_paths import pyproject_file

infinidysk_v2_router = APIRouter(prefix="/infinidysk-v2")


def _manager(kind, handler, updater):
    if kind == "major":
        from utils.infinidysk_v2_execution import InfiniDyskV2Executor

        return INFINIDYSK_V2_MIGRATION_MANAGER.configure(
            process_handler=handler,
            updater=updater,
            config_manager=CONFIG_MANAGER,
            executor=InfiniDyskV2Executor(handler, CONFIG_MANAGER),
        )
    return INFINIDYSK_V2_POSTGRES_MANAGER.configure(
        process_handler=handler, config_manager=CONFIG_MANAGER
    )


def _invoke(kind, operation, payload, handler, updater, *, mutation=False):
    version = get_project_version(pyproject_file())
    if mutation and not api_version_supported(version):
        raise HTTPException(
            status_code=426, detail=f"DUMB API {API_MINIMUM} or newer is required."
        )
    manager = _manager(kind, handler, updater)
    try:
        callback = getattr(manager, operation)
        if mutation and operation not in {"preview", "preview_start", "preview_cancel"}:
            existing = operation in {
                "rollback",
                "activate",
                "cutover",
                "cancel",
                "retry_consumers",
                "retry_stage",
                "finish_consumer_recovery",
            }
            with admit_v2_job(kind, existing=existing):
                return callback(payload)
        return callback(payload) if payload is not None else callback()
    except InfiniDyskMigrationAdmissionError:
        raise HTTPException(
            status_code=409,
            detail="Another lifecycle or migration operation owns admission.",
        ) from None
    except (InfiniDyskV2MigrationError, ValueError, OSError) as error:
        # Manager errors are already sanitized. Unexpected driver/native errors
        # must never send connection strings or credentials to API clients.
        message = (
            str(error)
            if isinstance(error, (ValueError, InfiniDyskV2MigrationError))
            else "Migration evidence or runtime state is unavailable."
        )
        raise HTTPException(status_code=409, detail=message) from None


def _private_preview_response(response):
    if response is not None:
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"


@infinidysk_v2_router.get("/status")
async def major_status(
    response: Response = None,
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    _private_preview_response(response)
    return await run_in_threadpool(_invoke, "major", "status", None, handler, updater)


@infinidysk_v2_router.post("/preview/start")
async def major_preview_start(
    response: Response = None,
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    _private_preview_response(response)
    return await run_in_threadpool(
        _invoke, "major", "preview_start", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.get("/preview/task")
async def major_preview_latest(
    response: Response = None,
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    _private_preview_response(response)
    return await run_in_threadpool(
        _invoke, "major", "preview_task", None, handler, updater
    )


@infinidysk_v2_router.get("/preview/task/{task_id}")
async def major_preview_task(
    task_id: str,
    response: Response = None,
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    _private_preview_response(response)
    return await run_in_threadpool(
        _invoke, "major", "preview_task", {"task_id": task_id}, handler, updater
    )


@infinidysk_v2_router.post("/preview/task/{task_id}/cancel")
async def major_preview_cancel(
    task_id: str,
    response: Response = None,
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    _private_preview_response(response)
    return await run_in_threadpool(
        _invoke,
        "major",
        "preview_cancel",
        {"task_id": task_id},
        handler,
        updater,
        mutation=True,
    )


@infinidysk_v2_router.post("/preview")
async def major_preview(
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke, "major", "preview", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.post("/rehearse")
async def major_rehearse(
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke, "major", "rehearse", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.post("/prepare")
async def major_prepare(
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke, "major", "prepare", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.post("/activate")
async def major_activate(
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke, "major", "activate", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.post("/start")
async def major_start(
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke, "major", "start", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.get("/jobs/{job_id}")
async def major_job(
    job_id: str,
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke, "major", "get_job", job_id, handler, updater
    )


@infinidysk_v2_router.post("/rollback")
async def major_rollback(
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke, "major", "rollback", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.post("/cancel")
async def major_cancel(
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke, "major", "cancel", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.post("/retry-stage")
async def major_retry_stage(
    response: Response = None,
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    _private_preview_response(response)
    return await run_in_threadpool(
        _invoke, "major", "retry_stage", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.post("/retry-consumers")
async def major_retry_consumers(
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke, "major", "retry_consumers", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.post("/finish-consumer-recovery")
async def major_finish_consumer_recovery(
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke,
        "major",
        "finish_consumer_recovery",
        request,
        handler,
        updater,
        mutation=True,
    )


@infinidysk_v2_router.post("/cleanup/preview")
async def major_cleanup_preview(
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke, "major", "cleanup_preview", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.post("/cleanup")
async def major_cleanup(
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke, "major", "cleanup", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.get("/postgres/status")
async def postgres_status(
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke, "postgres", "status", None, handler, updater
    )


@infinidysk_v2_router.post("/postgres/prepare-target")
async def postgres_prepare_target(
    response: Response = None,
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    _private_preview_response(response)
    return await run_in_threadpool(
        _invoke, "postgres", "prepare_target", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.post("/postgres/preview")
async def postgres_preview(
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke, "postgres", "preview", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.post("/postgres/start")
async def postgres_start(
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke, "postgres", "start", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.post("/postgres/cutover")
async def postgres_cutover(
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke, "postgres", "cutover", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.post("/postgres/cancel")
async def postgres_cancel(
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke, "postgres", "cancel", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.post("/postgres/rollback")
async def postgres_rollback(
    request: dict[str, Any] = Body(...),
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        _invoke, "postgres", "rollback", request, handler, updater, mutation=True
    )


@infinidysk_v2_router.get("/jobs/{job_id}/report", response_class=PlainTextResponse)
async def major_report(
    job_id: str,
    handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    current_user=Depends(get_optional_current_user),
):
    raw = await run_in_threadpool(
        _invoke, "major", "get_report", job_id, handler, updater
    )
    return PlainTextResponse(
        raw, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    )
