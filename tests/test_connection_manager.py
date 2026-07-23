import asyncio
import unittest

from starlette.websockets import WebSocketState

from api.connection_manager import ConnectionManager


class _FakeWebSocket:
    def __init__(self, send_error=None):
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED
        self.send_error = send_error
        self.messages = []
        self.send_loop = None
        self.sent = asyncio.Event()

    async def accept(self):
        return None

    async def close(self):
        self.application_state = WebSocketState.DISCONNECTED

    async def send_text(self, message):
        self.send_loop = asyncio.get_running_loop()
        if self.send_error:
            raise self.send_error
        self.messages.append(message)
        self.sent.set()


class ConnectionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_threaded_broadcast_runs_on_websocket_owner_loop(self):
        manager = ConnectionManager()
        websocket = _FakeWebSocket()
        await manager.connect(websocket)
        owner_loop = asyncio.get_running_loop()

        scheduled = await asyncio.to_thread(manager.schedule_broadcast, "hello")
        await asyncio.wait_for(websocket.sent.wait(), timeout=1)

        self.assertTrue(scheduled)
        self.assertIs(websocket.send_loop, owner_loop)
        self.assertEqual(websocket.messages, ["hello"])

    async def test_broadcast_prunes_disconnected_websocket(self):
        manager = ConnectionManager()
        websocket = _FakeWebSocket()
        await manager.connect(websocket)
        websocket.client_state = WebSocketState.DISCONNECTED

        await manager.broadcast("ignored")

        self.assertEqual(websocket.messages, [])
        self.assertNotIn(websocket, manager.active_connections)

    async def test_broadcast_prunes_websocket_after_transport_error(self):
        manager = ConnectionManager()
        websocket = _FakeWebSocket(send_error=BrokenPipeError())
        await manager.connect(websocket)

        await manager.broadcast("ignored")

        self.assertNotIn(websocket, manager.active_connections)


if __name__ == "__main__":
    unittest.main()
