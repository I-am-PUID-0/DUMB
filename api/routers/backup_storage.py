"""Authenticated selection of container-mounted cold backup destinations."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.background import BackgroundTask

from utils.backup_storage import (
    BackupStorageError,
    get_backup_storage,
    parse_backup_reference,
)
from utils.config_loader import CONFIG_MANAGER
from utils.dependencies import get_optional_current_user
from utils.infinidysk_v2_support import API_MINIMUM, api_version_supported
from utils.project_metadata import get_project_version
from utils.runtime_paths import pyproject_file

backup_storage_router = APIRouter(prefix="/backup-storage")
ProfileId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]


class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: ProfileId
    name: str = Field(default="", max_length=120)
    path: str = Field(min_length=1, max_length=4096)
    enabled: StrictBool = True
    make_default: StrictBool = False
    allow_shared_permissions: StrictBool = False


class DefaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination_id: str = Field(max_length=64)


def _invoke(operation, *args, mutation=False):
    if mutation and not api_version_supported(get_project_version(pyproject_file())):
        raise HTTPException(
            status_code=426, detail=f"DUMB API {API_MINIMUM} or newer is required."
        )
    try:
        storage = get_backup_storage(CONFIG_MANAGER)
        return getattr(storage, operation)(*args)
    except BackupStorageError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except (OSError, ValueError):
        raise HTTPException(
            status_code=409,
            detail="The mounted backup destination or its owned receipt is unavailable.",
        ) from None


@backup_storage_router.get("")
def storage_catalog(current_user=Depends(get_optional_current_user)):
    return _invoke("profiles")


@backup_storage_router.post("/preview")
def storage_preview(
    request: ProfileRequest, current_user=Depends(get_optional_current_user)
):
    return _invoke("preview", request.model_dump(), mutation=True)


@backup_storage_router.post("/profiles")
def storage_register(
    request: ProfileRequest, current_user=Depends(get_optional_current_user)
):
    return _invoke("save_profile", request.model_dump(), mutation=True)


@backup_storage_router.post("/default")
def storage_default(
    request: DefaultRequest, current_user=Depends(get_optional_current_user)
):
    return _invoke("set_default", request.destination_id, mutation=True)


@backup_storage_router.get("/artifacts")
def storage_artifacts(
    destination_id: str = Query(..., max_length=64),
    namespace: str = Query(..., max_length=80),
    current_user=Depends(get_optional_current_user),
):
    return {
        "protocol": 1,
        "artifacts": _invoke("list_artifacts", destination_id, namespace),
    }


def _artifact_chunks(reference):
    storage = get_backup_storage(CONFIG_MANAGER)
    with storage.open_artifact(reference) as handle:
        while chunk := handle.read(1024 * 1024):
            yield chunk


@backup_storage_router.get("/download")
def storage_download(
    reference: str = Query(..., max_length=512),
    current_user=Depends(get_optional_current_user),
):
    chunks = None
    try:
        _profile, _namespace, artifact = parse_backup_reference(reference)
        chunks = _artifact_chunks(reference)
        # Opening and full readback verification run in FastAPI's worker thread,
        # before sending response headers. The iterator retains its open lease
        # until the attachment is sent or the response is disconnected.
        first = next(chunks, b"")
    except (BackupStorageError, OSError, ValueError):
        if chunks is not None:
            chunks.close()
        raise HTTPException(
            status_code=409,
            detail="The owned backup could not be verified for download.",
        ) from None

    def body():
        try:
            if first:
                yield first
            yield from chunks
        finally:
            chunks.close()

    return StreamingResponse(
        body(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="dumb-backup-{artifact}.bin"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(chunks.close),
    )
