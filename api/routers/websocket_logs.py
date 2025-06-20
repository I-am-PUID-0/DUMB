from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from utils.dependencies import get_websocket_manager

websocket_router = APIRouter()


@websocket_router.websocket("/logs")
async def websocket_logs(
    websocket: WebSocket, websocket_manager=Depends(get_websocket_manager)
):
    await websocket_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await websocket_manager.disconnect(websocket)
