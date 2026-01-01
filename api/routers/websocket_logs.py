from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from utils.dependencies import get_websocket_manager
import json


websocket_router = APIRouter()


@websocket_router.websocket("/logs")
async def websocket_logs(
    websocket: WebSocket, websocket_manager=Depends(get_websocket_manager)
):
    await websocket_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
                continue
            if data and data[0] == "{":
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") == "ping":
                    await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        await websocket_manager.disconnect(websocket)
