from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from utils.dependencies import get_optional_current_user, get_rclone_optimizer_manager
from utils.rclone_optimizer import RcloneOptimizerError, RcloneOptimizerManager

rclone_optimizer_router = APIRouter(prefix="/rclone-optimizer")


class OptimizerLimits(BaseModel):
    max_vfs_cache_gib: int = Field(default=5, ge=1, le=2048)
    min_free_disk_gib: int = Field(default=10, ge=1, le=65536)
    max_memory_mib: int = Field(default=2048, ge=256, le=262144)
    max_test_download_gib: float = Field(default=4, ge=0.25, le=100)
    max_duration_minutes: int = Field(default=20, ge=2, le=180)
    concurrent_streams: int = Field(default=1, ge=1, le=3)
    startup_buffer_mib: int = Field(default=32, ge=1, le=256)
    bandwidth_limit_mbps: int = Field(default=0, ge=0, le=100000)
    model_config = ConfigDict(extra="forbid")


class OptimizerStartRequest(BaseModel):
    process_name: str = Field(min_length=1, max_length=200)
    selected_paths: list[str] = Field(min_length=1, max_length=8)
    depth: Literal["quick", "standard", "thorough"] = "standard"
    limits: OptimizerLimits = Field(default_factory=OptimizerLimits)
    model_config = ConfigDict(extra="forbid")


class OptimizerJobRequest(BaseModel):
    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    model_config = ConfigDict(extra="forbid")


def _bad_request(error: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))


@rclone_optimizer_router.get("/instances")
async def list_instances(
    manager: RcloneOptimizerManager = Depends(get_rclone_optimizer_manager),
    current_user: str = Depends(get_optional_current_user),
):
    return {"instances": await run_in_threadpool(manager.list_instances)}


@rclone_optimizer_router.get("/content")
async def discover_content(
    process_name: str = Query(min_length=1, max_length=200),
    manager: RcloneOptimizerManager = Depends(get_rclone_optimizer_manager),
    current_user: str = Depends(get_optional_current_user),
):
    try:
        return await run_in_threadpool(manager.discover_content, process_name)
    except RcloneOptimizerError as error:
        raise _bad_request(error) from None


@rclone_optimizer_router.get("/jobs")
async def list_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    manager: RcloneOptimizerManager = Depends(get_rclone_optimizer_manager),
    current_user: str = Depends(get_optional_current_user),
):
    return {"jobs": await run_in_threadpool(manager.recent_jobs, limit)}


@rclone_optimizer_router.post("/jobs", status_code=202)
async def start_job(
    request: OptimizerStartRequest,
    manager: RcloneOptimizerManager = Depends(get_rclone_optimizer_manager),
    current_user: str = Depends(get_optional_current_user),
):
    try:
        return {
            "job": await run_in_threadpool(
                manager.create_job,
                request.process_name,
                request.selected_paths,
                request.depth,
                request.limits.model_dump(),
            )
        }
    except RcloneOptimizerError as error:
        raise _bad_request(error) from None


@rclone_optimizer_router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    manager: RcloneOptimizerManager = Depends(get_rclone_optimizer_manager),
    current_user: str = Depends(get_optional_current_user),
):
    try:
        return {"job": await run_in_threadpool(manager.get_job, job_id)}
    except RcloneOptimizerError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None


@rclone_optimizer_router.get("/latest")
async def latest_job(
    process_name: str | None = Query(default=None, max_length=200),
    active_only: bool = False,
    manager: RcloneOptimizerManager = Depends(get_rclone_optimizer_manager),
    current_user: str = Depends(get_optional_current_user),
):
    return {
        "job": await run_in_threadpool(manager.latest_job, process_name, active_only)
    }


@rclone_optimizer_router.post("/cancel")
async def cancel_job(
    request: OptimizerJobRequest,
    manager: RcloneOptimizerManager = Depends(get_rclone_optimizer_manager),
    current_user: str = Depends(get_optional_current_user),
):
    try:
        return {"job": await run_in_threadpool(manager.cancel_job, request.job_id)}
    except RcloneOptimizerError as error:
        raise _bad_request(error) from None


@rclone_optimizer_router.post("/apply")
async def apply_recommendation(
    request: OptimizerJobRequest,
    manager: RcloneOptimizerManager = Depends(get_rclone_optimizer_manager),
    current_user: str = Depends(get_optional_current_user),
):
    try:
        return {"job": await run_in_threadpool(manager.apply, request.job_id)}
    except RcloneOptimizerError as error:
        raise _bad_request(error) from None


@rclone_optimizer_router.post("/rollback")
async def rollback_recommendation(
    request: OptimizerJobRequest,
    manager: RcloneOptimizerManager = Depends(get_rclone_optimizer_manager),
    current_user: str = Depends(get_optional_current_user),
):
    try:
        return {"job": await run_in_threadpool(manager.rollback, request.job_id)}
    except RcloneOptimizerError as error:
        raise _bad_request(error) from None
