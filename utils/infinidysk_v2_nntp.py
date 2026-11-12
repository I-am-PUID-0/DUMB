"""Bounded, read-only NNTP access for a contained native migration.

The importer keeps its private network namespace. Its loopback relays can ask
this controller-owned Unix broker to contact only captured provider endpoints.
Credentials and addresses never enter public diagnostics.
"""

from contextlib import closing
import copy
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import select
import signal
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import quote
import uuid

MAX_PROVIDERS = 64
MAX_CONFIG_BYTES = 1024 * 1024
MAX_COMMAND_BYTES = 8192
MAX_BODY_LINE = 1024 * 1024
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
MAX_CONNECTIONS_PER_PROVIDER = 2
MAX_TOTAL_CONNECTIONS = 4
MAX_WORKERS = 32
SOCKET_TIMEOUT = 30
CLIENT_IDLE_TIMEOUT = 3
PROVIDER_ENV = "NZBDAV_CONFIG__USENET__PROVIDERS"


class NntpBridgeError(ValueError):
    """A captured-provider bridge could not safely complete its operation."""


def _text(value, label, *, empty=True):
    if (
        not isinstance(value, str)
        or len(value.encode()) > 4096
        or any(ord(character) < 32 for character in value)
        or (not empty and not value)
    ):
        raise NntpBridgeError(f"A captured provider {label} is invalid.")
    return value


def validate_providers(raw):
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("Providers"), list)
        or len(raw["Providers"]) > MAX_PROVIDERS
    ):
        raise NntpBridgeError("Captured Usenet provider configuration is invalid.")
    providers = copy.deepcopy(raw["Providers"])
    ids = set()
    for provider in providers:
        if not isinstance(provider, dict):
            raise NntpBridgeError("A captured Usenet provider is invalid.")
        for key in ("Type", "Port", "MaxConnections"):
            if type(provider.get(key)) is not int:
                raise NntpBridgeError("Captured provider numeric settings are invalid.")
        if not 0 <= provider["Type"] <= 255:
            raise NntpBridgeError("A captured provider type is invalid.")
        if (
            provider["Type"] in {1, 2, 3}
            and not 1 <= provider["MaxConnections"] <= 65535
        ):
            raise NntpBridgeError("An enabled provider connection limit is invalid.")
        if not 1 <= provider["Port"] <= 65535:
            raise NntpBridgeError("A captured provider port is invalid.")
        _text(provider.get("Host"), "hostname", empty=False)
        if any(character.isspace() for character in provider["Host"]) or any(
            character in provider["Host"] for character in "/\\@"
        ):
            raise NntpBridgeError("A captured provider hostname is invalid.")
        try:
            address = ipaddress.ip_address(provider["Host"].strip("[]"))
        except ValueError:
            address = None
        if (
            provider["Type"] in {1, 2, 3}
            and address
            and (address.is_unspecified or address.is_multicast)
        ):
            raise NntpBridgeError("A captured provider address is invalid.")
        if type(provider.get("UseSsl")) is not bool:
            raise NntpBridgeError("Captured provider TLS policy is invalid.")
        if (
            provider["Type"] in {1, 2, 3}
            and provider["UseSsl"]
            and provider.get("SkipTlsVerification") is True
        ):
            raise NntpBridgeError(
                "Native migration requires verified provider TLS; an insecure legacy provider policy is unsupported."
            )
        provider["User"] = _text(provider.get("User", ""), "username")
        provider["Pass"] = _text(provider.get("Pass", ""), "password")
        for key in ("Nickname", "StorageGroup"):
            if provider.get(key) is not None:
                _text(provider[key], key)
        depth = provider.get("PipeliningDepth")
        if depth is not None and type(depth) is not int:
            raise NntpBridgeError("Captured provider pipeline depth is invalid.")
        try:
            identifier = uuid.UUID(str(provider.get("ProviderId") or uuid.UUID(int=0)))
        except (ValueError, TypeError, AttributeError) as error:
            raise NntpBridgeError("A captured provider identity is invalid.") from error
        if identifier.int == 0:
            identifier = uuid.uuid4()
        if identifier in ids:
            raise NntpBridgeError("Captured provider identities are ambiguous.")
        ids.add(identifier)
        provider["ProviderId"] = str(identifier)
    return providers


def read_source_providers(database, environment=None):
    """Read captured C# settings, honoring its exact supported environment key."""
    override = (environment or {}).get(PROVIDER_ENV)
    if override is None:
        path = Path(database)
        value = path.lstat()
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise NntpBridgeError(
                "Captured provider settings require a direct source database."
            )
        with closing(
            sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
        ) as connection:
            rows = connection.execute(
                "SELECT ConfigValue FROM ConfigItems WHERE ConfigName='usenet.providers'"
            ).fetchall()
        if len(rows) > 1:
            raise NntpBridgeError("Captured provider settings are ambiguous.")
        override = rows[0][0] if rows else '{"Providers":[]}'
    if not isinstance(override, str) or len(override.encode()) > MAX_CONFIG_BYTES:
        raise NntpBridgeError("Captured provider settings exceed their supported size.")
    try:
        raw = json.loads(override)
    except (ValueError, TypeError) as error:
        raise NntpBridgeError("Captured provider settings are unreadable.") from error
    return validate_providers(raw)


def _resolve_targets(host, port):
    """Resolve only the captured host, once, with a killable DNS deadline."""
    try:
        direct = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        direct = None
    if direct is not None:
        addresses = [str(direct)]
    else:
        program = (
            "import json,socket,sys;"
            "print(json.dumps(sorted({row[4][0] for row in socket.getaddrinfo("
            "sys.argv[1],int(sys.argv[2]),type=socket.SOCK_STREAM)})))"
        )
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", program, host, str(port)],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            if result.returncode or len(result.stdout) > 8192:
                raise NntpBridgeError(
                    "The captured NNTP hostname could not be resolved."
                )
            addresses = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise NntpBridgeError(
                "The captured NNTP hostname could not be resolved."
            ) from error
    if not isinstance(addresses, list) or not 1 <= len(addresses) <= 16:
        raise NntpBridgeError("The captured NNTP address set is invalid.")
    for address in addresses:
        try:
            value = ipaddress.ip_address(address)
        except (ValueError, TypeError) as error:
            raise NntpBridgeError(
                "The captured NNTP address set is invalid."
            ) from error
        if value.is_unspecified or value.is_multicast:
            raise NntpBridgeError("The captured NNTP address set is invalid.")
    return tuple(addresses)


class _ClientDisconnected(Exception):
    pass


def _send_client(client, data):
    try:
        client.sendall(data)
    except OSError as error:
        raise _ClientDisconnected from error


def _line(handle, limit=MAX_COMMAND_BYTES, *, eof=False):
    value = handle.readline(limit + 1)
    if not value and eof:
        return None
    if not value or len(value) > limit or not value.endswith(b"\r\n"):
        raise NntpBridgeError("An NNTP protocol line is incomplete or too large.")
    return value


def _allowed_command(line, provider):
    command, separator, argument = line[:-2].partition(b" ")
    command = command.upper()
    if command in {b"BODY", b"HEAD", b"STAT", b"ARTICLE"}:
        return bool(
            separator and re.fullmatch(rb"<[^<>\s\x00-\x1f\x7f]{1,2000}>", argument)
        )
    if command in {b"CAPABILITIES", b"QUIT"}:
        return not separator
    if command == b"MODE":
        return argument.upper() == b"READER"
    if command == b"AUTHINFO":
        kind, separator, credential = argument.partition(b" ")
        if not separator or kind.upper() not in {b"USER", b"PASS"}:
            return False
        expected = provider["User" if kind.upper() == b"USER" else "Pass"].encode()
        return hmac.compare_digest(credential, expected)
    return False


class NntpBridge:
    """Own a bounded broker; start/close it with the contained native process."""

    def __init__(self, providers, directory, uid, gid):
        self.providers = validate_providers({"Providers": providers})
        self.directory = Path(directory)
        self.uid, self.gid = int(uid), int(gid)
        self.path = self.directory / "nntp.sock"
        if len(os.fsencode(self.path)) > 103:
            raise NntpBridgeError(
                "Migration working paths are too long for the private NNTP bridge."
            )
        self.token = secrets.token_hex(32)
        self._server = None
        self._listener = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads = set()
        self._sockets = set()
        self._workers = threading.BoundedSemaphore(MAX_WORKERS)
        self._total = threading.BoundedSemaphore(MAX_TOTAL_CONNECTIONS)
        self._per_provider = [
            threading.BoundedSemaphore(
                min(MAX_CONNECTIONS_PER_PROVIDER, max(1, provider["MaxConnections"]))
            )
            for provider in self.providers
        ]
        self._resolved = {}
        self._activity = time.monotonic()
        self._counts = {
            "commands": 0,
            "bytes_received": 0,
            "denied_commands": 0,
            "provider_failures": 0,
            "active_connections": 0,
            "peak_connections": 0,
        }
        self._socket_identity = None
        self._directory_fd = None

    @property
    def last_activity(self):
        with self._lock:
            return self._activity

    @property
    def stats(self):
        with self._lock:
            return {
                **self._counts,
                "read_only": True,
                "max_connections_per_provider": MAX_CONNECTIONS_PER_PROVIDER,
                "max_total_connections": MAX_TOTAL_CONNECTIONS,
            }

    @property
    def payload(self):
        if self._server is None:
            raise NntpBridgeError("The private NNTP bridge has not started.")
        return {
            "socket": str(self.path),
            "token": self.token,
            "providers": copy.deepcopy(self.providers),
        }

    def _record(self, key, count=1, *, activity=False):
        with self._lock:
            self._counts[key] += count
            if activity:
                self._activity = time.monotonic()

    def _track(self, stream):
        with self._lock:
            if self._stop.is_set():
                stream.close()
                raise NntpBridgeError("The private NNTP bridge is stopping.")
            self._sockets.add(stream)
        return stream

    def start(self):
        if self._server is not None:
            raise NntpBridgeError("The private NNTP bridge already started.")
        info = self.directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or self.directory.is_symlink()
            or info.st_mode & 0o077
        ):
            raise NntpBridgeError(
                "The NNTP bridge requires its private local working directory."
            )
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._directory_fd = os.open(
                self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            opened = os.fstat(self._directory_fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise NntpBridgeError("The private NNTP directory changed.")
            server.bind(f"/proc/self/fd/{self._directory_fd}/nntp.sock")
            value = os.stat(
                "nntp.sock", dir_fd=self._directory_fd, follow_symlinks=False
            )
            self._socket_identity = (value.st_dev, value.st_ino)
            os.chmod(
                "nntp.sock", 0o600, dir_fd=self._directory_fd, follow_symlinks=False
            )
            if os.geteuid() == 0:
                os.chown(
                    "nntp.sock",
                    self.uid,
                    self.gid,
                    dir_fd=self._directory_fd,
                    follow_symlinks=False,
                )
            server.listen(MAX_WORKERS)
            server.settimeout(0.2)
            self._server = server
            self._listener = threading.Thread(
                target=self._accept, name="dumb-nntp-broker", daemon=True
            )
            self._listener.start()
            return self
        except BaseException:
            server.close()
            self._remove_socket()
            raise

    def _remove_socket(self):
        if self._directory_fd is None:
            return
        try:
            if self._socket_identity is not None:
                try:
                    value = os.stat(
                        "nntp.sock", dir_fd=self._directory_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    value = None
                if value is not None:
                    if (
                        value.st_dev,
                        value.st_ino,
                    ) != self._socket_identity or not stat.S_ISSOCK(value.st_mode):
                        raise NntpBridgeError(
                            "The private NNTP socket changed; it was retained."
                        )
                    os.unlink("nntp.sock", dir_fd=self._directory_fd)
        finally:
            os.close(self._directory_fd)
            self._directory_fd = None

    def __enter__(self):
        return self.start()

    def _accept(self):
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if self._stop.is_set():
                client.close()
                return
            if not self._workers.acquire(blocking=False):
                client.close()
                continue
            client.settimeout(SOCKET_TIMEOUT)
            try:
                self._track(client)
            except NntpBridgeError:
                self._workers.release()
                return
            thread = threading.Thread(
                target=self._serve, args=(client,), name="dumb-nntp-read", daemon=True
            )
            with self._lock:
                self._threads.add(thread)
                thread.start()

    def _connect(self, provider):
        # The client cannot name another target: only the captured provider
        # selected by its authenticated ordinal reaches this code.
        key = provider["ProviderId"]
        with self._lock:
            addresses = self._resolved.get(key)
        if addresses is None:
            addresses = _resolve_targets(provider["Host"], provider["Port"])
            with self._lock:
                self._resolved[key] = addresses
        stream = None
        deadline = time.monotonic() + SOCKET_TIMEOUT
        for address in addresses:
            try:
                stream = self._track(
                    socket.create_connection(
                        (address, provider["Port"]),
                        timeout=max(0.1, deadline - time.monotonic()),
                    )
                )
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
        if stream is None:
            raise NntpBridgeError("The captured NNTP endpoint could not be reached.")
        if provider["UseSsl"]:
            context = ssl.create_default_context()
            try:
                wrapped = context.wrap_socket(stream, server_hostname=provider["Host"])
            except BaseException:
                with self._lock:
                    self._sockets.discard(stream)
                stream.close()
                raise
            with self._lock:
                self._sockets.discard(stream)
            stream = self._track(wrapped)
        return stream

    def _serve(self, client):
        upstream = None
        held_provider = None
        held_total = False
        counted = False
        try:
            with client.makefile("rb") as request:
                handshake = request.readline(1025)
                if len(handshake) > 1024 or not handshake.endswith(b"\n"):
                    raise NntpBridgeError("The private NNTP handshake is invalid.")
                try:
                    hello = json.loads(handshake)
                except (ValueError, UnicodeError) as error:
                    raise NntpBridgeError(
                        "The private NNTP handshake is invalid."
                    ) from error
                index = hello.get("provider") if isinstance(hello, dict) else None
                if (
                    type(index) is not int
                    or not 0 <= index < len(self.providers)
                    or not isinstance(hello.get("token"), str)
                    or not hmac.compare_digest(hello["token"], self.token)
                    or self.providers[index]["Type"] not in {1, 2, 3}
                ):
                    raise NntpBridgeError(
                        "The private NNTP provider binding is invalid."
                    )
                provider = self.providers[index]
                if not self._per_provider[index].acquire(timeout=SOCKET_TIMEOUT):
                    raise NntpBridgeError("The private NNTP provider budget is busy.")
                held_provider = self._per_provider[index]
                if not self._total.acquire(timeout=SOCKET_TIMEOUT):
                    raise NntpBridgeError("The private NNTP connection budget is busy.")
                held_total = True
                if self._stop.is_set():
                    return
                upstream = self._connect(provider)
                with self._lock:
                    self._counts["active_connections"] += 1
                    self._counts["peak_connections"] = max(
                        self._counts["peak_connections"],
                        self._counts["active_connections"],
                    )
                counted = True
                with upstream.makefile("rb") as response:
                    greeting = _line(response)
                    _send_client(client, greeting)
                    self._record("bytes_received", len(greeting), activity=True)
                    while not self._stop.is_set():
                        # Reap pooled idle connections before another provider
                        # exhausts its admission timeout. Active reads keep the
                        # longer I/O deadline.
                        client.settimeout(CLIENT_IDLE_TIMEOUT)
                        try:
                            command = _line(request, eof=True)
                        except (socket.timeout, ConnectionResetError):
                            return
                        if command is None:
                            return
                        client.settimeout(SOCKET_TIMEOUT)
                        if not _allowed_command(command, provider):
                            self._record("denied_commands")
                            _send_client(
                                client,
                                b"502 Migration bridge permits read-only NNTP commands only\r\n",
                            )
                            return
                        self._record("commands", activity=True)
                        upstream.sendall(command)
                        reply = _line(response)
                        if not re.match(rb"^[1-5][0-9]{2}(?: |\r)", reply):
                            raise NntpBridgeError(
                                "The NNTP provider response is invalid."
                            )
                        _send_client(client, reply)
                        self._record("bytes_received", len(reply), activity=True)
                        if reply[:3] in {
                            b"100",
                            b"101",
                            b"215",
                            b"220",
                            b"221",
                            b"222",
                        }:
                            size = 0
                            while True:
                                row = _line(response, MAX_BODY_LINE)
                                size += len(row)
                                if size > MAX_RESPONSE_BYTES:
                                    raise NntpBridgeError(
                                        "An NNTP read exceeds its byte budget."
                                    )
                                _send_client(client, row)
                                self._record("bytes_received", len(row), activity=True)
                                if row == b".\r\n":
                                    break
                        if command[:-2].upper() == b"QUIT":
                            return
        except _ClientDisconnected:
            pass
        except (OSError, ValueError, ssl.SSLError):
            if not self._stop.is_set():
                self._record("provider_failures")
                try:
                    client.sendall(b"400 Migration provider bridge is unavailable\r\n")
                except OSError:
                    pass
        finally:
            if counted:
                self._record("active_connections", -1)
            for stream in (client, upstream):
                if stream is not None:
                    with self._lock:
                        self._sockets.discard(stream)
                    stream.close()
            if held_total:
                self._total.release()
            if held_provider is not None:
                held_provider.release()
            self._workers.release()
            with self._lock:
                self._threads.discard(threading.current_thread())

    def close(self):
        self._stop.set()
        if self._server is not None:
            self._server.close()
        # Acceptors must finish before the authoritative worker/socket snapshot.
        if self._listener is not None:
            self._listener.join(timeout=2)
            if self._listener.is_alive():
                raise NntpBridgeError("The private NNTP acceptor did not stop.")
        with self._lock:
            streams = list(self._sockets)
            threads = list(self._threads)
        for stream in streams:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            stream.close()
        deadline = time.monotonic() + SOCKET_TIMEOUT + 2
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            raise NntpBridgeError(
                "Private NNTP workers did not stop; preserve their working directory."
            )
        self._remove_socket()
        self._server = None

    def __exit__(self, *unused):
        self.close()

    def restore_native_providers(self, database, catalog, *, native_stopped=False):
        """Restore captured provider semantics using this verified native catalog."""
        if native_stopped is not True:
            raise NntpBridgeError(
                "Stop and verify the native process group before provider restoration."
            )
        return restore_native_providers(database, self.providers, catalog)


def _catalog_servers(catalog):
    if not isinstance(catalog, dict) or not isinstance(catalog.get("profiles"), list):
        raise NntpBridgeError("The verified native provider catalog is unavailable.")
    servers = {}
    for profile in catalog["profiles"]:
        if not isinstance(profile, dict) or not isinstance(
            profile.get("servers"), list
        ):
            raise NntpBridgeError("The native provider catalog shape is unsupported.")
        for server in profile["servers"]:
            if not isinstance(server, dict) or not all(
                key in server for key in ("hostname", "port", "backbone")
            ):
                raise NntpBridgeError("A native provider catalog server is invalid.")
            servers.setdefault(server["hostname"].casefold(), (profile["id"], server))
    return servers


def restore_native_providers(database, providers, catalog):
    servers = _catalog_servers(catalog)
    groups = {}
    for provider in providers:
        server = servers.get(provider["Host"].casefold())
        group = (provider.get("StorageGroup") or "").strip().lower()
        if group and server:
            groups[group] = server[1]["backbone"]
    expected = {provider["ProviderId"]: provider for provider in providers}
    path = Path(database)
    value = path.lstat()
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_nlink != 1
        or any(parent.is_symlink() for parent in path.parents)
    ):
        raise NntpBridgeError(
            "The native provider database is not a direct owned file."
        )
    with closing(sqlite3.connect(path, timeout=5)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT value FROM config WHERE key='providers'"
        ).fetchall()
        if not providers and not rows:
            return {
                "verified": True,
                "provider_count": 0,
                "sha256": hashlib.sha256(b"[]").hexdigest(),
            }
        if len(rows) != 1 or len(rows[0][0].encode()) > MAX_CONFIG_BYTES:
            raise NntpBridgeError(
                "Native imported provider settings are missing or oversized."
            )
        try:
            stored = json.loads(rows[0][0])
        except (ValueError, TypeError) as error:
            raise NntpBridgeError(
                "Native imported provider settings are unreadable."
            ) from error
        if (
            not isinstance(stored, list)
            or len(stored) != len(providers)
            or any(not isinstance(item, dict) for item in stored)
            or len({item.get("id") for item in stored}) != len(stored)
            or {item.get("id") for item in stored} != set(expected)
        ):
            raise NntpBridgeError(
                "Native provider identities do not match the captured source."
            )
        for item in stored:
            source = expected[item["id"]]
            if (
                item.get("username") != source["User"]
                or item.get("password") != source["Pass"]
            ):
                raise NntpBridgeError(
                    "Native provider authentication differs from the captured source."
                )
            match = servers.get(source["Host"].casefold())
            profile, backbone = None, None
            hostname, port = source["Host"], source["Port"]
            if match:
                profile_id, server = match
                backbone = server["backbone"]
                if server["port"] == port:
                    profile = profile_id
                    hostname = server["hostname"]
            else:
                backbone = groups.get(
                    (source.get("StorageGroup") or "").strip().lower()
                )
            item.update(
                name=(
                    source.get("Nickname")
                    if (source.get("Nickname") or "").strip()
                    else source["Host"]
                ),
                profile=profile,
                hostname=hostname,
                port=port,
                tls=source["UseSsl"],
                max_connections=min(65535, max(1, source["MaxConnections"])),
                pipeline_depth=min(
                    16,
                    max(
                        1,
                        (
                            source.get("PipeliningDepth")
                            if source.get("PipeliningDepth") is not None
                            else 4
                        ),
                    ),
                ),
                backbone=backbone,
            )
        encoded = json.dumps(stored, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "UPDATE config SET value=? WHERE key='providers'", (encoded,)
        )
        connection.commit()
        if (
            connection.execute(
                "SELECT value FROM config WHERE key='providers'"
            ).fetchone()[0]
            != encoded
        ):
            raise NntpBridgeError("Native provider restoration did not verify.")
    return {
        "verified": True,
        "provider_count": len(stored),
        "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
    }


class ContainedRelays:
    """Loopback-only endpoints inside the already-confined namespace."""

    def __init__(self, payload):
        self.payload = payload
        self._stop = threading.Event()
        self._servers = []
        self._threads = []
        self._accept_threads = []
        self._clients = set()
        self._lock = threading.Lock()
        self._workers = threading.BoundedSemaphore(MAX_WORKERS)

    def start(self):
        providers = copy.deepcopy(self.payload["providers"])
        for index, provider in enumerate(providers):
            if provider["Type"] not in {1, 2, 3}:
                continue
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.bind(("127.0.0.1", 0))
            server.listen(MAX_WORKERS)
            server.settimeout(0.2)
            self._servers.append(server)
            provider.update(
                Host="127.0.0.1",
                Port=server.getsockname()[1],
                UseSsl=False,
                SkipTlsVerification=False,
                MaxConnections=min(2, max(1, provider["MaxConnections"])),
                PipeliningDepth=1,
            )
            thread = threading.Thread(
                target=self._accept,
                args=(server, index),
                name="dumb-nntp-loopback",
                daemon=True,
            )
            with self._lock:
                self._threads.append(thread)
                self._accept_threads.append(thread)
                thread.start()
        return {
            PROVIDER_ENV: json.dumps({"Providers": providers}, separators=(",", ":"))
        }

    def _accept(self, server, index):
        while not self._stop.is_set():
            try:
                client, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._workers.acquire(blocking=False):
                client.close()
                continue
            thread = threading.Thread(
                target=self._relay,
                args=(client, index),
                name="dumb-nntp-pipe",
                daemon=True,
            )
            with self._lock:
                self._threads.append(thread)
                thread.start()

    def _relay(self, client, index):
        remote = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            remote.settimeout(SOCKET_TIMEOUT)
            client.settimeout(SOCKET_TIMEOUT)
            with self._lock:
                if self._stop.is_set():
                    return
                self._clients.update((client, remote))
            remote.connect(self.payload["socket"])
            remote.sendall(
                json.dumps({"provider": index, "token": self.payload["token"]}).encode()
                + b"\n"
            )
            while not self._stop.is_set():
                ready, _, _ = select.select([client, remote], [], [], 0.2)
                for current in ready:
                    data = current.recv(64 * 1024)
                    if not data:
                        return
                    (remote if current is client else client).sendall(data)
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                self._clients.discard(client)
                self._clients.discard(remote)
            remote.close()
            client.close()
            self._workers.release()

    def close(self):
        self._stop.set()
        for server in self._servers:
            server.close()
        for thread in self._accept_threads:
            thread.join(timeout=2)
        if any(thread.is_alive() for thread in self._accept_threads):
            raise NntpBridgeError("Contained NNTP acceptors did not stop.")
        with self._lock:
            streams = list(self._clients)
            threads = list(self._threads)
        for stream in streams:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            stream.close()
        for thread in threads:
            thread.join(timeout=2)
        if any(thread.is_alive() for thread in threads):
            raise NntpBridgeError("Contained NNTP relays did not stop.")


def run_contained_native(payload, environment):
    """Keep relays alive, own/reap the native child in the helper's process group."""
    relays = ContainedRelays(payload["nntp_bridge"])
    child = None
    stopped = threading.Event()

    def terminate(*unused):
        stopped.set()
        if child is not None:
            child.terminate()

    previous = {
        sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        environment = {**environment, **relays.start()}
        child = subprocess.Popen([payload["binary"]], env=environment)
        if stopped.is_set():
            child.terminate()
        while child.poll() is None:
            try:
                child.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                if stopped.is_set():
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
        return child.returncode
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        relays.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
