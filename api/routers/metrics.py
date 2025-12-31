from fastapi import APIRouter, Depends, Query
from utils.dependencies import get_metrics_collector
from utils.config_loader import CONFIG_MANAGER
from utils.metrics_history_reader import read_history
import time


metrics_router = APIRouter()


@metrics_router.get("")
async def get_metrics_snapshot(collector=Depends(get_metrics_collector)):
    return collector.snapshot()


@metrics_router.get("/history")
async def get_metrics_history(
    since: float | None = Query(default=None),
    full: bool = Query(default=False),
    limit: int = Query(default=5000),
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
