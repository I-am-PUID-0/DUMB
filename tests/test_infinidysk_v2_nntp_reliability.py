"""Pooled native-session and bounded transport recovery on owned loopback peers."""

from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import socket
import socketserver
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from tests.test_infinidysk_v2_nntp import client, provider
from utils.infinidysk_v2_nntp import (
    ContainedRelays,
    NntpBridge,
    PROVIDER_ENV,
    _ProviderSession,
)
from utils.infinidysk_v2_provider_diagnostics import sanitize_provider_diagnostics


class ScriptedPeer:
    def __init__(self, action=None, greeting=b"200 fixture ready\r\n"):
        self.action = action
        self.greeting = greeting
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.clients, self.threads = set(), set()
        self.connections, self.commands = 0, []
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                with owner.lock:
                    owner.connections += 1
                    serial = owner.connections
                    owner.clients.add(self.request)
                    owner.threads.add(threading.current_thread())
                self.request.settimeout(3)
                try:
                    self.wfile.write(owner.greeting)
                    self.wfile.flush()
                    while command := self.rfile.readline(8193):
                        with owner.lock:
                            owner.commands.append(command)
                        if (
                            owner.action
                            and owner.action(self, serial, command) is False
                        ):
                            return
                        verb = command.split(b" ", 1)[0].strip().upper()
                        if verb == b"AUTHINFO":
                            reply = (
                                b"381 password required\r\n"
                                if b" USER " in command
                                else b"281 authenticated\r\n"
                            )
                        elif verb == b"MODE":
                            reply = b"200 reader mode\r\n"
                        elif verb == b"DATE":
                            reply = b"111 20260101000000\r\n"
                        elif verb == b"BODY":
                            reply = b"222 article follows\r\nfixture body\r\n.\r\n"
                        else:
                            reply = b"223 article exists\r\n"
                        self.wfile.write(reply)
                        self.wfile.flush()
                except OSError:
                    pass
                finally:
                    with owner.lock:
                        owner.clients.discard(self.request)
                        owner.threads.discard(threading.current_thread())

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            block_on_close = False

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        with self.lock:
            clients, threads = list(self.clients), list(self.threads)
        for stream in clients:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            stream.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for thread in threads:
            thread.join(timeout=2)
        assert not self.thread.is_alive()
        assert not any(thread.is_alive() for thread in threads)


class NntpReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nntp-reliability-")
        self.addCleanup(self.temp.cleanup)

    def peer(self, *args, **kwargs):
        peer = ScriptedPeer(*args, **kwargs)
        self.addCleanup(peer.close)
        return peer

    def bridge(self, peer, count=1, **settings):
        root = Path(self.temp.name) / str(len(list(Path(self.temp.name).iterdir())))
        root.mkdir(mode=0o700)
        bridge = NntpBridge(
            [provider(peer.port, **settings) for _ in range(count)],
            root,
            os.geteuid(),
            os.getegid(),
        ).start()
        self.addCleanup(bridge.close)
        return bridge

    def diagnostics(self, bridge):
        result = bridge.stats["provider_diagnostics"]
        self.assertEqual(sanitize_provider_diagnostics(result), result)
        return result["providers"][0]

    def body(self, stream, response):
        stream.sendall(b"BODY <fixture@example.invalid>\r\n")
        self.assertEqual(response.readline(), b"222 article follows\r\n")
        self.assertEqual(response.readline(), b"fixture body\r\n")
        self.assertEqual(response.readline(), b".\r\n")

    def test_idle_native_session_survives_and_replays_accepted_initialization(self):
        peer = self.peer()
        bridge = self.bridge(peer)
        with (
            patch("utils.infinidysk_v2_nntp.CLIENT_IDLE_TIMEOUT", 0.05),
            client(bridge) as (stream, response),
        ):
            self.assertTrue(response.readline().startswith(b"200 "))
            for command, code in (
                (b"AUTHINFO USER fixture-user\r\n", b"381"),
                (b"AUTHINFO PASS fixture-password\r\n", b"281"),
                (b"MODE READER\r\n", b"200"),
            ):
                stream.sendall(command)
                self.assertTrue(response.readline().startswith(code))
            self.body(stream, response)
            deadline = time.monotonic() + 2
            while bridge.stats["active_connections"] and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(bridge.stats["active_connections"], 0)
            # Split a command over a period longer than the lease-idle timer.
            stream.sendall(b"BO")
            time.sleep(0.1)
            stream.sendall(b"DY <fixture@example.invalid>\r\n")
            self.assertEqual(response.readline(), b"222 article follows\r\n")
            self.assertEqual(response.readline(), b"fixture body\r\n")
            self.assertEqual(response.readline(), b".\r\n")
            stream.sendall(b"DATE\r\nSTAT <fixture@example.invalid>\r\n")
            self.assertEqual(response.readline(), b"111 20260101000000\r\n")
            self.assertEqual(response.readline(), b"223 article exists\r\n")
        self.assertEqual(peer.connections, 2)
        self.assertEqual(peer.commands.count(b"AUTHINFO USER fixture-user\r\n"), 2)
        self.assertEqual(peer.commands.count(b"AUTHINFO PASS fixture-password\r\n"), 2)
        self.assertEqual(peer.commands.count(b"MODE READER\r\n"), 2)
        self.assertEqual(self.diagnostics(bridge)["failures"], {})

    def test_busy_primaries_yield_to_queued_fallback_without_closing_native(self):
        release = threading.Event()
        arrived = threading.Event()
        entered = []
        lock = threading.Lock()

        def block_stat(handler, serial, command):
            if command.startswith(b"STAT ") and not release.is_set():
                with lock:
                    entered.append(serial)
                    if len(entered) == 4:
                        arrived.set()
                release.wait(3)

        peer = self.peer(block_stat)
        self.addCleanup(release.set)
        bridge = self.bridge(peer, count=3)
        with ExitStack() as stack:
            sessions = []
            for index in (0, 0, 1, 1):
                stream, response = stack.enter_context(client(bridge, index=index))
                self.assertTrue(response.readline().startswith(b"200 "))
                stream.sendall(b"STAT <fixture>\r\n")
                sessions.append((stream, response))
            self.assertTrue(arrived.wait(2))
            self.assertEqual(bridge.stats["active_connections"], 4)
            # Queue a fallback while all physical slots are genuinely doing I/O.
            fallback = socket.socket(socket.AF_UNIX)
            stack.callback(fallback.close)
            fallback.settimeout(0.1)
            fallback.connect(str(bridge.path))
            fallback.sendall(
                json.dumps({"provider": 2, "token": bridge.token}).encode() + b"\n"
            )
            with self.assertRaises(socket.timeout):
                fallback.recv(1024)
            release.set()
            fallback.settimeout(2)
            self.assertTrue(fallback.recv(1024).startswith(b"200 "))
            for stream, response in sessions:
                self.assertTrue(response.readline().startswith(b"223 "))
                stream.sendall(b"DATE\r\n")
                self.assertTrue(response.readline().startswith(b"111 "))
        self.assertEqual(bridge.stats["peak_connections"], 4)
        self.assertEqual(bridge.stats["provider_failures"], 0)

    def test_disconnect_before_status_is_retried_without_duplicate_prefix(self):
        attempts = []

        def drop_once(handler, serial, command):
            if command.startswith(b"BODY "):
                attempts.append(serial)
                if len(attempts) == 1:
                    handler.wfile.write(b"22")  # truncated status, never exposed
                    handler.wfile.flush()
                    return False

        peer = self.peer(drop_once)
        bridge = self.bridge(peer)
        with client(bridge) as (stream, response):
            self.assertTrue(response.readline().startswith(b"200 "))
            self.body(stream, response)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.diagnostics(bridge)["retries"], 1)
        self.assertEqual(self.diagnostics(bridge)["failures"], {"connection_closed": 1})

    def test_partial_body_is_never_replayed_or_finished_with_synthetic_status(self):
        def partial(handler, serial, command):
            if command.startswith(b"BODY "):
                handler.wfile.write(b"222 article follows\r\npartial body\r\n")
                handler.wfile.flush()
                return False

        peer = self.peer(partial)
        bridge = self.bridge(peer)
        with client(bridge) as (stream, response):
            self.assertTrue(response.readline().startswith(b"200 "))
            stream.sendall(b"BODY <fixture>\r\n")
            self.assertEqual(response.readline(), b"222 article follows\r\n")
            self.assertEqual(response.readline(), b"partial body\r\n")
            self.assertEqual(response.readline(), b"")
        self.assertEqual(sum(row.startswith(b"BODY ") for row in peer.commands), 1)
        self.assertEqual(self.diagnostics(bridge)["retries"], 0)

    def test_continuously_streaming_body_can_exceed_pre_response_budget(self):
        def slow_body(handler, serial, command):
            if command.startswith(b"BODY "):
                handler.wfile.write(b"222 article follows\r\n")
                handler.wfile.flush()
                for _ in range(4):
                    peer.stop.wait(0.08)
                    handler.wfile.write(b"continued\r\n")
                    handler.wfile.flush()
                handler.wfile.write(b".\r\n")
                handler.wfile.flush()
                return False

        peer = self.peer(slow_body)
        bridge = self.bridge(peer)
        with (
            patch("utils.infinidysk_v2_nntp.SOCKET_TIMEOUT", 0.15),
            client(bridge) as (stream, response),
        ):
            self.assertTrue(response.readline().startswith(b"200 "))
            stream.sendall(b"BODY <fixture>\r\n")
            self.assertEqual(response.readline(), b"222 article follows\r\n")
            for _ in range(4):
                self.assertEqual(response.readline(), b"continued\r\n")
            self.assertEqual(response.readline(), b".\r\n")
        self.assertEqual(self.diagnostics(bridge)["retries"], 0)

    def test_article_missing_and_authentication_failure_are_not_retried(self):
        def replies(handler, serial, command):
            if command.startswith(b"BODY "):
                handler.wfile.write(b"430 No such article\r\n")
                handler.wfile.flush()
                return False
            if command.startswith(b"AUTHINFO PASS "):
                handler.wfile.write(b"481 Authentication rejected\r\n")
                handler.wfile.flush()
                return False

        peer = self.peer(replies)
        bridge = self.bridge(peer)
        with client(bridge) as (stream, response):
            self.assertTrue(response.readline().startswith(b"200 "))
            stream.sendall(b"BODY <fixture>\r\n")
            self.assertEqual(response.readline(), b"430 No such article\r\n")
        with client(bridge) as (stream, response):
            self.assertTrue(response.readline().startswith(b"200 "))
            stream.sendall(b"AUTHINFO PASS fixture-password\r\n")
            self.assertEqual(response.readline(), b"481 Authentication rejected\r\n")
        row = self.diagnostics(bridge)
        self.assertEqual(row["retries"], 0)
        self.assertEqual(row["failures"], {"article_missing": 1, "authentication": 1})

    def test_genuine_upstream_400_is_preserved_and_not_retried(self):
        peer = self.peer(greeting=b"400 Service temporarily unavailable\r\n")
        bridge = self.bridge(peer)
        with client(bridge) as (_, response):
            self.assertEqual(
                response.readline(), b"400 Service temporarily unavailable\r\n"
            )
            self.assertEqual(response.readline(), b"")
        self.assertEqual(peer.connections, 1)
        self.assertEqual(self.diagnostics(bridge)["retries"], 0)
        self.assertEqual(self.diagnostics(bridge)["failures"], {"provider_reply": 1})

    def test_certificate_rejection_closes_without_retry_or_synthetic_400(self):
        peer = self.peer()
        bridge = self.bridge(peer)
        with patch.object(
            bridge, "_connect", side_effect=ssl.SSLCertVerificationError("fixture")
        ) as connect:
            with client(bridge) as (_, response):
                self.assertEqual(response.readline(), b"")
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(self.diagnostics(bridge)["failures"], {"tls": 1})

    def test_transient_tls_eof_before_greeting_is_retried_with_verified_connect(self):
        peer = self.peer()
        bridge = self.bridge(peer)
        original = bridge._connect
        calls = []

        def connect(selected, **kwargs):
            calls.append(selected)
            if len(calls) == 1:
                raise ssl.SSLEOFError("fixture TLS session closed")
            return original(selected, **kwargs)

        with patch.object(bridge, "_connect", connect):
            with client(bridge) as (stream, response):
                self.assertTrue(response.readline().startswith(b"200 "))
                self.body(stream, response)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(self.diagnostics(bridge)["retries"], 1)
        self.assertEqual(self.diagnostics(bridge)["failures"], {"connection_closed": 1})

    def test_virtual_session_budget_covers_configured_pool_above_32(self):
        peer = self.peer()
        bridge = self.bridge(peer, count=40, MaxConnections=1)
        relays = ContainedRelays(bridge.payload)
        self.addCleanup(relays.close)
        clones = json.loads(relays.start()[PROVIDER_ENV])["Providers"]
        self.assertEqual(bridge._virtual_limit, 40)
        self.assertEqual(relays._virtual_limit, 40)
        with ExitStack() as stack:
            streams = []
            for clone in clones:
                stream = stack.enter_context(
                    closing(socket.create_connection((clone["Host"], clone["Port"]), 2))
                )
                self.assertTrue(stream.recv(1024).startswith(b"200 "))
                streams.append(stream)
            streams[0].sendall(b"DATE\r\n")
            self.assertTrue(streams[0].recv(1024).startswith(b"111 "))
            self.assertLessEqual(bridge.stats["peak_connections"], 4)
        self.assertEqual(bridge.stats["provider_failures"], 0)

    def test_session_cleanup_fault_still_releases_physical_budget(self):
        peer = self.peer()
        bridge = self.bridge(peer)
        session = _ProviderSession(bridge, 0)
        session.greeting()
        response = session.response
        session.response = Mock()
        session.response.close.side_effect = OSError("fixture close fault")
        try:
            with self.assertRaises(OSError):
                session.close()
            self.assertEqual(bridge._leased_total, 0)
            self.assertEqual(bridge.stats["active_connections"], 0)
        finally:
            response.close()

    def test_worker_cleanup_fault_still_releases_client_and_worker_budget(self):
        peer = self.peer()
        bridge = self.bridge(peer)
        original = _ProviderSession.close
        raised = threading.Event()

        def fail_once(session):
            original(session)
            if not raised.is_set():
                raised.set()
                raise OSError("fixture cleanup failure")

        with patch.object(_ProviderSession, "close", fail_once):
            with client(bridge) as (_, response):
                self.assertTrue(response.readline().startswith(b"200 "))
            deadline = time.monotonic() + 2
            while bridge._threads and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(raised.is_set())
            self.assertFalse(bridge._threads)
            self.assertFalse(bridge._sockets)
            self.assertEqual(bridge._workers._value, bridge._virtual_limit)
            self.assertEqual(bridge._leased_total, 0)
            self.assertEqual(bridge.stats["active_connections"], 0)

    def test_retry_shares_one_pre_response_deadline(self):
        attempts = []

        def delay(handler, serial, command):
            if command.startswith(b"BODY "):
                attempts.append(serial)
                peer.stop.wait(0.15 if len(attempts) == 1 else 0.8)
                return False

        peer = self.peer(delay)
        bridge = self.bridge(peer)
        with (
            patch("utils.infinidysk_v2_nntp.SOCKET_TIMEOUT", 0.4),
            client(bridge) as (stream, response),
        ):
            self.assertTrue(response.readline().startswith(b"200 "))
            started = time.monotonic()
            stream.sendall(b"BODY <fixture>\r\n")
            self.assertEqual(response.readline(), b"")
            self.assertLess(time.monotonic() - started, 0.65)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.diagnostics(bridge)["retries"], 1)
        self.assertEqual(self.diagnostics(bridge)["failures"]["timeout"], 1)

    def test_tls_handshake_receives_only_remaining_pre_response_budget(self):
        peer = self.peer()
        bridge = self.bridge(peer)
        secured = {**bridge.providers[0], "UseSsl": True}
        clock = [0]
        raw, wrapped = Mock(), Mock()

        def connect(*args, **kwargs):
            self.assertEqual(kwargs["timeout"], 30)
            clock[0] = 25
            return raw

        context = Mock(minimum_version=ssl.TLSVersion.TLSv1_2)
        context.wrap_socket.return_value = wrapped
        with (
            patch("utils.infinidysk_v2_nntp.time.monotonic", lambda: clock[0]),
            patch("utils.infinidysk_v2_nntp.socket.create_connection", connect),
            patch(
                "utils.infinidysk_v2_nntp.ssl.create_default_context",
                return_value=context,
            ),
        ):
            self.assertIs(bridge._connect(secured, deadline=30), wrapped)
        raw.settimeout.assert_called_once_with(5)
        with bridge._lock:
            bridge._sockets.clear()


if __name__ == "__main__":
    unittest.main()
