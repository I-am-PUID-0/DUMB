import asyncio
from typing import List
from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        if websocket not in self.active_connections:
            self.active_connections.append(websocket)

    async def disconnect(self, websocket: WebSocket):
        async with self.lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        tasks = [
            asyncio.create_task(connection.send_text(message))
            for connection in self.active_connections
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self):
        self.active_connections.clear()
