"""Run an importer with no external network and a kernel write allowlist.

This is a controller-owned helper, never an API-supplied command. It must fail
closed when Linux namespaces or Landlock are unavailable.
"""

import ctypes
import fcntl
import json
import http.client
import os
from pathlib import Path
import socket
import shutil
import struct
import stat
import sys


class Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class Beneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _call(result, label):
    if result < 0:
        raise OSError(ctypes.get_errno(), label)
    return result


def namespace_command(uid, gid, command):
    """Grant network setup authority only inside a newly mapped user namespace."""
    uid, gid = int(uid), int(gid)
    if not 0 < uid < 2**31 or not 0 < gid < 2**31:
        raise ValueError("Contained workers require positive, bounded non-root UID/GID")
    program = shutil.which("unshare")
    if not program:
        raise ValueError("Contained workers require the unshare mapping helper")
    return [
        program,
        "--user",
        f"--map-users=0:0:{max(uid + 1, 1001)}",
        f"--map-groups=0:0:{max(gid + 1, 1001)}",
        "--",
        *command,
    ]


def confine(writable):
    if os.geteuid() != 0:
        raise ValueError(
            "Contained import requires the controller's namespace privileges"
        )
    libc = ctypes.CDLL(None, use_errno=True)
    abi = _call(libc.syscall(444, None, 0, 1), "Landlock is unavailable")
    if abi < 3:
        raise ValueError("Contained import requires Landlock ABI 3 or newer")
    _call(
        libc.unshare(0x40000000 | 0x00020000),
        "Private network/mount namespace unavailable",
    )
    _call(
        libc.mount(None, b"/", None, 16384 | (1 << 18), None),
        "Private mount propagation unavailable",
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        flags = fcntl.ioctl(control, 0x8913, struct.pack("16sH14s", b"lo", 0, b""))
        current = struct.unpack("16sH14s", flags)[1]
        fcntl.ioctl(control, 0x8914, struct.pack("16sH14s", b"lo", current | 1, b""))
    # Read/execute remain available. Every filesystem write, create, delete,
    # rename and truncate is denied except beneath these private destinations.
    rights = (1 << 1) | sum(1 << bit for bit in range(4, 15))
    attributes = Ruleset(rights)
    descriptor = _call(
        libc.syscall(444, ctypes.byref(attributes), ctypes.sizeof(attributes), 0),
        "Landlock ruleset failed",
    )
    try:
        for name in writable:
            path = Path(name)
            if path.is_symlink() or not path.is_dir():
                raise ValueError("Sandbox writable roots must be direct directories")
            parent = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = Beneath(rights, parent)
                _call(
                    libc.syscall(445, descriptor, 1, ctypes.byref(rule), 0),
                    "Landlock path rule failed",
                )
            finally:
                os.close(parent)
        null = os.open("/dev/null", os.O_PATH | os.O_CLOEXEC)
        try:
            rule = Beneath(1 << 1, null)
            _call(
                libc.syscall(445, descriptor, 1, ctypes.byref(rule), 0),
                "Landlock null rule failed",
            )
        finally:
            os.close(null)
        _call(libc.prctl(38, 1, 0, 0, 0), "No-new-privileges failed")
        _call(libc.syscall(446, descriptor, 0), "Landlock confinement failed")
    finally:
        os.close(descriptor)
    return abi


def probe(payload, method, path):
    if method != "GET" or path not in {
        "/api/v1/upgrade/status",
        "/api/v1/upgrade/report",
        "/api/v1/catalog",
        "/healthz",
    }:
        raise ValueError("Unsupported contained probe")
    descriptor = os.open(
        Path(payload["data_dir"], ".dumb-native-auth.json"),
        os.O_RDONLY | os.O_NOFOLLOW,
    )
    with os.fdopen(descriptor) as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 8192:
            raise ValueError(
                "Contained probe credentials must be a bounded regular file"
            )
        auth = json.load(handle)
    connection = http.client.HTTPConnection(
        "127.0.0.1", int(payload["port"]), timeout=5
    )
    try:
        connection.request(method, path, headers={"X-Api-Key": auth["api_key"]})
        response = connection.getresponse()
        raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError("Contained native response exceeds its bound")
        print(
            json.dumps(
                {"status": response.status, "body": json.loads(raw) if raw else {}}
            )
        )
    finally:
        connection.close()


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--check":
        uid, gid = int(sys.argv[2]), int(sys.argv[3])
        abi = confine([])
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
        print(
            json.dumps(
                {
                    "landlock_abi": abi,
                    "network_namespace": os.readlink("/proc/self/ns/net"),
                    "uid": os.geteuid(),
                    "gid": os.getegid(),
                    "external_network": False,
                }
            )
        )
        return
    payload_path = Path(sys.argv[1])
    if payload_path.is_symlink() or payload_path.stat().st_mode & 0o077:
        raise ValueError("Sandbox payload must be private")
    payload = json.loads(payload_path.read_text())
    if len(sys.argv) > 2 and sys.argv[2] == "probe":
        probe(payload, sys.argv[3], sys.argv[4])
        return
    data = Path(payload["data_dir"])
    temporary = Path(payload["tmp_dir"])
    proof = Path(payload["proof_path"])
    abi = confine([str(data), str(temporary)])
    # Proof is inside the write allowlist; the parent checks it before accepting
    # any importer receipt. No caller can replace it with request JSON.
    proof.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "landlock_abi": abi,
                "network_namespace": os.readlink("/proc/self/ns/net"),
                "external_network": False,
                "write_roots": [str(data), str(temporary)],
            }
        )
    )
    proof.chmod(0o600)
    os.setgroups([])
    os.setgid(int(payload["gid"]))
    os.setuid(int(payload["uid"]))
    os.chdir(data)
    environment = {
        "PATH": os.environ["PATH"],
        "TZ": payload.get("tz") or "UTC",
        "TMPDIR": str(temporary),
        **payload["environment"],
    }
    if payload.get("nntp_bridge"):
        # This packaged helper is trusted code. Import before chdir-dependent
        # resolution, retaining the same private namespace and process group.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from utils.infinidysk_v2_nntp import run_contained_native

        raise SystemExit(run_contained_native(payload, environment))
    os.execve(payload["binary"], [payload["binary"]], environment)


if __name__ == "__main__":
    main()
