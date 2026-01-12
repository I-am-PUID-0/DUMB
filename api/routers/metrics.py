from fastapi import APIRouter, Depends, Query
from utils.dependencies import get_metrics_collector, get_optional_current_user
from utils.config_loader import CONFIG_MANAGER
from utils.metrics_history_reader import read_history, read_history_series
import time


metrics_router = APIRouter()


@metrics_router.get("")
async def get_metrics_snapshot(
    collector=Depends(get_metrics_collector),
    current_user: str = Depends(get_optional_current_user),
):
    return collector.snapshot()


@metrics_router.get("/history")
async def get_metrics_history(
    since: float | None = Query(default=None),
    full: bool = Query(default=False),
    limit: int = Query(default=5000),
    current_user: str = Depends(get_optional_current_user),
):
    history_dir = (
        CONFIG_MANAGER.get("dumb", {})
        .get("metrics", {})
        .get("history_dir", "/config/metrics")
    )
    if since is None and not full:
        since = time.time() - (6 * 60 * 60)

    items, truncated = read_history(
        history_dir=history_dir,
        since=since,
        full=full,
        limit=limit,
        default_hours=6,
    )
    return {"items": items, "truncated": truncated}


@metrics_router.get("/history_series")
async def get_metrics_history_series(
    since: float | None = Query(default=None),
    full: bool = Query(default=False),
    limit: int = Query(default=5000),
    bucket_seconds: int | None = Query(default=None),
    max_points: int = Query(default=600),
    current_user: str = Depends(get_optional_current_user),
):
    history_dir = (
        CONFIG_MANAGER.get("dumb", {})
        .get("metrics", {})
        .get("history_dir", "/config/metrics")
    )
    if since is None and not full:
        since = time.time() - (6 * 60 * 60)

    items, series, truncated, stats, bucket_seconds = read_history_series(
        history_dir=history_dir,
        since=since,
        full=full,
        limit=limit,
        default_hours=6,
        bucket_seconds=bucket_seconds,
        max_points=max_points,
    )
    timestamps = [item.get("timestamp") for item in items]
    return {
        "items": items,
        "series": series,
        "timestamps": timestamps,
        "truncated": truncated,
        "stats": stats,
        "bucket_seconds": bucket_seconds,
    }
