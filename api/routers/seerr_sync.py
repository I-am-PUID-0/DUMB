"""
Seerr Sync API Router - Exposes sync status and details.
"""

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from utils.dependencies import get_optional_current_user
from utils.config_loader import CONFIG_MANAGER
from utils.global_logger import logger
from utils.seerr_sync import _join_url, _seerr_req
import json
import os
import time
import urllib.error

seerr_sync_router = APIRouter()

_SYNC_STATE_FILE = "/config/seerr_sync_state.json"


class SeerrSyncTestRequest(BaseModel):
    url: str
    api_key: str


def _load_sync_state() -> dict:
    """Load sync state from file."""
    if os.path.exists(_SYNC_STATE_FILE):
        try:
            with open(_SYNC_STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Failed to load seerr sync state: %s", e)
    return {}


def _get_sync_config() -> dict:
    """Get seerr_sync configuration."""
    return CONFIG_MANAGER.get("seerr_sync", {}) or {}


def _count_subordinate_stats(state: dict) -> dict:
    """Count synced requests per subordinate."""
    stats = {}
    for fp, req_info in state.get("requests", {}).items():
        for sub_label, sub_info in req_info.get("subordinates", {}).items():
            if sub_label not in stats:
                stats[sub_label] = {"synced": 0}
            stats[sub_label]["synced"] += 1

    # Count failures per subordinate
    for failed_key, failed_info in state.get("failed", {}).items():
        if "||" in failed_key:
            _, sub_label = failed_key.split("||", 1)
            if sub_label not in stats:
                stats[sub_label] = {"synced": 0}
            stats[sub_label].setdefault("failed", 0)
            stats[sub_label]["failed"] += 1

    return stats


@seerr_sync_router.get("/status")
async def get_seerr_sync_status(
    current_user: str = Depends(get_optional_current_user),
):
    """Get summary of seerr-sync operation status."""
    sync_cfg = _get_sync_config()
    enabled = sync_cfg.get("enabled", False)

    if not enabled:
        return {
            "enabled": False,
            "status": "disabled",
        }

    state = _load_sync_state()
    if not state:
        return {
            "enabled": True,
            "status": "initializing",
            "message": "Sync state not yet created",
        }

    # Calculate next poll time
    last_poll_ts = state.get("last_poll_ts")
    poll_interval = sync_cfg.get("poll_interval_seconds", 60)
    next_poll_ts = None
    if last_poll_ts:
        try:
            last_poll_time = time.mktime(
                time.strptime(last_poll_ts, "%Y-%m-%dT%H:%M:%SZ")
            )
            next_poll_time = last_poll_time + poll_interval
            next_poll_ts = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(next_poll_time)
            )
        except Exception:
            pass

    subordinate_stats = _count_subordinate_stats(state)

    return {
        "enabled": True,
        "status": "active",
        "last_poll": last_poll_ts,
        "next_poll": next_poll_ts,
        "poll_interval_seconds": poll_interval,
        "total_requests_tracked": len(state.get("requests", {})),
        "total_failed": len(state.get("failed", {})),
        "subordinates": subordinate_stats,
    }


@seerr_sync_router.get("/failed")
async def get_seerr_sync_failed(
    current_user: str = Depends(get_optional_current_user),
):
    """Get list of failed sync requests with details."""
    state = _load_sync_state()
    failed = state.get("failed", {})

    # Transform to more readable format
    failed_list = []
    for failed_key, info in failed.items():
        parts = failed_key.split("||")
        fingerprint = parts[0] if parts else failed_key
        sub_label = parts[1] if len(parts) > 1 else "unknown"

        failed_list.append(
            {
                "fingerprint": fingerprint,
                "subordinate": sub_label,
                "media_type": info.get("media_type"),
                "tmdb_id": info.get("tmdb_id"),
                "error": info.get("error"),
                "failed_at": info.get("failed_at"),
            }
        )

    return {
        "count": len(failed_list),
        "failed_requests": failed_list,
    }


@seerr_sync_router.get("/state")
async def get_seerr_sync_state(
    current_user: str = Depends(get_optional_current_user),
):
    """Get full raw sync state (for debugging)."""
    state = _load_sync_state()
    return state


@seerr_sync_router.post("/test")
async def test_seerr_sync_connection(
    payload: SeerrSyncTestRequest,
    current_user: str = Depends(get_optional_current_user),
):
    """Test connectivity to a Seerr instance with the provided URL and API key."""
    if not payload.url or not payload.api_key:
        raise HTTPException(status_code=400, detail="URL and API key are required.")

    test_url = _join_url(payload.url, "/api/v1/status")
    try:
        status = _seerr_req(test_url, payload.api_key, method="GET")
        return {"ok": True, "status": status}
    except urllib.error.HTTPError as e:
        detail = f"Seerr responded with HTTP {e.code}"
        raise HTTPException(status_code=400, detail=detail)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Connection failed: {e}")


@seerr_sync_router.delete("/failed")
async def clear_failed_requests(
    fingerprint: str = Query(None, description="Clear specific fingerprint (optional)"),
    current_user: str = Depends(get_optional_current_user),
):
    """Clear failed requests to retry them on next sync cycle."""
    state = _load_sync_state()

    if not state.get("failed"):
        return {"message": "No failed requests to clear", "cleared": 0}

    if fingerprint:
        # Clear specific fingerprint across all subordinates
        keys_to_remove = [
            k for k in state["failed"].keys() if k.startswith(f"{fingerprint}||")
        ]
        for key in keys_to_remove:
            state["failed"].pop(key, None)
        cleared = len(keys_to_remove)
    else:
        # Clear all failed requests
        cleared = len(state["failed"])
        state["failed"] = {}

    # Save updated state
    try:
        with open(_SYNC_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        return {"message": f"Cleared {cleared} failed request(s)", "cleared": cleared}
    except Exception as e:
        logger.error("Failed to save sync state: %s", e)
        return {"error": str(e), "cleared": 0}
