"""Installation backup and restore previews, jobs and opt-in scheduling."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from utils.config_loader import CONFIG_MANAGER
from utils.dependencies import get_optional_current_user, get_process_handler
from utils.backup_storage import BackupStorageError
from utils.installation_backup import (
    InstallationBackupError,
    get_installation_backup_manager,
)
from utils.installation_backup_archive import InstallationArchiveError


class PrivateInstallationRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def guarded(request):
            if request.method in {"POST", "PUT"}:
                raw = bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        return JSONResponse(
                            status_code=413,
                            content={
                                "detail": "The installation request exceeds its bound."
                            },
                        )
                request._body = bytes(raw)
            try:
                return await original(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": "The installation request does not match its schema."
                    },
                )

        return guarded


installation_backup_router = APIRouter(
    prefix="/installation-backups", route_class=PrivateInstallationRoute
)
Destination = Annotated[str, Field(min_length=1, max_length=64)]


class BackupPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination_id: Destination


class RestorePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str | None = Field(default=None, max_length=512)
    archive_path: str | None = Field(default=None, max_length=4096)
    allow_shared_permissions: StrictBool = False


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_token: str = Field(min_length=1, max_length=128)
    confirmation: str = Field(min_length=1, max_length=160)
    acknowledgements: dict[str, StrictBool]


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str = Field(min_length=1, max_length=160)


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    destination_id: str = Field(default="", max_length=64)
    interval_hours: int = Field(default=24, ge=1, le=8760, strict=True)
    keep_last: int = Field(default=7, ge=1, le=100, strict=True)


def manager(
    _user=Depends(get_optional_current_user), handler=Depends(get_process_handler)
):
    return get_installation_backup_manager(CONFIG_MANAGER, handler)


def invoke(controller, operation, *args):
    try:
        return getattr(controller, operation)(*args)
    except (
        InstallationBackupError,
        InstallationArchiveError,
        BackupStorageError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except (OSError, ValueError, KeyError):
        raise HTTPException(
            status_code=409,
            detail="The owned installation archive, persistence inventory or recovery state is unavailable.",
        ) from None


@installation_backup_router.get("/status")
def status(
    response: Response,
    controller=Depends(manager),
    _user=Depends(get_optional_current_user),
):
    response.headers["Cache-Control"] = "no-store"
    return invoke(controller, "status")


@installation_backup_router.get("/catalog")
def catalog(
    destination_id: str = Query(..., min_length=1, max_length=64),
    controller=Depends(manager),
    _user=Depends(get_optional_current_user),
):
    return invoke(controller, "catalog", destination_id)


@installation_backup_router.post("/preview")
def preview(
    request: BackupPreviewRequest,
    response: Response,
    controller=Depends(manager),
    _user=Depends(get_optional_current_user),
):
    response.headers["Cache-Control"] = "no-store"
    return invoke(controller, "preview", request.model_dump())


@installation_backup_router.get("/preview/status")
def preview_status(
    response: Response,
    preview_id: str | None = Query(
        default=None, min_length=32, max_length=32, pattern="^[0-9a-f]{32}$"
    ),
    controller=Depends(manager),
    _user=Depends(get_optional_current_user),
):
    response.headers["Cache-Control"] = "no-store"
    return invoke(controller, "preview_status", preview_id)


@installation_backup_router.post("/preview/{preview_id}/cancel")
def cancel_preview(
    preview_id: str,
    response: Response,
    controller=Depends(manager),
    _user=Depends(get_optional_current_user),
):
    if len(preview_id) != 32 or any(
        value not in "0123456789abcdef" for value in preview_id
    ):
        raise HTTPException(status_code=409, detail="The preview identity is invalid.")
    response.headers["Cache-Control"] = "no-store"
    return invoke(controller, "cancel_preview", preview_id)


@installation_backup_router.post("/start")
def start(
    request: StartRequest,
    controller=Depends(manager),
    _user=Depends(get_optional_current_user),
):
    return invoke(controller, "start", request.model_dump())


@installation_backup_router.get("/jobs/{job_id}")
def job(
    job_id: str,
    response: Response,
    controller=Depends(manager),
    _user=Depends(get_optional_current_user),
):
    response.headers["Cache-Control"] = "no-store"
    return invoke(controller, "get_job", job_id)


@installation_backup_router.put("/schedule")
def schedule(
    request: ScheduleRequest,
    controller=Depends(manager),
    _user=Depends(get_optional_current_user),
):
    return invoke(controller, "update_schedule", request.model_dump())


@installation_backup_router.post("/restore/preview")
def restore_preview(
    request: RestorePreviewRequest,
    controller=Depends(manager),
    _user=Depends(get_optional_current_user),
):
    return invoke(controller, "restore_preview", request.model_dump())


@installation_backup_router.post("/restore/start")
def restore_start(
    request: StartRequest,
    controller=Depends(manager),
    _user=Depends(get_optional_current_user),
):
    return invoke(controller, "restore_start", request.model_dump())


@installation_backup_router.post("/jobs/{job_id}/rollback")
def rollback(
    job_id: str,
    request: RollbackRequest,
    controller=Depends(manager),
    _user=Depends(get_optional_current_user),
):
    return invoke(controller, "rollback", job_id, request.model_dump())
