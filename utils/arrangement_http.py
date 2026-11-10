"""Bounded API transport with pinned DNS, no proxy inheritance or redirects."""

from __future__ import annotations

import http.client
import ipaddress
import json as json_module
import queue
import socket
import ssl
import threading
import time
import math
from http.cookies import SimpleCookie
from urllib.parse import urlencode, urlsplit, urlunsplit

MAX_RESPONSE = 2 * 1024 * 1024
MAX_SCHEMA_RESPONSE = 16 * 1024 * 1024
MAX_JSON_NODES = 1000000
_DNS_SLOTS = threading.BoundedSemaphore(4)
_METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(value)
    for value in ("100.100.100.200", "168.63.129.16", "fd00:ec2::254")
)


class ApiError(RuntimeError):
    def __init__(
        self,
        message="The application API request failed.",
        *,
        status_code=None,
        stage=None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.stage = stage


def _bounded_limit(value, default, maximum):
    if value is None:
        return default
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= maximum
    ):
        raise ValueError("Use a bounded integer API response limit.")
    return value


class ApiResponse:
    def __init__(self, status_code, content=b"", headers=(), *, json_node_limit=100000):
        self.status_code = status_code
        self.json_node_limit = _bounded_limit(json_node_limit, 100000, MAX_JSON_NODES)
        self.content = content
        self.headers = dict(headers)
        self.text = content.decode("utf-8", errors="replace")

    def json(self):
        try:
            value = json_module.loads(
                self.content.decode("utf-8"),
                parse_constant=lambda _: (_ for _ in ()).throw(
                    ValueError("Invalid JSON number.")
                ),
            )
            pending, count = [(value, 0)], 0
            while pending:
                item, depth = pending.pop()
                count += 1
                if depth > 32 or count > self.json_node_limit:
                    raise ValueError("API response structure is too large.")
                if isinstance(item, dict):
                    pending.extend((child, depth + 1) for child in item.values())
                elif isinstance(item, list):
                    pending.extend((child, depth + 1) for child in item)
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise ApiError(
                "The application returned invalid JSON.",
                status_code=self.status_code,
                stage="response",
            ) from None

    def raise_for_status(self):
        if not 200 <= self.status_code < 300:
            raise ApiError(
                "The application rejected the API request.",
                status_code=self.status_code,
                stage="response",
            )


def _resolve(host, port):
    """Keep stalled resolver work bounded without changing global timeouts."""
    if not _DNS_SLOTS.acquire(blocking=False):
        raise ApiError(
            "Address resolution is busy; retry the connection check.", stage="dns"
        )
    result = queue.Queue(maxsize=1)

    def lookup():
        try:
            result.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError:
            result.put(None)
        finally:
            _DNS_SLOTS.release()

    threading.Thread(target=lookup, name="setup-dns", daemon=True).start()
    try:
        addresses = result.get(timeout=3)
    except queue.Empty:
        raise ApiError("Address resolution timed out.", stage="dns") from None
    if not addresses:
        raise ApiError("The application hostname could not be resolved.", stage="dns")
    return list(dict.fromkeys(item[4][0] for item in addresses))


def validate_endpoint(url, *, managed=False, allow_private=False):
    if (
        not isinstance(url, str)
        or len(url) > 2048
        or any(ord(ch) <= 32 or ch == "\\" for ch in url)
    ):
        raise ValueError("Use an absolute HTTP or HTTPS application URL.")
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "Use an HTTP(S) application URL without embedded credentials, query or fragment."
        )
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise ValueError("Invalid application port.") from None
    if not 1 <= port <= 65535:
        raise ValueError("Invalid application port.")
    addresses = _resolve(parts.hostname, port)
    for address in addresses:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
        parsed = (
            parsed.ipv4_mapped
            if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped
            else parsed
        )
        if (
            parsed.is_link_local
            or parsed.is_unspecified
            or parsed.is_multicast
            or parsed in _METADATA_ADDRESSES
        ):
            raise ValueError(
                "This address is not permitted for an application connection."
            )
        if managed:
            if not parsed.is_loopback:
                raise ValueError(
                    "Managed application connections must use loopback addresses."
                )
        elif not parsed.is_global:
            private_networks = (
                "10.0.0.0/8",
                "172.16.0.0/12",
                "192.168.0.0/16",
                "100.64.0.0/10",
                "127.0.0.0/8",
                "::1/128",
                "fc00::/7",
            )
            authorized_private = allow_private and any(
                parsed.version == network.version and parsed in network
                for network in (ipaddress.ip_network(cidr) for cidr in private_networks)
            )
            if not authorized_private:
                raise ValueError(
                    "Authorize a supported private-network address for this external application."
                )
    return parts, port, addresses


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, address, port, timeout):
        super().__init__(host, port=port, timeout=timeout)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, port, timeout, context):
        super().__init__(host, port=port, timeout=timeout, context=context)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        except Exception:
            self.sock.close()
            raise


class ApiClient:
    def __init__(
        self,
        base_url,
        *,
        headers=None,
        managed=False,
        allow_private=False,
        verify_tls=True,
        timeout=8,
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.managed = managed
        self.allow_private = allow_private
        self.verify_tls = verify_tls
        timeout = float(timeout)
        if not math.isfinite(timeout):
            raise ValueError("Use a finite application timeout.")
        self.timeout = min(max(timeout, 1), 15)
        self.cookies = {}

    def request(
        self,
        method,
        path,
        *,
        json=None,
        data=None,
        params=None,
        headers=None,
        response_limit=None,
        json_node_limit=None,
    ):
        response_limit = _bounded_limit(
            response_limit, MAX_RESPONSE, MAX_SCHEMA_RESPONSE
        )
        json_node_limit = _bounded_limit(json_node_limit, 100000, MAX_JSON_NODES)
        if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE", "PROPFIND"}:
            raise ValueError("Unsupported application API method.")
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or any(ch in path for ch in "\r\n\\")
        ):
            raise ValueError("Use a relative application API path.")
        parts, port, addresses = validate_endpoint(
            self.base_url, managed=self.managed, allow_private=self.allow_private
        )
        base = parts.path.rstrip("/")
        endpoint = urlsplit(base + path)
        if endpoint.scheme or endpoint.netloc or endpoint.fragment:
            raise ValueError("Invalid application API path.")
        query = endpoint.query
        if params:
            query += ("&" if query else "") + urlencode(params, doseq=True)
        target = urlunsplit(("", "", endpoint.path, query, ""))
        request_headers = {
            "Accept": "application/json",
            **self.headers,
            **(headers or {}),
        }
        network_host = parts.hostname.encode("idna").decode("ascii")
        hostname = f"[{network_host}]" if ":" in network_host else network_host
        request_headers["Host"] = f"{hostname}:{port}"
        if self.cookies:
            request_headers["Cookie"] = "; ".join(
                f"{key}={value}" for key, value in self.cookies.items()
            )
        body = None
        if json is not None:
            body = json_module.dumps(json).encode()
            request_headers["Content-Type"] = "application/json"
        elif isinstance(data, dict):
            body = urlencode(data, doseq=True).encode()
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif data is not None:
            body = data.encode() if isinstance(data, str) else data
        if body is not None and len(body) > 262144:
            raise ValueError("Application setup request is too large.")
        address = addresses[0]
        if parts.scheme == "https":
            context = ssl.create_default_context()
            if not self.verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            connection = _PinnedHTTPS(
                network_host, address, port, self.timeout, context=context
            )
        else:
            connection = _PinnedHTTP(network_host, address, port, self.timeout)
        timed_out = threading.Event()

        def abort():
            timed_out.set()
            active_socket = getattr(connection, "sock", None)
            if active_socket is not None:
                try:
                    active_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

        deadline = time.monotonic() + self.timeout
        timer = threading.Timer(self.timeout, abort)
        timer.daemon = True
        try:
            # Validate headers before transmitting any credentials.
            for name, value in request_headers.items():
                if (
                    not isinstance(name, str)
                    or not isinstance(value, str)
                    or not name
                    or len(name) > 128
                    or len(value) > 16384
                    or any(ord(ch) < 32 or ord(ch) == 127 for ch in name + value)
                    or any(ch in name for ch in ' ()<>@,;:\\"/[]?={}')
                ):
                    raise ValueError("Invalid application API headers.")
            if (
                sum(len(name) + len(value) for name, value in request_headers.items())
                > 32768
            ):
                raise ValueError("Application API headers exceed the supported size.")
            timer.start()
            connection.request(
                method.upper(), target, body=body, headers=request_headers
            )
            response = connection.getresponse()
            if timed_out.is_set():
                raise ApiError(
                    "The application API exceeded its time limit.", stage="timeout"
                )
            chunks, size = [], 0
            reader = getattr(response, "read1", response.read)
            while True:
                remaining = deadline - time.monotonic()
                if timed_out.is_set() or remaining <= 0:
                    raise ApiError(
                        "The application API exceeded its time limit.", stage="timeout"
                    )
                active_socket = getattr(connection, "sock", None)
                if active_socket is not None:
                    active_socket.settimeout(remaining)
                chunk = reader(min(65536, response_limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > response_limit:
                    raise ApiError(
                        "The application API response exceeded its size limit."
                    )
            content = b"".join(chunks)
            response_headers = response.getheaders()
            if (
                sum(
                    len(str(name)) + len(str(value)) for name, value in response_headers
                )
                > 65536
            ):
                raise ApiError(
                    "The application API response headers exceeded their size limit."
                )
            for header, value in response_headers:
                if header.lower() == "set-cookie" and len(value) < 8192:
                    cookies = SimpleCookie()
                    cookies.load(value)
                    for key, morsel in cookies.items():
                        if (
                            len(key) <= 128
                            and len(morsel.value) <= 4096
                            and (len(self.cookies) < 32 or key in self.cookies)
                        ):
                            candidate = {**self.cookies, key: morsel.value}
                            if (
                                sum(
                                    len(name) + len(cookie)
                                    for name, cookie in candidate.items()
                                )
                                <= 16384
                            ):
                                self.cookies = candidate
            return ApiResponse(
                response.status,
                content,
                response_headers,
                json_node_limit=json_node_limit,
            )
        except ssl.SSLError:
            raise ApiError(
                "The application TLS connection could not be verified.", stage="tls"
            ) from None
        except (TimeoutError, socket.timeout):
            raise ApiError(
                "The application API exceeded its time limit.", stage="timeout"
            ) from None
        except (OSError, http.client.HTTPException):
            raise ApiError(
                "The application API could not be reached securely.", stage="connect"
            ) from None
        finally:
            timer.cancel()
            connection.close()

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def put(self, path, **kwargs):
        return self.request("PUT", path, **kwargs)
