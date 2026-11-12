"""Real loopback NNTP fixtures; no external provider traffic."""

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import socket
import socketserver
import shutil
import ssl
import subprocess
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from utils.infinidysk_v2_nntp import (
    ContainedRelays,
    NntpBridge,
    NntpBridgeError,
    PROVIDER_ENV,
    read_source_providers,
    restore_native_providers,
    validate_providers,
    _resolve_targets,
)


class FakeNntp:
    def __init__(self):
        self.commands = []
        self.clients = set()
        self.lock = threading.Lock()
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                with owner.lock:
                    owner.clients.add(self.request)
                try:
                    self.wfile.write(b"200 NNTP fixture ready\r\n")
                    self.wfile.flush()
                    while command := self.rfile.readline(8193):
                        with owner.lock:
                            owner.commands.append(command)
                        verb = command.split(b" ", 1)[0].strip().upper()
                        if verb == b"QUIT":
                            self.wfile.write(b"205 closing\r\n")
                            self.wfile.flush()
                            return
                        if verb == b"CAPABILITIES":
                            self.wfile.write(b"101 capabilities\r\nVERSION 2\r\n.\r\n")
                        elif verb == b"AUTHINFO":
                            self.wfile.write(
                                b"381 password required\r\n"
                                if b" USER " in command
                                else b"281 accepted\r\n"
                            )
                        elif verb in {b"BODY", b"ARTICLE", b"HEAD"}:
                            self.wfile.write(
                                b"222 0 <fixture>\r\nfixture body\r\n.\r\n"
                            )
                        else:
                            self.wfile.write(b"223 0 <fixture>\r\n")
                        self.wfile.flush()
                except OSError:
                    pass
                finally:
                    with owner.lock:
                        owner.clients.discard(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            block_on_close = False

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive()


def provider(port, **extra):
    return {
        "ProviderId": str(uuid.uuid4()),
        "Type": 1,
        "Host": "127.0.0.1",
        "Port": port,
        "UseSsl": False,
        "User": "fixture-user",
        "Pass": "fixture-password",
        "MaxConnections": 20,
        "PipeliningDepth": 8,
        "Nickname": "Fixture",
        **extra,
    }


@contextmanager
def client(bridge, index=0, token=None):
    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stream.settimeout(2)
    try:
        stream.connect(str(bridge.path))
        stream.sendall(
            json.dumps({"provider": index, "token": token or bridge.token}).encode()
            + b"\n"
        )
        with stream.makefile("rb") as reply:
            yield stream, reply
    finally:
        stream.close()


class NntpBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = FakeNntp()
        self.addCleanup(self.server.close)
        self.provider = provider(self.server.port)

    def bridge(self, providers=None):
        directory = self.root / ("bridge-" + uuid.uuid4().hex[:8])
        directory.mkdir(mode=0o700)
        bridge = NntpBridge(
            providers or [self.provider], directory, os.geteuid(), os.getegid()
        )
        self.addCleanup(bridge.close)
        return bridge.start()

    def test_real_broker_allows_only_read_commands_and_preserves_framing(self):
        bridge = self.bridge()
        before = bridge.last_activity
        with client(bridge) as (stream, response):
            self.assertTrue(response.readline().startswith(b"200 "))
            for command, code in (
                (b"AUTHINFO USER fixture-user\r\n", b"381"),
                (b"AUTHINFO PASS fixture-password\r\n", b"281"),
                (b"MODE READER\r\n", b"223"),
                (b"STAT <fixture>\r\n", b"223"),
            ):
                stream.sendall(command)
                self.assertTrue(response.readline().startswith(code))
            stream.sendall(b"BODY <fixture>\r\n")
            self.assertEqual(response.readline(), b"222 0 <fixture>\r\n")
            self.assertEqual(response.readline(), b"fixture body\r\n")
            self.assertEqual(response.readline(), b".\r\n")
            stream.sendall(b"CAPABILITIES\r\n")
            self.assertEqual(response.readline(), b"101 capabilities\r\n")
            self.assertEqual(response.readline(), b"VERSION 2\r\n")
            self.assertEqual(response.readline(), b".\r\n")
            stream.sendall(b"QUIT\r\n")
            self.assertEqual(response.readline(), b"205 closing\r\n")
        self.assertGreater(bridge.last_activity, before)
        self.assertEqual(bridge.stats["commands"], 7)
        self.assertGreater(bridge.stats["bytes_received"], 0)
        self.assertTrue(bridge.stats["read_only"])

    def test_mutating_commands_and_wrong_credentials_never_reach_provider(self):
        bridge = self.bridge()
        for command in (
            b"POST\r\n",
            b"IHAVE <fixture>\r\n",
            b"TAKETHIS <fixture>\r\n",
            b"STARTTLS\r\n",
            b"AUTHINFO PASS wrong-password\r\n",
            b"BODY 1\r\n",
            b"BODY <fixture> extra\r\n",
        ):
            with self.subTest(command=command):
                with client(bridge) as (stream, response):
                    self.assertTrue(response.readline().startswith(b"200 "))
                    stream.sendall(command)
                    self.assertTrue(response.readline().startswith(b"502 "))
        self.assertEqual(self.server.commands, [])
        self.assertEqual(bridge.stats["denied_commands"], 7)

    def test_private_handshake_cannot_choose_an_unbound_target(self):
        bridge = self.bridge()
        for index, token in ((0, "invalid"), (-1, bridge.token), (1, bridge.token)):
            with self.subTest(index=index, token_valid=token == bridge.token):
                with client(bridge, index=index, token=token) as (_, response):
                    self.assertTrue(response.readline().startswith(b"400 "))
        self.assertEqual(self.server.commands, [])

    def test_connection_budgets_remain_two_per_provider_four_total(self):
        bridge = self.bridge(
            [self.provider, provider(self.server.port), provider(self.server.port)]
        )
        held = []
        try:
            for index in (0, 0, 1, 1):
                stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                stream.settimeout(2)
                stream.connect(str(bridge.path))
                stream.sendall(
                    json.dumps({"provider": index, "token": bridge.token}).encode()
                    + b"\n"
                )
                self.assertTrue(stream.recv(1024).startswith(b"200 "))
                held.append(stream)
            self.assertEqual(bridge.stats["active_connections"], 4)
            self.assertEqual(bridge.stats["peak_connections"], 4)
            extra = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            held.append(extra)
            extra.settimeout(0.1)
            extra.connect(str(bridge.path))
            extra.sendall(
                json.dumps({"provider": 2, "token": bridge.token}).encode() + b"\n"
            )
            with self.assertRaises(socket.timeout):
                extra.recv(1024)
            self.assertEqual(bridge.stats["active_connections"], 4)
        finally:
            for stream in held:
                stream.close()
        bridge.close()
        self.assertEqual(bridge.stats["active_connections"], 0)
        self.assertFalse(bridge.path.exists())

    def test_contained_loopback_relays_use_only_broker_and_cap_native_import(self):
        bridge = self.bridge()
        relays = ContainedRelays(bridge.payload)
        self.addCleanup(relays.close)
        env = relays.start()
        clone = json.loads(env[PROVIDER_ENV])["Providers"][0]
        self.assertEqual(clone["Host"], "127.0.0.1")
        self.assertFalse(clone["UseSsl"])
        self.assertEqual(clone["MaxConnections"], 2)
        self.assertEqual(clone["PipeliningDepth"], 1)
        self.assertEqual(self.provider["MaxConnections"], 20)
        self.assertEqual(self.provider["PipeliningDepth"], 8)
        with closing(
            socket.create_connection((clone["Host"], clone["Port"]), timeout=2)
        ) as stream:
            self.assertTrue(stream.recv(1024).startswith(b"200 "))
            stream.sendall(b"STAT <fixture>\r\n")
            self.assertTrue(stream.recv(1024).startswith(b"223 "))
        relays.close()

    def test_disabled_provider_network_policies_are_preserved_without_connections(self):
        disabled = [
            {
                **self.provider,
                "ProviderId": str(uuid.uuid4()),
                "Type": 0,
                "UseSsl": True,
                "SkipTlsVerification": True,
            },
            {
                **self.provider,
                "ProviderId": str(uuid.uuid4()),
                "Type": 255,
                "Host": "0.0.0.0",
            },
            {
                **self.provider,
                "ProviderId": str(uuid.uuid4()),
                "Type": 0,
                "Host": "224.0.0.1",
            },
        ]
        self.assertEqual(validate_providers({"Providers": disabled}), disabled)
        bridge = self.bridge([self.provider, *disabled])
        relays = ContainedRelays(bridge.payload)
        self.addCleanup(relays.close)
        clones = json.loads(relays.start()[PROVIDER_ENV])["Providers"]
        self.assertEqual(clones[1:], disabled)
        with patch.object(bridge, "_connect", wraps=bridge._connect) as connect:
            for index in range(1, len(disabled) + 1):
                with client(bridge, index=index) as (_, response):
                    self.assertTrue(response.readline().startswith(b"400 "))
            connect.assert_not_called()
            with client(bridge) as (stream, response):
                self.assertTrue(response.readline().startswith(b"200 "))
                stream.sendall(b"STAT <fixture>\r\n")
                self.assertTrue(response.readline().startswith(b"223 "))
            self.assertEqual(connect.call_count, 1)
        self.assertEqual(self.server.commands, [b"STAT <fixture>\r\n"])
        for source in disabled:
            with self.subTest(active=source["Host"], tls=source["UseSsl"]):
                with self.assertRaises(NntpBridgeError):
                    validate_providers({"Providers": [{**source, "Type": 1}]})
        for changes in ({"Host": ""}, {"Port": 0}):
            with self.subTest(incomplete=changes):
                with self.assertRaises(NntpBridgeError):
                    validate_providers(
                        {"Providers": [{**self.provider, "Type": 0, **changes}]}
                    )

    def test_tls_uses_original_hostname_and_preserves_stricter_verified_policy(self):
        secured = {**self.provider, "UseSsl": True, "Host": "provider.example.invalid"}
        bridge = self.bridge([secured])
        for minimum in (
            ssl.TLSVersion.TLSv1,
            ssl.TLSVersion.TLSv1_1,
            ssl.TLSVersion.TLSv1_2,
            ssl.TLSVersion.TLSv1_3,
        ):
            with self.subTest(minimum=minimum.name):
                raw = Mock()
                wrapped = Mock()
                context = Mock(
                    minimum_version=minimum,
                    check_hostname=True,
                    verify_mode=ssl.CERT_REQUIRED,
                )
                context.wrap_socket.return_value = wrapped
                with (
                    patch(
                        "utils.infinidysk_v2_nntp.socket.create_connection",
                        return_value=raw,
                    ),
                    patch(
                        "utils.infinidysk_v2_nntp._resolve_targets",
                        return_value=("127.0.0.1",),
                    ),
                    patch(
                        "utils.infinidysk_v2_nntp.ssl.create_default_context",
                        return_value=context,
                    ),
                ):
                    self.assertIs(bridge._connect(secured), wrapped)
                self.assertEqual(
                    context.minimum_version, max(minimum, ssl.TLSVersion.TLSv1_2)
                )
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                context.wrap_socket.assert_called_once_with(
                    raw, server_hostname="provider.example.invalid"
                )
                with bridge._lock:
                    bridge._sockets.clear()

    def test_idle_primary_connections_release_budget_for_third_provider(self):
        bridge = self.bridge(
            [self.provider, provider(self.server.port), provider(self.server.port)]
        )
        held = []
        try:
            with patch("utils.infinidysk_v2_nntp.CLIENT_IDLE_TIMEOUT", 0.5):
                for index in (0, 0, 1, 1):
                    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    stream.settimeout(2)
                    stream.connect(str(bridge.path))
                    stream.sendall(
                        json.dumps({"provider": index, "token": bridge.token}).encode()
                        + b"\n"
                    )
                    self.assertTrue(stream.recv(1024).startswith(b"200 "))
                    held.append(stream)
                started = time.monotonic()
                with client(bridge, index=2) as (stream, response):
                    self.assertTrue(response.readline().startswith(b"200 "))
                    self.assertLess(time.monotonic() - started, 2)
                    stream.sendall(b"STAT <fixture>\r\n")
                    self.assertTrue(response.readline().startswith(b"223 "))
                self.assertLessEqual(bridge.stats["peak_connections"], 4)
                self.assertEqual(bridge.stats["provider_failures"], 0)
        finally:
            for stream in held:
                stream.close()

    def test_lower_source_connection_limit_is_enforced_by_broker(self):
        bridge = self.bridge([{**self.provider, "MaxConnections": 1}])
        with client(bridge) as (_, response):
            self.assertTrue(response.readline().startswith(b"200 "))
            with client(bridge) as (waiting, _):
                waiting.settimeout(0.1)
                with self.assertRaises(socket.timeout):
                    waiting.recv(1024)
                self.assertEqual(bridge.stats["active_connections"], 1)
        self.assertLessEqual(bridge.stats["peak_connections"], 1)

    def test_occupied_socket_path_is_never_removed_on_start_failure(self):
        for kind in ("file", "symlink", "socket"):
            with self.subTest(kind=kind):
                directory = self.root / ("occupied-" + kind)
                directory.mkdir(mode=0o700)
                path = directory / "nntp.sock"
                owned_socket = None
                if kind == "file":
                    path.write_bytes(b"foreign")
                elif kind == "symlink":
                    path.symlink_to(self.root / "outside")
                else:
                    owned_socket = socket.socket(socket.AF_UNIX)
                    owned_socket.bind(str(path))
                before = path.lstat()
                bridge = NntpBridge(
                    [self.provider], directory, os.geteuid(), os.getegid()
                )
                try:
                    with self.assertRaises(OSError):
                        bridge.start()
                    bridge.close()
                    after = path.lstat()
                    self.assertEqual(
                        (before.st_dev, before.st_ino), (after.st_dev, after.st_ino)
                    )
                finally:
                    if owned_socket is not None:
                        owned_socket.close()

    def test_normal_client_eof_does_not_report_provider_failure(self):
        bridge = self.bridge()
        with client(bridge) as (_, response):
            self.assertTrue(response.readline().startswith(b"200 "))
        deadline = time.monotonic() + 2
        while bridge.stats["active_connections"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(bridge.stats["provider_failures"], 0)

    def test_broker_shutdown_during_connection_arrivals_joins_every_worker(self):
        bridge = self.bridge()
        stop = threading.Event()

        def arrivals():
            while not stop.is_set():
                stream = socket.socket(socket.AF_UNIX)
                stream.settimeout(0.05)
                try:
                    stream.connect(str(bridge.path))
                    stream.sendall(
                        json.dumps({"provider": 0, "token": bridge.token}).encode()
                        + b"\n"
                    )
                    stream.recv(1024)
                except OSError:
                    pass
                finally:
                    stream.close()

        pump = threading.Thread(target=arrivals, daemon=True)
        pump.start()
        try:
            deadline = time.monotonic() + 2
            while not bridge.stats["peak_connections"] and time.monotonic() < deadline:
                time.sleep(0.01)
            bridge.close()
        finally:
            stop.set()
            pump.join(timeout=2)
        self.assertFalse(pump.is_alive())
        self.assertFalse(bridge._listener.is_alive())
        self.assertFalse(any(thread.is_alive() for thread in bridge._threads))
        self.assertEqual(bridge.stats["active_connections"], 0)
        self.assertFalse(bridge.path.exists())

    def test_contained_relay_shutdown_during_arrivals_joins_workers(self):
        bridge = self.bridge()
        relays = ContainedRelays(bridge.payload)
        clone = json.loads(relays.start()[PROVIDER_ENV])["Providers"][0]
        stop = threading.Event()

        def arrivals():
            while not stop.is_set():
                try:
                    with closing(
                        socket.create_connection(
                            (clone["Host"], clone["Port"]), timeout=0.05
                        )
                    ) as stream:
                        stream.recv(1024)
                except OSError:
                    pass

        pump = threading.Thread(target=arrivals, daemon=True)
        pump.start()
        try:
            deadline = time.monotonic() + 2
            while not bridge.stats["peak_connections"] and time.monotonic() < deadline:
                time.sleep(0.01)
            relays.close()
        finally:
            stop.set()
            pump.join(timeout=2)
            relays.close()
        self.assertFalse(pump.is_alive())
        self.assertFalse(any(thread.is_alive() for thread in relays._threads))

    def test_dns_resolution_is_bounded_and_pins_only_valid_addresses(self):
        with patch(
            "utils.infinidysk_v2_nntp.subprocess.run",
            return_value=Mock(returncode=0, stdout='["127.0.0.1"]'),
        ) as resolve:
            self.assertEqual(
                _resolve_targets("provider.example.invalid", 563), ("127.0.0.1",)
            )
        self.assertEqual(resolve.call_args.kwargs["timeout"], 8)
        with patch(
            "utils.infinidysk_v2_nntp.subprocess.run",
            return_value=Mock(returncode=0, stdout='["0.0.0.0"]'),
        ):
            with self.assertRaises(NntpBridgeError):
                _resolve_targets("provider.example.invalid", 563)

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL CLI is unavailable")
    def test_real_provider_tls_rejects_untrusted_certificate(self):
        certificate, key = self.root / "cert.pem", self.root / "key.pem"
        result = subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(certificate),
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2)

        def tls_peer():
            try:
                connection, _ = listener.accept()
                connection.settimeout(2)
                with connection:
                    with context.wrap_socket(connection, server_side=True):
                        pass
            except (OSError, ssl.SSLError):
                pass

        peer = threading.Thread(target=tls_peer, daemon=True)
        peer.start()
        secured = {
            **self.provider,
            "Host": "localhost",
            "Port": listener.getsockname()[1],
            "UseSsl": True,
        }
        bridge = self.bridge([secured])
        try:
            with self.assertRaises(ssl.SSLCertVerificationError):
                bridge._connect(secured)
        finally:
            listener.close()
            peer.join(timeout=3)
        self.assertFalse(peer.is_alive())
        with bridge._lock:
            self.assertFalse(bridge._sockets)

    def test_unsafe_source_provider_settings_are_refused(self):
        for changes in (
            {"UseSsl": True, "SkipTlsVerification": True},
            {"Host": "0.0.0.0"},
            {"Host": "https://example.invalid"},
            {"Port": 0},
            {"Pass": "private\r\nPOST"},
            {"MaxConnections": True},
            {"MaxConnections": 0},
            {"MaxConnections": -1},
        ):
            with self.subTest(
                changes={key: value for key, value in changes.items() if key != "Pass"}
            ):
                with self.assertRaises(NntpBridgeError):
                    validate_providers({"Providers": [{**self.provider, **changes}]})

    def test_source_provider_read_is_read_only_and_honors_legacy_override(self):
        database = self.root / "source.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE ConfigItems(ConfigName TEXT,ConfigValue TEXT)"
            )
            connection.execute(
                "INSERT INTO ConfigItems VALUES(?,?)",
                ("usenet.providers", json.dumps({"Providers": [self.provider]})),
            )
        before = database.read_bytes()
        self.assertEqual(
            read_source_providers(database)[0]["ProviderId"],
            self.provider["ProviderId"],
        )
        overridden = provider(self.server.port, MaxConnections=1)
        selected = read_source_providers(
            database, {PROVIDER_ENV: json.dumps({"Providers": [overridden]})}
        )
        self.assertEqual(selected[0]["MaxConnections"], 1)
        self.assertEqual(database.read_bytes(), before)

    def test_restore_native_provider_semantics_after_verified_shutdown(self):
        original = provider(
            563,
            Host="news.example.invalid",
            UseSsl=True,
            StorageGroup="fixture-backbone",
            MaxConnections=30,
            PipeliningDepth=7,
        )
        alternate = provider(
            self.server.port,
            Host="custom.example.invalid",
            StorageGroup="fixture-backbone",
        )
        originals = validate_providers({"Providers": [original, alternate]})
        database = self.root / "native.sqlite"
        stored = [
            {
                "id": p["ProviderId"],
                "name": "relay",
                "hostname": "127.0.0.1",
                "port": 1234,
                "tls": False,
                "username": p["User"],
                "password": p["Pass"],
                "max_connections": 2,
                "pipeline_depth": 1,
                "profile": None,
                "backbone": None,
                "priority": 2,
                "quota": {"gb": 100, "period": "block", "reset_day": 1},
                "enabled": True,
            }
            for p in originals
        ]
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT)")
            connection.execute(
                "INSERT INTO config VALUES('providers',?)", (json.dumps(stored),)
            )
        catalog = {
            "profiles": [
                {
                    "id": "fixture",
                    "servers": [
                        {
                            "hostname": "news.example.invalid",
                            "port": 563,
                            "backbone": "fixture-backbone",
                        }
                    ],
                }
            ]
        }
        proof = restore_native_providers(database, originals, catalog)
        self.assertTrue(proof["verified"])
        with sqlite3.connect(database) as connection:
            result = json.loads(
                connection.execute(
                    "SELECT value FROM config WHERE key='providers'"
                ).fetchone()[0]
            )
        self.assertEqual(result[0]["hostname"], original["Host"])
        self.assertTrue(result[0]["tls"])
        self.assertEqual(result[0]["profile"], "fixture")
        self.assertEqual(result[0]["max_connections"], 30)
        self.assertEqual(result[0]["pipeline_depth"], 7)
        self.assertEqual(result[1]["backbone"], "fixture-backbone")
        self.assertEqual(result[0]["quota"], stored[0]["quota"])
        self.assertEqual(result[0]["priority"], 2)
        bridge = NntpBridge(originals, self.root, os.geteuid(), os.getegid())
        with self.assertRaisesRegex(NntpBridgeError, "Stop"):
            bridge.restore_native_providers(database, catalog)
        self.assertNotIn(original["Pass"], str(proof))

    def test_restore_refuses_changed_provider_identity_without_mutation(self):
        database = self.root / "native.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT)")
            connection.execute("INSERT INTO config VALUES('providers','[]')")
        before = database.read_bytes()
        with self.assertRaises(NntpBridgeError):
            restore_native_providers(database, [self.provider], {"profiles": []})
        self.assertEqual(database.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
