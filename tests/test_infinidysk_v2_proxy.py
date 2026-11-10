import http.client
import http.server
import queue
import socket
import socketserver
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from utils.infinidysk_v2_proxy import (
    CompatibilityProxy,
    compatibility_auth_environment,
    compatibility_auth_policy,
    start_compatibility_proxy,
)


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
        self.server.received_paths.put(self.path)
        body = b"native-fixture-media"
        if self.headers.get("Range") == "bytes=3-8":
            body = body[3:9]
            self.send_response(206)
            self.send_header("Content-Range", "bytes 3-8/20")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class UploadApplication(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.connection.settimeout(2)
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.received_paths.put(
            (self.command, self.path, self.headers.get("Authorization"), body)
        )
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_PROPFIND = do_POST


class UpgradeApplication(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.connection.settimeout(2)
        self.server.received_paths.put(self.path)
        self.send_response(101)
        self.send_header("Connection", "Upgrade")
        self.send_header("Upgrade", "websocket")
        self.end_headers()
        self.wfile.flush()
        frame = self.rfile.read(8)
        self.wfile.write(frame)
        self.wfile.flush()
        self.server.received_paths.put(frame)
        self.close_connection = True


class CompatibilityProxyTests(unittest.TestCase):
    def server(self, handler, *, received_paths=None):
        server = ThreadedServer(("127.0.0.1", 0), handler)
        server.received_paths = received_paths
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
        received_paths = queue.Queue()
        proxy = self.proxy(self.server(Application, received_paths=received_paths))
        connection = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=2)
        self.addCleanup(connection.close)
        path = "/view/.ids/a/b/c/d/e/fixture?downloadKey=fixture&extension=mkv"
        connection.request("GET", path, headers={"Range": "bytes=3-8"})
        response = connection.getresponse()
        self.assertEqual(response.status, 206)
        self.assertEqual(received_paths.get(timeout=2), path)
        self.assertIsNone(response.getheader("X-Fixture-Path"))
        self.assertEqual(response.getheader("Content-Range"), "bytes 3-8/20")
        self.assertEqual(response.read(), b"ive-fi")

    def test_sab_and_webdav_requests_preserve_method_uri_auth_and_binary_body(self):
        received = queue.Queue()
        proxy = self.proxy(self.server(UploadApplication, received_paths=received))
        payload = b"fixture-upload" + bytes(range(256))
        for method, path in (
            ("POST", "/api?mode=addfile&apikey=fixture&name=a%20b.nzb"),
            ("PROPFIND", "/content/a%20b/%FF?fixture=one%26two"),
        ):
            with self.subTest(method=method):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", proxy.port, timeout=2
                )
                self.addCleanup(connection.close)
                authorization = "Basic Zml4dHVyZTpmaXh0dXJl"
                connection.request(
                    method,
                    path,
                    body=payload,
                    headers={"Authorization": authorization},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), payload)
                self.assertEqual(
                    received.get(timeout=2), (method, path, authorization, payload)
                )
                connection.close()

    def test_http_upgrade_keeps_uri_and_following_binary_frames_unchanged(self):
        received = queue.Queue()
        proxy = self.proxy(self.server(UpgradeApplication, received_paths=received))
        path = "/fixture-upgrade?token=fixture%2Bvalue"
        frame = b"\x82\x06" + bytes(range(6))
        with socket.create_connection(
            ("127.0.0.1", proxy.port), timeout=2
        ) as connection:
            connection.settimeout(2)
            with connection.makefile("rb") as stream:
                connection.sendall(
                    f"GET {path} HTTP/1.1\r\nHost: fixture.invalid\r\n"
                    "Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n".encode() + frame
                )
                self.assertIn(b"101", stream.readline(8192))
                for _ in range(16):
                    if stream.readline(8192) == b"\r\n":
                        break
                else:
                    self.fail("Unbounded upgrade response headers")
                self.assertEqual(stream.read(len(frame)), frame)
        self.assertEqual(received.get(timeout=2), path)
        self.assertEqual(received.get(timeout=2), frame)

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


class CompatibilityAuthPolicyTests(unittest.TestCase):
    def test_every_compatibility_listener_requires_auth_including_loopback(self):
        for host in ("0.0.0.0", "127.0.0.1", "::1", "192.0.2.3"):
            with self.subTest(host=host):
                service = {
                    "frontend_port": 3333,
                    "backend_port": 8888,
                    "env": {
                        "ASPNETCORE_URLS": (
                            f"http://[{host}]" if ":" in host else f"http://{host}"
                        )
                    },
                }
                self.assertEqual(
                    compatibility_auth_policy(service),
                    {
                        "enforced": True,
                        "mode": "required",
                        "reason": "legacy_compatibility_listener",
                    },
                )
                self.assertEqual(
                    compatibility_auth_environment(service),
                    {"INFINIDYSK__AUTH__MODE": "required"},
                )
                with (
                    patch("utils.infinidysk_v2_proxy.CompatibilityProxy") as proxy,
                    self.assertLogs(
                        "utils.infinidysk_v2_proxy", level="WARNING"
                    ) as logs,
                ):
                    child = SimpleNamespace(returncode=None)
                    self.assertIs(
                        start_compatibility_proxy(service, child), proxy.return_value
                    )
                proxy.assert_called_once_with(host, 8888, 3333, process=child)
                self.assertIn(
                    "Saved native authentication settings are unchanged", logs.output[0]
                )

    def test_disabled_relay_preserves_explicit_native_policy(self):
        service = {
            "backend_port": 3333,
            "frontend_port": 3333,
            "env": {"INFINIDYSK__AUTH__MODE": "local"},
        }
        self.assertEqual(
            compatibility_auth_policy(service),
            {"enforced": False, "mode": None, "reason": None},
        )
        self.assertEqual(compatibility_auth_environment(service), {})
        with patch("utils.infinidysk_v2_proxy.CompatibilityProxy") as proxy:
            self.assertIsNone(start_compatibility_proxy(service, None))
        proxy.assert_not_called()

    def test_conflicting_or_parent_auth_overrides_cannot_create_listener(self):
        cases = [
            {"INFINIDYSK__AUTH__MODE": "local"},
            {"INFINIDYSK__AUTH__MODE": "disabled"},
            {"INFINIDYSK__AUTH__MODE": None},
            {"INFINIDYSK__AUTH": None},
            {"INFINIDYSK__Auth__Mode": "local"},
            {"INFINIDYSK__AUTH__MODE": "required", "INFINIDYSK__Auth__Mode": "local"},
            {"INFINIDYSK__AUTH": '{"mode":"local"}'},
            {"INFINIDYSK__Auth": '{"mode":"required"}'},
        ]
        for environment in cases:
            with self.subTest(keys=list(environment)):
                service = {"env": environment}
                with patch("utils.infinidysk_v2_proxy.CompatibilityProxy") as proxy:
                    with self.assertRaisesRegex(
                        ValueError, "requires native admin authentication"
                    ):
                        start_compatibility_proxy(service, None)
                proxy.assert_not_called()
