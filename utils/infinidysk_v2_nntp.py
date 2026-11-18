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

from utils.infinidysk_v2_provider_diagnostics import PROVIDER_FAILURE_CATEGORIES

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
MAX_TRANSIENT_ATTEMPTS = 2
PROVIDER_ENV = "NZBDAV_CONFIG__USENET__PROVIDERS"


class NntpBridgeError(ValueError):
    """A captured-provider bridge could not safely complete its operation."""


class _ProviderFailure(NntpBridgeError):
    def __init__(self, category, *, retryable=False):
        super().__init__("The captured provider could not complete an NNTP read.")
        self.category = category
        self.retryable = retryable


class _ProviderStatus(_ProviderFailure):
    def __init__(self, reply):
        super().__init__(
            "authentication"
            if reply[:3] in {b"480", b"481", b"482", b"502"}
            else "provider_reply"
        )
        self.reply = reply


def _provider_failure(error):
    if isinstance(error, _ProviderFailure):
        return error
    if isinstance(error, (ssl.SSLZeroReturnError, ssl.SSLEOFError)):
        return _ProviderFailure("connection_closed", retryable=True)
    if isinstance(error, ssl.SSLError):
        return _ProviderFailure("tls")
    if isinstance(error, (socket.timeout, TimeoutError)):
        return _ProviderFailure("timeout", retryable=True)
    if isinstance(error, OSError):
        return _ProviderFailure("connection_closed", retryable=True)
    return _ProviderFailure("protocol")


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


def _resolve_targets(host, port, *, timeout=8):
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
                timeout=timeout,
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
    if not value:
        raise _ProviderFailure("connection_closed", retryable=True)
    if len(value) > limit:
        raise _ProviderFailure("response_limit")
    if not value.endswith(b"\n"):
        raise _ProviderFailure("connection_closed", retryable=True)
    if not value.endswith(b"\r\n"):
        raise _ProviderFailure("protocol")
    return value


def _allowed_command(line, provider):
    command, separator, argument = line[:-2].partition(b" ")
    command = command.upper()
    if command in {b"BODY", b"HEAD", b"STAT", b"ARTICLE"}:
        return bool(
            separator and re.fullmatch(rb"<[^<>\s\x00-\x1f\x7f]{1,2000}>", argument)
        )
    if command in {b"CAPABILITIES", b"DATE", b"QUIT"}:
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


def _virtual_limit(providers):
    return max(
        MAX_WORKERS,
        sum(
            min(MAX_CONNECTIONS_PER_PROVIDER, row["MaxConnections"])
            for row in providers
            if row["Type"] in {1, 2, 3}
        ),
    )


class _ClientReader:
    """Keep buffered commands usable across idle periods and split TCP writes."""

    def __init__(self, stream, stop):
        self.stream, self.stop = stream, stop
        self.buffer = bytearray()

    def line(self, limit, *, idle=lambda: None, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.stop.is_set():
            end = self.buffer.find(b"\n")
            if end >= 0:
                if end + 1 > limit:
                    raise NntpBridgeError("A private NNTP command is too large.")
                value = bytes(self.buffer[: end + 1])
                del self.buffer[: end + 1]
                return value
            if len(self.buffer) >= limit:
                raise NntpBridgeError("A private NNTP command is too large.")
            if self.buffer and deadline is None:
                deadline = time.monotonic() + SOCKET_TIMEOUT
            if deadline is not None and time.monotonic() >= deadline:
                raise NntpBridgeError("A private NNTP command is incomplete.")
            idle()
            ready, _, _ = select.select([self.stream], [], [], 0.05)
            if ready:
                data = self.stream.recv(min(64 * 1024, limit - len(self.buffer)))
                if not data:
                    if self.buffer:
                        raise NntpBridgeError("A private NNTP command is incomplete.")
                    return None
                self.buffer.extend(data)
        return None


class _ProviderSession:
    """A native session survives replacement of its bounded upstream lease."""

    def __init__(self, bridge, index):
        self.bridge, self.index = bridge, index
        self.provider = bridge.providers[index]
        self.stream = self.response = None
        self.held = False
        self.deadline = None
        self.initialization = []
        self.last_command = time.monotonic()

    def close(self):
        stream, response = self.stream, self.response
        self.stream = self.response = None
        try:
            if response is not None:
                response.close()
        finally:
            try:
                if stream is not None:
                    with self.bridge._lock:
                        self.bridge._sockets.discard(stream)
                    try:
                        stream.close()
                    finally:
                        self.bridge._record("active_connections", -1)
            finally:
                if self.held:
                    self.held = False
                    self.bridge._release_budget(self.index)

    def idle(self):
        if self.stream is not None and (
            self.bridge._has_waiters()
            or time.monotonic() - self.last_command >= CLIENT_IDLE_TIMEOUT
        ):
            self.close()

    def _read_reply(self):
        self._io_deadline()
        reply = _line(self.response)
        if not re.match(rb"^[1-5][0-9]{2}(?: |\r)", reply):
            raise _ProviderFailure("protocol")
        self.bridge._received(self.index, len(reply))
        return reply

    def _open(self):
        self.bridge._acquire_budget(self.index, self.deadline)
        self.held = True
        try:
            self.stream = self.bridge._connect(self.provider, deadline=self.deadline)
            with self.bridge._lock:
                self.bridge._counts["active_connections"] += 1
                self.bridge._counts["peak_connections"] = max(
                    self.bridge._counts["peak_connections"],
                    self.bridge._counts["active_connections"],
                )
            self.response = self.stream.makefile("rb")
            greeting = self._read_reply()
            if greeting[:3] not in {b"200", b"201"}:
                raise _ProviderStatus(greeting)
            authenticated = False
            for command in self.initialization:
                if command.upper().startswith(b"AUTHINFO PASS ") and authenticated:
                    continue
                self._io_deadline()
                self.stream.sendall(command)
                reply = self._read_reply()
                if command.upper().startswith(b"AUTHINFO USER "):
                    if reply[:3] not in {b"281", b"381"}:
                        raise _ProviderFailure("authentication")
                    authenticated = reply[:3] == b"281"
                elif command.upper().startswith(b"AUTHINFO PASS "):
                    if reply[:3] != b"281":
                        raise _ProviderFailure("authentication")
                    authenticated = True
                elif reply[:3] not in {b"200", b"201"}:
                    raise _ProviderFailure("authentication")
            return greeting
        except BaseException:
            self.close()
            raise

    def _remember(self, command, reply):
        upper = command.upper()
        if upper.startswith(b"AUTHINFO USER ") and reply[:3] in {b"281", b"381"}:
            self.initialization = [
                row
                for row in self.initialization
                if not row.upper().startswith(b"AUTHINFO ")
            ]
            self.initialization.append(command)
        elif upper.startswith(b"AUTHINFO PASS ") and reply[:3] == b"281":
            self.initialization = [
                row
                for row in self.initialization
                if not row.upper().startswith(b"AUTHINFO PASS ")
            ]
            self.initialization.append(command)
        elif upper == b"MODE READER\r\n" and reply[:3] in {b"200", b"201"}:
            self.initialization = [
                row for row in self.initialization if row.upper() != upper
            ]
            self.initialization.append(command)

    def _retry(self, error, attempt):
        failure = _provider_failure(error)
        if not self.bridge._stop.is_set():
            self.bridge._failure(self.index, failure.category)
        self.close()
        if (
            not failure.retryable
            or attempt + 1 >= MAX_TRANSIENT_ATTEMPTS
            or self.bridge._stop.is_set()
            or time.monotonic() >= self.deadline
        ):
            raise failure from error
        self.bridge._provider_record(self.index, "retries")
        self.bridge._stop.wait(min(0.05, self.deadline - time.monotonic()))

    def _io_deadline(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _ProviderFailure("timeout", retryable=True)
        self.stream.settimeout(remaining)

    def greeting(self):
        self.deadline = time.monotonic() + SOCKET_TIMEOUT
        for attempt in range(MAX_TRANSIENT_ATTEMPTS):
            try:
                return self._open()
            except _ProviderStatus as error:
                self.bridge._failure(self.index, error.category)
                return error.reply
            except (OSError, ValueError) as error:
                self._retry(error, attempt)

    def command(self, command, client):
        self.deadline = time.monotonic() + SOCKET_TIMEOUT
        # Yield a warm connection to older queued work before taking new work.
        if self.bridge._has_waiters():
            self.close()
        for attempt in range(MAX_TRANSIENT_ATTEMPTS):
            try:
                if self.stream is None:
                    self._open()
                self._io_deadline()
                self.stream.sendall(command)
                reply = self._read_reply()
                break
            except _ProviderStatus as error:
                self.bridge._failure(self.index, error.category)
                _send_client(client, error.reply)
                raise
            except (OSError, ValueError) as error:
                self._retry(error, attempt)
        self._remember(command, reply)
        if reply[:1] in {b"4", b"5"}:
            category = (
                "authentication"
                if command.upper().startswith(b"AUTHINFO ")
                else (
                    "article_missing"
                    if reply[:3] in {b"423", b"430", b"451"}
                    else "provider_reply"
                )
            )
            self.bridge._failure(self.index, category)
        # Once any response is exposed, never replay the command: a partial
        # BODY must remain a failed read, not a concatenation of two articles.
        _send_client(client, reply)
        try:
            if reply[:3] in {b"100", b"101", b"215", b"220", b"221", b"222"}:
                size = 0
                while True:
                    # After exposure the native client uses a read-inactivity
                    # timeout; continuous article data may take longer overall.
                    self.stream.settimeout(SOCKET_TIMEOUT)
                    row = _line(self.response, MAX_BODY_LINE)
                    size += len(row)
                    if size > MAX_RESPONSE_BYTES:
                        raise _ProviderFailure("response_limit")
                    self.bridge._received(self.index, len(row))
                    _send_client(client, row)
                    if row == b".\r\n":
                        break
        except (OSError, ValueError) as error:
            failure = _provider_failure(error)
            self.bridge._failure(self.index, failure.category)
            raise failure from error
        self.last_command = time.monotonic()
        if self.bridge._has_waiters():
            self.close()
        return reply


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
        self._virtual_limit = _virtual_limit(self.providers)
        self._workers = threading.BoundedSemaphore(self._virtual_limit)
        self._budget = threading.Condition()
        self._queue = []
        self._leased_total = 0
        self._leased_providers = [0 for _ in self.providers]
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
        self._diagnostics = {
            "schema_version": 1,
            "providers": [
                {
                    "provider_ordinal": index + 1,
                    "requests": 0,
                    "bytes_received": 0,
                    "retries": 0,
                    "failures": {},
                }
                for index in range(len(self.providers))
            ],
            "unbound_failures": {},
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
                "provider_diagnostics": copy.deepcopy(self._diagnostics),
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

    def _provider_record(self, index, key, count=1):
        with self._lock:
            self._diagnostics["providers"][index][key] += count

    def _received(self, index, count):
        self._record("bytes_received", count, activity=True)
        self._provider_record(index, "bytes_received", count)

    def _failure(self, index, category):
        assert category in PROVIDER_FAILURE_CATEGORIES
        with self._lock:
            target = (
                self._diagnostics["unbound_failures"]
                if index is None
                else self._diagnostics["providers"][index]["failures"]
            )
            target[category] = target.get(category, 0) + 1
            if category != "article_missing":
                self._counts["provider_failures"] += 1

    def _has_waiters(self):
        with self._budget:
            return bool(self._queue)

    def _acquire_budget(self, index, deadline):
        ticket = object()
        with self._budget:
            self._queue.append((ticket, index))
            try:
                while not self._stop.is_set():
                    eligible = next(
                        (
                            row[0]
                            for row in self._queue
                            if self._leased_providers[row[1]]
                            < min(
                                MAX_CONNECTIONS_PER_PROVIDER,
                                self.providers[row[1]]["MaxConnections"],
                            )
                        ),
                        None,
                    )
                    if (
                        eligible is ticket
                        and self._leased_total < MAX_TOTAL_CONNECTIONS
                    ):
                        self._leased_total += 1
                        self._leased_providers[index] += 1
                        return
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _ProviderFailure("connection_limit", retryable=True)
                    self._budget.wait(min(0.05, remaining))
                raise _ProviderFailure("connection_closed")
            finally:
                self._queue.remove((ticket, index))
                self._budget.notify_all()

    def _release_budget(self, index):
        with self._budget:
            self._leased_total -= 1
            self._leased_providers[index] -= 1
            self._budget.notify_all()

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
            server.listen(self._virtual_limit)
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
                self._failure(None, "bridge_capacity")
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

    def _connect(self, provider, *, deadline=None):
        # The client cannot name another target: only the captured provider
        # selected by its authenticated ordinal reaches this code.
        key = provider["ProviderId"]
        deadline = (
            deadline if deadline is not None else time.monotonic() + SOCKET_TIMEOUT
        )
        with self._lock:
            addresses = self._resolved.get(key)
        if addresses is None:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _ProviderFailure("timeout", retryable=True)
                addresses = _resolve_targets(
                    provider["Host"], provider["Port"], timeout=min(8, remaining)
                )
            except _ProviderFailure:
                raise
            except NntpBridgeError as error:
                raise _ProviderFailure("dns", retryable=True) from error
            with self._lock:
                self._resolved[key] = addresses
        stream = None
        for address in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                stream = self._track(
                    socket.create_connection(
                        (address, provider["Port"]),
                        timeout=remaining,
                    )
                )
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
        if stream is None:
            raise _ProviderFailure("connect", retryable=True)
        if provider["UseSsl"]:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _ProviderFailure("timeout", retryable=True)
                stream.settimeout(remaining)
                context = ssl.create_default_context()
                # Keep hostname verification and stricter platform TLS policy.
                minimum = context.minimum_version
                context.minimum_version = (
                    ssl.TLSVersion.TLSv1_2
                    if minimum < ssl.TLSVersion.TLSv1_2
                    else minimum
                )
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
        session = None
        bound_index = None
        try:
            request = _ClientReader(client, self._stop)
            handshake = request.line(1024, timeout=SOCKET_TIMEOUT)
            if handshake is None:
                return
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
                raise NntpBridgeError("The private NNTP provider binding is invalid.")
            bound_index = index
            session = _ProviderSession(self, index)
            greeting = session.greeting()
            _send_client(client, greeting)
            if greeting[:3] not in {b"200", b"201"}:
                return
            while not self._stop.is_set():
                command = request.line(MAX_COMMAND_BYTES, idle=session.idle)
                if command is None:
                    return
                if not command.endswith(b"\r\n") or not _allowed_command(
                    command, self.providers[index]
                ):
                    self._record("denied_commands")
                    _send_client(
                        client,
                        b"502 Migration bridge permits read-only NNTP commands only\r\n",
                    )
                    return
                self._record("commands", activity=True)
                self._provider_record(index, "requests")
                reply = session.command(command, client)
                if reply[:3] == b"400" or command[:-2].upper() == b"QUIT":
                    return
        except _ClientDisconnected:
            pass
        except _ProviderFailure:
            # Closing signals a transport failure to the native retry policy.
            # A synthetic NNTP400 is a non-retryable Status error there.
            pass
        except (OSError, ValueError):
            if not self._stop.is_set():
                self._failure(
                    bound_index, "bridge_handshake" if session is None else "protocol"
                )
                if session is None:
                    try:
                        client.sendall(b"400 Invalid private migration binding\r\n")
                    except OSError:
                        pass
        finally:
            try:
                if session is not None:
                    session.close()
            except (OSError, ValueError):
                if not self._stop.is_set():
                    self._failure(bound_index, "protocol")
            finally:
                with self._lock:
                    self._sockets.discard(client)
                try:
                    client.close()
                finally:
                    self._workers.release()
                    with self._lock:
                        self._threads.discard(threading.current_thread())

    def close(self):
        self._stop.set()
        with self._budget:
            self._budget.notify_all()
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
        self._virtual_limit = _virtual_limit(self.payload["providers"])
        self._workers = threading.BoundedSemaphore(self._virtual_limit)

    def start(self):
        providers = copy.deepcopy(self.payload["providers"])
        for index, provider in enumerate(providers):
            if provider["Type"] not in {1, 2, 3}:
                continue
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.bind(("127.0.0.1", 0))
            server.listen(self._virtual_limit)
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
