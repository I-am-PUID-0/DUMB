"""Side-effect-free TCP port availability probes."""

from __future__ import annotations

import errno
import socket

import psutil


def _check_bind(family: int, address: str, port: int) -> bool | None:
    sock = None
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            try:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            except OSError:
                pass

        # This is a transient availability probe. The socket is never placed
        # into listening mode, never accepts traffic, and is closed immediately.
        sock.bind((address, port))
        return True
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES, errno.EPERM):
            return False
        if exc.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, errno.EINVAL):
            return None
        return False
    finally:
        if sock is not None:
            sock.close()


def is_port_available(port: int) -> bool:
    """Return whether a port is free for a managed service to bind."""

    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port <= 65535:
        return False

    try:
        for connection in psutil.net_connections(kind="inet"):
            if (
                connection.status == psutil.CONN_LISTEN
                and connection.laddr
                and connection.laddr.port == port
            ):
                return False
    except Exception:
        # Binding below remains the authoritative fallback when process
        # visibility is restricted.
        pass

    for family, address in (
        (socket.AF_INET, "127.0.0.1"),
        (socket.AF_INET6, "::1"),
    ):
        if _check_bind(family, address, port) is False:
            return False
    return True
