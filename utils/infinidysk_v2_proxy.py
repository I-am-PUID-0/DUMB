"""Bounded, process-owned TCP compatibility for the retained 1.x backend port.

Raw forwarding preserves SAB, WebDAV, signed playback paths, ranges and upgraded
connections. No request/response content or credentials are inspected or logged.
"""

import ipaddress
import select
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit


class CompatibilityProxy:
    def __init__(
        self,
        host,
        port,
        target_port,
        *,
        process=None,
        max_connections=32,
        idle_timeout=600,
        connect_timeout=5,
    ):
        address = ipaddress.ip_address(host)
        self.target_port = target_port
        self.process = process
        self.idle_timeout = idle_timeout
        self.connect_timeout = connect_timeout
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.sockets = set()
        self.slots = threading.BoundedSemaphore(max_connections)
        self.pool = ThreadPoolExecutor(
            max_workers=max_connections, thread_name_prefix="infinidysk-compat"
        )
        self.listener = socket.socket(
            socket.AF_INET6 if address.version == 6 else socket.AF_INET,
            socket.SOCK_STREAM,
        )
        try:
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listener.bind((host, port))
            self.listener.listen(max_connections)
            self.listener.settimeout(0.2)
        except OSError:
            self.listener.close()
            self.pool.shutdown(wait=False, cancel_futures=True)
            raise
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(
            target=self._accept, name="infinidysk-compat-listener", daemon=True
        )
        self.thread.start()

    def _accept(self):
        try:
            while not self.stop.is_set() and (
                self.process is None or self.process.returncode is None
            ):
                try:
                    client, _ = self.listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not self.slots.acquire(blocking=False):
                    client.close()
                    continue
                with self.lock:
                    self.sockets.add(client)
                try:
                    self.pool.submit(self._connection, client)
                except RuntimeError:
                    with self.lock:
                        self.sockets.discard(client)
                    client.close()
                    self.slots.release()
                    break
        finally:
            self.stop.set()
            self.listener.close()
            self._close_sockets()
            self.pool.shutdown(wait=False, cancel_futures=True)

    def _close_sockets(self):
        with self.lock:
            sockets = list(self.sockets)
        for connection in sockets:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def _connection(self, client):
        upstream = None
        try:
            upstream = socket.create_connection(
                ("127.0.0.1", self.target_port), timeout=self.connect_timeout
            )
            with self.lock:
                self.sockets.add(upstream)
            if self.stop.is_set():
                return
            peers = (client, upstream)
            for connection in peers:
                connection.setblocking(False)
            pending = [bytearray(), bytearray()]
            reading = [True, True]
            write_closed = [False, False]
            touched = time.monotonic()
            while (
                not self.stop.is_set()
                and time.monotonic() - touched < self.idle_timeout
            ):
                for index in (0, 1):
                    if (
                        not reading[1 - index]
                        and not pending[index]
                        and not write_closed[index]
                    ):
                        peers[index].shutdown(socket.SHUT_WR)
                        write_closed[index] = True
                if not any(reading) and not any(pending):
                    break
                readable = [
                    peers[i]
                    for i in (0, 1)
                    if reading[i] and len(pending[1 - i]) < 65536
                ]
                writable = [peers[i] for i in (0, 1) if pending[i]]
                ready_read, ready_write, _ = select.select(readable, writable, [], 0.2)
                for connection in ready_read:
                    index = peers.index(connection)
                    chunk = connection.recv(65536 - len(pending[1 - index]))
                    if chunk:
                        pending[1 - index].extend(chunk)
                        touched = time.monotonic()
                    else:
                        reading[index] = False
                for connection in ready_write:
                    index = peers.index(connection)
                    count = connection.send(pending[index])
                    del pending[index][:count]
                    touched = time.monotonic()
        except (OSError, ValueError):
            # A restart, client close or unavailable native listener is expected.
            pass
        finally:
            for connection in (client, upstream):
                if connection is not None:
                    with self.lock:
                        self.sockets.discard(connection)
                    connection.close()
            self.slots.release()

    def close(self):
        self.stop.set()
        self.listener.close()
        self._close_sockets()
        if threading.current_thread() is not self.thread:
            self.thread.join(timeout=2)
        self.pool.shutdown(wait=True, cancel_futures=True)


def start_compatibility_proxy(service, process):
    frontend = int(service.get("frontend_port") or 3000)
    backend = int(service.get("backend_port") or 8080)
    if not 1 <= backend <= 65535 or not 1 <= frontend <= 65535:
        raise ValueError("Invalid InfiniDysk compatibility listener port")
    if backend == frontend:
        return None
    urls = str((service.get("env") or {}).get("ASPNETCORE_URLS") or "http://0.0.0.0")
    if ";" in urls:
        raise ValueError(
            "Multiple legacy backend interfaces require explicit compatibility configuration"
        )
    host = urlsplit(urls.replace("*", "0.0.0.0").replace("+", "0.0.0.0")).hostname
    if host == "localhost":
        host = "127.0.0.1"
    if not host:
        raise ValueError("Cannot resolve the retained backend listener interface")
    return CompatibilityProxy(host, backend, frontend, process=process)
