import asyncio
from typing import Set
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect


class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.lock = asyncio.Lock()
        self.loop = None

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        if self.loop is None or self.loop.is_closed():
            self.loop = asyncio.get_running_loop()
        async with self.lock:
            self.active_connections.add(websocket)

    async def disconnect(self, websocket: WebSocket):
        async with self.lock:
            self.active_connections.discard(websocket)

    async def broadcast(self, message: str):
        async with self.lock:
            connections = list(self.active_connections)
        if not connections:
            return
        tasks = [asyncio.create_task(self._safe_send(conn, message)) for conn in connections]
        results = await asyncio.gather(*tasks)
        stale = [conn for conn, ok in zip(connections, results) if not ok]
        if stale:
            async with self.lock:
                for conn in stale:
                    self.active_connections.discard(conn)

    async def shutdown(self):
        async with self.lock:
            connections = list(self.active_connections)
            self.active_connections.clear()
        if connections:
            tasks = [asyncio.create_task(conn.close()) for conn in connections]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _safe_send(self, websocket: WebSocket, message: str) -> bool:
        try:
            await websocket.send_text(message)
            return True
        except (WebSocketDisconnect, RuntimeError):
            return False
