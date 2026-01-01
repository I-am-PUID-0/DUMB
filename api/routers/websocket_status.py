from fastapi import APIRouter, Depends, WebSocket
from starlette.websockets import WebSocketDisconnect
from utils.dependencies import get_api_state, get_status_manager
import asyncio, json

websocket_status_router = APIRouter()


@websocket_status_router.websocket("/status")
async def websocket_status(
    websocket: WebSocket,
    api_state=Depends(get_api_state),
    status_manager=Depends(get_status_manager),
):
    interval = 2.0
    if "interval" in websocket.query_params:
        try:
            interval = float(websocket.query_params["interval"])
            interval = max(0.5, min(interval, 10.0))
        except ValueError:
            interval = 2.0

    include_health = _parse_bool(websocket.query_params.get("health"))

    await status_manager.connect(websocket)
    try:
        while True:
            if include_health:
                snapshot = api_state.get_running_status_snapshot(include_health=True)
                payload = {"type": "status", "processes": snapshot}
            else:
                running = api_state.get_running_processes()
                payload = {"type": "status", "running": running}
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        pass
    finally:
        await status_manager.disconnect(websocket)


def _parse_bool(value):
    if value is None:
        return False
    value = value.strip().lower()
    return value in ("1", "true", "yes", "on")
