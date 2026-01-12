import asyncio
import json
from fastapi import APIRouter, Depends, WebSocket
from starlette.websockets import WebSocketDisconnect
from utils.dependencies import (
    get_metrics_collector,
    get_metrics_manager,
    get_websocket_current_user,
)
from utils.config_loader import CONFIG_MANAGER
from utils.metrics_history_reader import read_history, read_history_series


websocket_metrics_router = APIRouter()
_publisher_task = None
_publisher_lock = asyncio.Lock()
_publisher_interval = 2.0
_latest_snapshot = None


@websocket_metrics_router.websocket("/metrics")
async def websocket_metrics(
    websocket: WebSocket,
    collector=Depends(get_metrics_collector),
    metrics_manager=Depends(get_metrics_manager),
    current_user: str = Depends(get_websocket_current_user),
):
    interval = 2.0
    if "interval" in websocket.query_params:
        try:
            interval = float(websocket.query_params["interval"])
            interval = max(0.5, min(interval, 10.0))
        except ValueError:
            interval = 2.0

    history_enabled = _parse_bool(websocket.query_params.get("history", "false"))
    history_full = _parse_bool(websocket.query_params.get("history_full", "false"))
    history_limit = _parse_int(websocket.query_params.get("history_limit"), 5000)
    history_since = _parse_float(websocket.query_params.get("history_since"))
    history_bucket = _parse_int(websocket.query_params.get("history_bucket"))
    history_points = _parse_int(websocket.query_params.get("history_points"), 600)
    bootstrap = _parse_bool(websocket.query_params.get("bootstrap", "false"))

    await metrics_manager.connect(websocket)
    try:
        if bootstrap:
            history_dir = (
                CONFIG_MANAGER.get("dumb", {})
                .get("metrics", {})
                .get("history_dir", "/config/metrics")
            )
            items, series, truncated, stats, bucket_seconds = read_history_series(
                history_dir=history_dir,
                since=history_since,
                full=history_full,
                limit=history_limit,
                default_hours=6,
                bucket_seconds=history_bucket,
                max_points=history_points,
            )
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "bootstrap",
                        "snapshot": _latest_snapshot or collector.snapshot(),
                        "items": items,
                        "series": series,
                        "timestamps": [item.get("timestamp") for item in items],
                        "truncated": truncated,
                        "stats": stats,
                        "bucket_seconds": bucket_seconds,
                    }
                )
            )
        elif history_enabled:
            history_dir = (
                CONFIG_MANAGER.get("dumb", {})
                .get("metrics", {})
                .get("history_dir", "/config/metrics")
            )
            items, truncated = read_history(
                history_dir=history_dir,
                since=history_since,
                full=history_full,
                limit=history_limit,
                default_hours=6,
            )
            await websocket.send_text(
                json.dumps({"type": "history", "items": items, "truncated": truncated})
            )

        await _ensure_publisher(collector, metrics_manager, interval)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await metrics_manager.disconnect(websocket)


def _parse_bool(value):
    if value is None:
        return False
    value = value.strip().lower()
    return value in ("1", "true", "yes", "on")


def _parse_int(value, default=None):
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_float(value, default=None):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def _ensure_publisher(collector, metrics_manager, interval):
    global _publisher_task, _publisher_interval
    async with _publisher_lock:
        _publisher_interval = min(_publisher_interval, interval) if _publisher_task else interval
        if _publisher_task is None or _publisher_task.done():
            _publisher_task = asyncio.create_task(_publisher_loop(collector, metrics_manager))


async def _publisher_loop(collector, metrics_manager):
    global _latest_snapshot, _publisher_interval
    while True:
        try:
            if not metrics_manager.active_connections:
                await asyncio.sleep(_publisher_interval)
                continue
            snapshot = await asyncio.to_thread(collector.snapshot)
            _latest_snapshot = snapshot
            await metrics_manager.broadcast(json.dumps({"type": "snapshot", "data": snapshot}))
        except Exception:
            await asyncio.sleep(_publisher_interval)
            continue
        await asyncio.sleep(_publisher_interval)
