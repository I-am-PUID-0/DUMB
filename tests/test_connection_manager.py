import asyncio
import unittest

from starlette.websockets import WebSocketState

from api.connection_manager import ConnectionManager


class _FakeWebSocket:
    def __init__(self, send_error=None, send_delay=0):
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED
        self.send_error = send_error
        self.send_delay = send_delay
        self.messages = []
        self.send_loop = None
        self.sent = asyncio.Event()
        self.active_sends = 0
        self.max_active_sends = 0

    async def accept(self):
        return None

    async def close(self):
        self.application_state = WebSocketState.DISCONNECTED

    async def send_text(self, message):
        self.send_loop = asyncio.get_running_loop()
        self.active_sends += 1
        self.max_active_sends = max(self.max_active_sends, self.active_sends)
        try:
            if self.send_delay:
                await asyncio.sleep(self.send_delay)
            if self.send_error:
                raise self.send_error
            self.messages.append(message)
            self.sent.set()
        finally:
            self.active_sends -= 1


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

    async def test_direct_send_prunes_websocket_after_transport_error(self):
        manager = ConnectionManager()
        websocket = _FakeWebSocket(send_error=BrokenPipeError())
        await manager.connect(websocket)

        sent = await manager.send(websocket, "ignored")

        self.assertFalse(sent)
        self.assertNotIn(websocket, manager.active_connections)

    async def test_direct_and_broadcast_sends_are_serialized_per_connection(self):
        manager = ConnectionManager()
        websocket = _FakeWebSocket(send_delay=0.01)
        await manager.connect(websocket)

        await asyncio.gather(
            manager.send(websocket, "direct"),
            manager.broadcast("broadcast"),
        )

        self.assertEqual(websocket.max_active_sends, 1)
        self.assertCountEqual(websocket.messages, ["direct", "broadcast"])


if __name__ == "__main__":
    unittest.main()
