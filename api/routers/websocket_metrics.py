import asyncio
import json
from fastapi import APIRouter, Depends, WebSocket
from starlette.websockets import WebSocketDisconnect
from utils.dependencies import get_metrics_collector, get_metrics_manager
from utils.config_loader import CONFIG_MANAGER
from utils.metrics_history_reader import read_history


websocket_metrics_router = APIRouter()


@websocket_metrics_router.websocket("/metrics")
async def websocket_metrics(
    websocket: WebSocket,
    collector=Depends(get_metrics_collector),
    metrics_manager=Depends(get_metrics_manager),
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

    await metrics_manager.connect(websocket)
    try:
        if history_enabled:
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

        while True:
            payload = collector.snapshot()
            await websocket.send_text(json.dumps({"type": "snapshot", "data": payload}))
            await asyncio.sleep(interval)
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
