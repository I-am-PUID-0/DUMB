"""Fixed read-only consumer probe, executed in a short-lived isolated child.

The parent supplies a tracked PID and its start time. No commands are accepted.
Namespace/root descriptors are captured before dropping to the consumer's real
effective identity and supplementary groups. Only metadata or a bounded sample
of an explicitly selected regular media file is returned, never file contents.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import stat
import sys
from collections import deque

from utils.arrangement_preflight import filesystem_for, REMOTE_FILESYSTEMS


def start_time(pid):
    with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
        return handle.read().rsplit(")", 1)[1].split()[19]


def _same(left, right):
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def enter_consumer(pid, expected):
    if start_time(pid) != expected:
        raise ValueError("The selected process changed before verification.")
    with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
        fields = dict(line.split(":", 1) for line in handle if ":" in line)
    uid, gid = int(fields["Uid"].split()[1]), int(fields["Gid"].split()[1])
    groups = [int(value) for value in fields.get("Groups", "").split()][:128]
    # Docker commonly omits CAP_SYS_PTRACE. Capture proc descriptors with the
    # consumer's fs credentials instead of requiring a privileged container.
    previous_uid, previous_gid = os.geteuid(), os.getegid()
    ns = root = None
    try:
        if previous_uid == 0:
            os.setegid(gid)
            os.seteuid(uid)
        elif uid != previous_uid or gid != previous_gid:
            raise PermissionError("The controller cannot assume the consumer identity.")
        ns = os.open(f"/proc/{pid}/ns/mnt", os.O_RDONLY | os.O_CLOEXEC)
        root = os.open(f"/proc/{pid}/root", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except Exception:
        if ns is not None:
            os.close(ns)
        raise
    finally:
        if previous_uid == 0:
            os.seteuid(previous_uid)
            os.setegid(previous_gid)
    try:
        if start_time(pid) != expected:
            raise ValueError("The selected process changed during verification.")
        if not _same(os.fstat(ns), os.stat("/proc/self/ns/mnt")):
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.setns(ns, 0) != 0:
                raise PermissionError(
                    "The controller cannot enter this mount namespace."
                )
        if not _same(os.fstat(root), os.stat("/")):
            os.fchdir(root)
            os.chroot(".")
            os.chdir("/")
        if os.geteuid() == 0:
            os.setgroups(groups)
            os.setgid(gid)
            os.setuid(uid)
        elif uid != os.geteuid() or gid != os.getegid():
            raise PermissionError("The controller cannot assume the consumer identity.")
    finally:
        os.close(ns)
        os.close(root)
    return uid, gid


def _mounts(pid):
    rows = []
    with open(f"/proc/{pid}/mountinfo", encoding="utf-8") as handle:
        for line in handle:
            left, separator, right = line.partition(" - ")
            if not separator:
                continue
            path = left.split()[4]
            for old, new in (("\\040", " "), ("\\011", "\t"), ("\\134", "\\")):
                path = path.replace(old, new)
            rows.append((path, right.split()[0]))
    return rows


def _authorize_path(path, mounts, allow_remote):
    """Resolve local aliases before touching their remote or protected target."""
    parts, current, links = deque(path.split("/")), "/", 0
    for _ in range(512):
        if current.split("/")[1] in {
            "config",
            "etc",
            "proc",
            "sys",
            "dev",
            "root",
            "run",
        }:
            raise ValueError("System and configuration targets are not media probes.")
        kind = filesystem_for(current, mounts)
        if not allow_remote and (
            kind == "unknown" or kind in REMOTE_FILESYSTEMS or kind.startswith("fuse.")
        ):
            raise ValueError("Remote-mount reading was not authorized.")
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode):
            links += 1
            if links > 40:
                raise ValueError("Too many path aliases.")
            target = os.readlink(current)
            current = "/" if target.startswith("/") else os.path.dirname(current)
            parts.extendleft(reversed(target.split("/")))
            continue
        if not parts:
            return current
        part = parts.popleft()
        if part in {"", "."}:
            continue
        current = (
            os.path.dirname(current) if part == ".." else os.path.join(current, part)
        )
    raise ValueError("Media path resolution exceeds its bound.")


def _check_mount(path, mounts, allow_remote):
    kind = filesystem_for(path, mounts)
    if not allow_remote and (
        kind == "unknown" or kind in REMOTE_FILESYSTEMS or kind.startswith("fuse.")
    ):
        raise ValueError("Remote-mount reading was not authorized.")


def _open_directory(path, mounts, allow_remote):
    """Open the resolved path without following aliases changed after inspection."""
    descriptor = os.open("/", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    current = "/"
    parts = [part for part in path.split("/") if part]
    try:
        for index, component in enumerate(parts):
            current = os.path.join(current, component)
            _check_mount(current, mounts, allow_remote)
            flags = os.O_RDONLY if index == len(parts) - 1 else os.O_PATH
            child = os.open(
                component,
                flags | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def probe(request):
    mounts = _mounts(int(request["pid"]))
    uid, gid = enter_consumer(int(request["pid"]), str(request["start_time"]))
    results = []
    for row in request["paths"]:
        record = {"path": row["path"], "accessible": False, "content_checked": False}
        descriptor = None
        try:
            resolved = _authorize_path(
                row["path"], mounts, row.get("allow_remote") is True
            )
            descriptor = _open_directory(
                resolved, mounts, row.get("allow_remote") is True
            )
            record["accessible"] = True
            relative = row.get("sample_file") or ""
            if relative:
                parts = relative.split("/")
                if relative.startswith("/") or any(p in {"", ".", ".."} for p in parts):
                    raise ValueError("Use an exact relative sample-file path.")
                for component in parts[:-1]:
                    resolved = os.path.join(resolved, component)
                    _check_mount(resolved, mounts, row.get("allow_remote") is True)
                    child = os.open(
                        component,
                        os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                    os.close(descriptor)
                    descriptor = child
                _check_mount(
                    os.path.join(resolved, parts[-1]),
                    mounts,
                    row.get("allow_remote") is True,
                )
                sample = os.open(
                    parts[-1],
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=descriptor,
                )
                try:
                    info = os.fstat(sample)
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError(
                            "The selected sample must be a regular media file."
                        )
                    limit = max(1, min(int(row.get("max_bytes") or 65536), 1048576))
                    content = os.read(sample, limit)
                    record.update(
                        content_checked=True,
                        read_bytes=len(content),
                        sample_sha256=hashlib.sha256(content).hexdigest(),
                        sample_complete=info.st_size <= len(content),
                    )
                    if not content:
                        raise ValueError("The selected sample is empty.")
                finally:
                    os.close(sample)
        except (OSError, ValueError):
            record.update(
                accessible=False,
                reason="The consumer could not open the selected directory or regular sample.",
            )
        finally:
            if descriptor is not None:
                os.close(descriptor)
        results.append(record)
    return {"uid": uid, "gid": gid, "namespace_verified": True, "paths": results}


def main():
    try:
        request = json.loads(sys.stdin.buffer.read(65537))
        result = probe(request)
    except (OSError, ValueError, KeyError, TypeError):
        result = {
            "namespace_verified": False,
            "reason": "The tracked consumer identity or namespace could not be verified.",
        }
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
