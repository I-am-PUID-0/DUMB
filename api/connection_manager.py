import asyncio
from typing import Dict, Set
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState


class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.lock = asyncio.Lock()
        self.broadcast_lock = asyncio.Lock()
        self.send_locks: Dict[WebSocket, asyncio.Lock] = {}
        self.event_loop = None

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.event_loop = asyncio.get_running_loop()
        async with self.lock:
            self.active_connections.add(websocket)
            self.send_locks[websocket] = asyncio.Lock()

    async def disconnect(self, websocket: WebSocket):
        async with self.lock:
            self.active_connections.discard(websocket)
            self.send_locks.pop(websocket, None)

    async def send(self, websocket: WebSocket, message: str) -> bool:
        async with self.lock:
            send_lock = self.send_locks.get(websocket)
        if send_lock is None:
            return False

        async with send_lock:
            sent = await self._safe_send(websocket, message)
        if not sent:
            await self.disconnect(websocket)
        return sent

    async def broadcast(self, message: str):
        async with self.broadcast_lock:
            async with self.lock:
                connections = list(self.active_connections)
            if not connections:
                return
            tasks = [
                asyncio.create_task(self.send(conn, message)) for conn in connections
            ]
            await asyncio.gather(*tasks)

    def schedule_broadcast(self, message: str) -> bool:
        loop = self.event_loop
        if loop is None or loop.is_closed():
            return False
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        try:
            if running_loop is loop:
                self._create_broadcast_task(message)
            else:
                loop.call_soon_threadsafe(self._create_broadcast_task, message)
        except RuntimeError:
            return False
        return True

    def _create_broadcast_task(self, message: str):
        if not self.active_connections:
            return
        task = asyncio.create_task(self.broadcast(message))
        task.add_done_callback(self._consume_broadcast_result)

    @staticmethod
    def _consume_broadcast_result(task):
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass

    async def shutdown(self):
        async with self.lock:
            connections = list(self.active_connections)
            self.active_connections.clear()
            self.send_locks.clear()
        if connections:
            tasks = [asyncio.create_task(conn.close()) for conn in connections]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _safe_send(self, websocket: WebSocket, message: str) -> bool:
        if (
            getattr(websocket, "client_state", WebSocketState.CONNECTED)
            is not WebSocketState.CONNECTED
            or getattr(websocket, "application_state", WebSocketState.CONNECTED)
            is not WebSocketState.CONNECTED
        ):
            return False
        try:
            await websocket.send_text(message)
            return True
        except (WebSocketDisconnect, RuntimeError, OSError):
            return False
