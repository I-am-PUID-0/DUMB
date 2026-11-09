import http.client
import http.server
import socket
import socketserver
import threading
import unittest
from types import SimpleNamespace

from utils.infinidysk_v2_proxy import CompatibilityProxy, start_compatibility_proxy


class ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        try:
            while data := self.request.recv(8192):
                self.request.sendall(data)
        except OSError:
            pass


class Application(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b"native-fixture-media"
        if self.headers.get("Range") == "bytes=3-8":
            body = body[3:9]
            self.send_response(206)
            self.send_header("Content-Range", "bytes 3-8/20")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Fixture-Path", self.path)
        self.end_headers()
        self.wfile.write(body)


class CompatibilityProxyTests(unittest.TestCase):
    def server(self, handler):
        server = ThreadedServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

        self.addCleanup(cleanup)
        return server.server_address[1]

    def proxy(self, target, **kwargs):
        proxy = CompatibilityProxy("127.0.0.1", 0, target, **kwargs)
        self.addCleanup(proxy.close)
        return proxy

    def test_retained_origin_forwards_signed_paths_and_ranges_unchanged(self):
        proxy = self.proxy(self.server(Application))
        connection = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=2)
        self.addCleanup(connection.close)
        path = "/view/.ids/a/b/c/d/e/fixture?downloadKey=fixture&extension=mkv"
        connection.request("GET", path, headers={"Range": "bytes=3-8"})
        response = connection.getresponse()
        self.assertEqual(response.status, 206)
        self.assertEqual(response.getheader("X-Fixture-Path"), path)
        self.assertEqual(response.getheader("Content-Range"), "bytes 3-8/20")
        self.assertEqual(response.read(), b"ive-fi")

    def test_binary_bidirectional_stream_and_half_close(self):
        proxy = self.proxy(self.server(Echo))
        with socket.create_connection(
            ("127.0.0.1", proxy.port), timeout=2
        ) as connection:
            # Includes non-UTF8 and HTTP/WebSocket-like framing: no protocol rewriting.
            payload = bytes(range(256)) * 256
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
            received = bytearray()
            while chunk := connection.recv(8192):
                received.extend(chunk)
        self.assertEqual(received, payload)

    def test_connection_limit_idle_timeout_and_close_release_listener(self):
        proxy = self.proxy(self.server(Echo), max_connections=1, idle_timeout=0.3)
        first = socket.create_connection(("127.0.0.1", proxy.port), timeout=2)
        self.addCleanup(first.close)
        first.sendall(b"ready")
        self.assertEqual(first.recv(5), b"ready")
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=2) as rejected:
            self.assertEqual(rejected.recv(1), b"")
        self.assertEqual(first.recv(1), b"")
        port = proxy.port
        proxy.close()
        self.assertFalse(proxy.thread.is_alive())
        with socket.socket() as replacement:
            replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            replacement.bind(("127.0.0.1", port))

    def test_child_exit_closes_listener_and_active_connections(self):
        child = SimpleNamespace(returncode=None)
        proxy = self.proxy(self.server(Echo), process=child)
        with socket.create_connection(
            ("127.0.0.1", proxy.port), timeout=2
        ) as connection:
            connection.sendall(b"ready")
            self.assertEqual(connection.recv(5), b"ready")
            child.returncode = 1
            proxy.thread.join(timeout=2)
            self.assertFalse(proxy.thread.is_alive())
            self.assertEqual(connection.recv(1), b"")

    def test_conflicting_listener_is_refused_and_same_port_needs_no_proxy(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            with self.assertRaises(OSError):
                CompatibilityProxy("127.0.0.1", port, 3333)
        self.assertIsNone(
            start_compatibility_proxy(
                {"frontend_port": 3333, "backend_port": 3333},
                SimpleNamespace(returncode=None),
            )
        )
