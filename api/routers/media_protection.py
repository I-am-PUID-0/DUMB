"""Media-server protection controls for dependent storage maintenance."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from utils.dependencies import (
    get_logger,
    get_media_protection_manager,
    get_optional_current_user,
)
from utils.media_protection import (
    MEDIA_KEYS,
    plex_library_settings,
    public_policy,
    save_global_settings,
    save_policy,
    update_plex_library_settings,
)

media_protection_router = APIRouter(prefix="/media-protection")


class PreflightRequest(BaseModel):
    process_name: str = Field(min_length=1, max_length=200)
    action: Literal["stop", "restart", "update", "scheduled_update"]
    model_config = ConfigDict(extra="forbid")


class PolicyUpdateRequest(BaseModel):
    process_name: str = Field(min_length=1, max_length=200)
    enabled: bool | None = None
    api_key: str | None = Field(default=None, max_length=1000)
    clear_api_key: bool = False
    stop_when_idle_on_outage: bool | None = None
    protected_mounts: list[str] | None = Field(default=None, max_length=32)
    model_config = ConfigDict(extra="forbid")


class GlobalSettingsUpdateRequest(BaseModel):
    enabled: bool | None = None
    recovery_stabilization_seconds: int | None = Field(default=None, ge=5, le=600)
    recovery_timeout_seconds: int | None = Field(default=None, ge=30, le=3600)
    monitor_interval_seconds: int | None = Field(default=None, ge=2, le=60)
    model_config = ConfigDict(extra="forbid")


class PlexSettingsUpdateRequest(BaseModel):
    autoEmptyTrash: bool | None = None
    fSEventLibraryUpdatesEnabled: bool | None = None
    fSEventLibraryPartialScanEnabled: bool | None = None
    scheduledLibraryUpdatesEnabled: bool | None = None
    scheduledLibraryUpdateInterval: int | None = Field(default=None, ge=0, le=23)
    model_config = ConfigDict(extra="forbid")


def _media_process(process_name: str) -> tuple[str, str]:
    from utils.config_loader import CONFIG_MANAGER

    key, _ = CONFIG_MANAGER.find_key_for_process(process_name)
    if key not in MEDIA_KEYS:
        raise HTTPException(status_code=404, detail="Media server not found")
    return key, process_name


@media_protection_router.get("/status")
async def get_status(
    process_name: str | None = Query(default=None, max_length=200),
    manager=Depends(get_media_protection_manager),
    current_user: str = Depends(get_optional_current_user),
):
    return await run_in_threadpool(manager.status, process_name)


@media_protection_router.post("/preflight")
async def preflight(
    request: PreflightRequest,
    manager=Depends(get_media_protection_manager),
    current_user: str = Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        manager.preflight, request.process_name, request.action
    )


@media_protection_router.get("/policy")
async def get_policy(
    process_name: str = Query(min_length=1, max_length=200),
    current_user: str = Depends(get_optional_current_user),
):
    _media_process(process_name)
    return public_policy(process_name)


@media_protection_router.put("/policy")
async def update_policy(
    request: PolicyUpdateRequest,
    current_user: str = Depends(get_optional_current_user),
):
    key, process_name = _media_process(request.process_name)
    updates = request.model_dump(
        exclude={"process_name", "clear_api_key"}, exclude_unset=True
    )
    if request.clear_api_key:
        updates["api_key"] = ""
    elif request.api_key is None:
        updates.pop("api_key", None)
    policy = await run_in_threadpool(save_policy, process_name, updates)
    policy["service_key"] = key
    return policy


@media_protection_router.put("/settings")
async def update_global_settings(
    request: GlobalSettingsUpdateRequest,
    current_user: str = Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        save_global_settings, request.model_dump(exclude_unset=True)
    )


def _plex_payload(settings: dict) -> dict:
    return {
        "settings": settings,
        "guidance": {
            "autoEmptyTrash": {
                "recommended": False,
                "risk": "high",
                "reason": "Keeps unavailable items in Plex trash so a temporary mount outage is reversible.",
            },
            "fSEventLibraryUpdatesEnabled": {
                "recommended": False,
                "risk": "medium",
                "reason": "Remote and virtual mounts can emit misleading filesystem events during reconnects.",
            },
            "fSEventLibraryPartialScanEnabled": {
                "recommended": True,
                "risk": "low",
                "reason": "If automatic change detection is enabled, partial scans limit the affected scope.",
            },
            "scheduledLibraryUpdatesEnabled": {
                "recommended": False,
                "risk": "medium",
                "reason": "Scheduled scans can overlap an unattended dependency outage.",
            },
            "scheduledLibraryUpdateInterval": {
                "recommended": 12,
                "risk": "low",
                "reason": "Relevant only when scheduled library updates remain enabled.",
            },
        },
    }


@media_protection_router.get("/plex-library-settings")
async def get_plex_settings(
    logger=Depends(get_logger),
    current_user: str = Depends(get_optional_current_user),
):
    try:
        settings = await run_in_threadpool(plex_library_settings, logger)
        return _plex_payload(settings)
    except Exception as error:
        logger.error("Unable to read Plex library settings: %s", error)
        raise HTTPException(
            status_code=503,
            detail="Unable to read Plex library settings. Verify Plex is running and its token is configured.",
        ) from None


@media_protection_router.put("/plex-library-settings")
async def put_plex_settings(
    request: PlexSettingsUpdateRequest,
    logger=Depends(get_logger),
    current_user: str = Depends(get_optional_current_user),
):
    updates = request.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No Plex settings supplied")
    try:
        settings = await run_in_threadpool(
            update_plex_library_settings, updates, logger
        )
        return _plex_payload(settings)
    except Exception as error:
        logger.error("Unable to update Plex library settings: %s", error)
        raise HTTPException(
            status_code=503,
            detail="Unable to update Plex library settings. Verify Plex is running and its token is configured.",
        ) from None
