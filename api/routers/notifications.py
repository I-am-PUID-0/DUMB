from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.dependencies import (
    get_notification_manager,
    get_optional_current_user,
)
from utils.notifications import SEVERITY_RANK, SUPPORTED_EVENT_TYPES

notifications_router = APIRouter()


class NotificationConfigRequest(BaseModel):
    config: Dict[str, Any]


class NotificationTestRequest(BaseModel):
    destination_id: str
    title: Optional[str] = None
    body: Optional[str] = None


class ManualNotificationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=10000)
    severity: str = "info"
    destination_ids: Optional[List[str]] = None


@notifications_router.get("/config")
def get_notification_config(
    manager=Depends(get_notification_manager),
    current_user: str = Depends(get_optional_current_user),
):
    return manager.get_config(redact=True)


@notifications_router.post("/config")
def update_notification_config(
    request: NotificationConfigRequest,
    manager=Depends(get_notification_manager),
    current_user: str = Depends(get_optional_current_user),
):
    try:
        return manager.update_config(request.config)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


@notifications_router.get("/events")
def get_notification_events(
    current_user: str = Depends(get_optional_current_user),
):
    return {
        "event_types": list(SUPPORTED_EVENT_TYPES),
        "severities": list(SEVERITY_RANK),
    }


@notifications_router.post("/test")
def test_notification_destination(
    request: NotificationTestRequest,
    manager=Depends(get_notification_manager),
    current_user: str = Depends(get_optional_current_user),
):
    try:
        return manager.send_test(request.destination_id, request.title, request.body)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


@notifications_router.post("/send")
def send_manual_notification(
    request: ManualNotificationRequest,
    manager=Depends(get_notification_manager),
    current_user: str = Depends(get_optional_current_user),
):
    if request.severity not in SEVERITY_RANK:
        raise HTTPException(status_code=400, detail="Unsupported severity.")
    queued = manager.send_manual(
        request.title,
        request.body,
        severity=request.severity,
        destination_ids=request.destination_ids,
    )
    if not queued:
        raise HTTPException(
            status_code=400,
            detail="No enabled destination with a configured URL matched the request.",
        )
    return {"status": "queued", "delivery_ids": queued}


@notifications_router.get("/history")
def get_notification_history(
    limit: int = Query(100, ge=1, le=500),
    status: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    manager=Depends(get_notification_manager),
    current_user: str = Depends(get_optional_current_user),
):
    return {"items": manager.history(limit=limit, status=status, event_type=event_type)}


@notifications_router.delete("/history")
def clear_notification_history(
    manager=Depends(get_notification_manager),
    current_user: str = Depends(get_optional_current_user),
):
    return {"deleted": manager.clear_history()}
