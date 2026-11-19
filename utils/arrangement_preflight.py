"""Bounded plan-specific storage checks without enumerating remote libraries."""

from __future__ import annotations

import os
from collections import deque
from pathlib import Path
import queue
import stat
import threading
import time

from utils.arrangement_plan import project_configuration, resource_config
from utils.bazarr_paths import bazarr_data_directory

_SLOTS = threading.BoundedSemaphore(8)
REMOTE_FILESYSTEMS = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "fuse",
        "fuse.rclone",
        "fuse.zurg",
        "fuseblk",
        "autofs",
    }
)


def mount_inventory():
    result = []
    try:
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            left, right = line.split(" - ", 1)
            raw = left.split()[4]
            for code, replacement in (("\\040", " "), ("\\011", "\t"), ("\\134", "\\")):
                raw = raw.replace(code, replacement)
            result.append((raw, right.split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    return result


def filesystem_for(path, mounts):
    matching = [
        (mount, kind)
        for mount, kind in mounts
        if path == mount or path.startswith(mount.rstrip("/") + "/")
    ]
    return max(matching, key=lambda item: len(item[0]))[1] if matching else "unknown"


def _permission(info, uid, gid, write=False, *, search_only=False):
    mode = info.st_mode
    shift = 6 if info.st_uid == uid else 3 if info.st_gid == gid else 0
    required = 1 if search_only else 3 if write else 5
    # Ancestors need search permission, not permission to enumerate their
    # contents. Only the final directory needs read/search or write/search.
    return uid == 0 or ((mode >> shift) & required) == required


def _probe(path, uid, gid, write, mounts, *, create_owner=None):
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise ValueError("A storage directory must be an absolute path.")
    parts = deque(path.split("/"))
    current = Path("/")
    known = {}
    links = 0

    def remote(candidate):
        kind = filesystem_for(str(candidate), mounts)
        if kind in REMOTE_FILESYSTEMS or kind.startswith("fuse."):
            return {
                "present": None,
                "accessible": None,
                "filesystem": kind,
                "reason": "Remote mount access was not probed; verify it from the consumer after startup.",
            }
        return None

    pending_remote = remote(current)
    if pending_remote:
        return pending_remote
    info = current.lstat()
    known[current] = info
    present = False
    # Resolve one component at a time. realpath/stat on a symlink would traverse
    # its target before checking that target's mount; retaining the resolved
    # prefix also prevents child aliases and '..' from hiding a remote mount.
    for _ in range(512):
        if not _permission(info, uid, gid, search_only=True):
            return {
                "present": not parts,
                "accessible": False,
                "filesystem": filesystem_for(str(current), mounts),
                "reason": "The configured service UID/GID lacks directory search permission.",
            }
        if not parts:
            present = True
            break
        component = parts.popleft()
        if component in {"", "."}:
            continue
        if component == "..":
            current = current.parent
            info = known[current]
            continue
        child = current / component
        pending_remote = remote(child)
        if pending_remote:
            return pending_remote
        try:
            child_info = child.lstat()
        except FileNotFoundError:
            # Creating a missing descendant requires write/search on the
            # nearest existing directory; do not create anything in preflight.
            break
        if stat.S_ISLNK(child_info.st_mode):
            links += 1
            if links > 40:
                raise ValueError("Too many symbolic links in the storage path.")
            target = os.readlink(child)
            if target.startswith("/"):
                current = Path("/")
                info = known[current]
            parts.extendleft(reversed(target.split("/")))
            continue
        if not stat.S_ISDIR(child_info.st_mode):
            return {
                "present": False,
                "accessible": False,
                "filesystem": filesystem_for(str(child), mounts),
                "reason": "A configured directory component is not a directory.",
            }
        current, info = child, child_info
        known[current] = info
    else:
        raise ValueError("Storage path resolution exceeds its component limit.")
    needs_write = write or not present
    # A managed installer can create its missing runtime directory and assign
    # the new directory to the service. Existing ancestors must still be
    # searchable by that service; an existing unwritable directory never gains
    # an ownership exemption. This check itself creates or changes nothing.
    creation_planned = not present and write and create_owner is not None
    owner = create_owner if creation_planned else (uid, gid)
    can_assign_owner = not creation_planned or owner[0] == 0 or owner == (uid, gid)
    access = can_assign_owner and _permission(info, *owner, write=needs_write)
    capacity = os.statvfs(current)
    readonly = bool(getattr(capacity, "f_flag", 0) & os.ST_RDONLY)
    reason = "Directory metadata and configured primary UID/GID permissions checked; supplementary ACLs and media contents were not checked."
    if needs_write and readonly:
        reason = "The configuration filesystem is read-only; it cannot accept service configuration."
    elif not can_assign_owner:
        reason = "The controller cannot assign a new runtime directory to the configured service UID/GID."
    elif creation_planned:
        reason = (
            "DUMB can create the missing runtime directory during installation and assign the new directory to the service; existing ancestors are searchable. No directory was created by this check."
            if access
            else "The controller lacks write/search permission to create the missing runtime directory."
        )
    return {
        "present": present,
        "accessible": access and not (needs_write and readonly),
        "filesystem": filesystem_for(str(current), mounts),
        "free_bytes": capacity.f_bavail * capacity.f_frsize,
        "reason": reason,
    }


def storage_checks(plan, payload, current, defaults, *, mounts=None, timeout=3.0):
    proposed, _ = project_configuration(payload, current, defaults)
    mounts = mount_inventory() if mounts is None else mounts
    try:
        uid, gid = int(current.get("puid", 1000)), int(current.get("pgid", 1000))
    except (TypeError, ValueError):
        uid, gid = 1000, 1000
    candidates = {}
    controller_owner = (os.geteuid(), os.getegid())

    def candidate(
        path,
        write,
        resource,
        *,
        owner=None,
        create_owner=None,
        label=None,
        read_owner=None,
    ):
        if isinstance(path, str) and path.startswith("/"):
            candidates.setdefault(
                (path, write, owner or (uid, gid), create_owner, label, read_owner),
                resource,
            )

    for resource in plan["services"]:
        cfg = resource_config(proposed, resource["id"])
        fields = ("config_dir", "data_dir", "cache_dir", "mount_dir", "mount_path")
        if resource["service_key"] == "bazarr":
            # config_dir is Bazarr's root-owned Python application, not its
            # writable state. setup_bazarr prepares the separate --config root.
            candidate(
                cfg.get("config_dir") or "/opt/bazarr",
                True,
                resource["id"],
                owner=controller_owner,
                label="Application installation",
                read_owner=(uid, gid),
            )
            candidate(
                bazarr_data_directory(cfg),
                True,
                resource["id"],
                create_owner=controller_owner,
            )
            fields = ("cache_dir", "mount_dir", "mount_path")
        if (
            resource["service_key"] == "infinidysk"
            and str(cfg.get("runtime_generation") or "1") == "2"
        ):
            fields = ("v2_data_dir", "v2_runtime_dir", "mount_path")
        for field in fields:
            candidate(
                cfg.get(field),
                field not in {"mount_dir", "mount_path"},
                resource["id"],
            )
    values = (payload.get("arrangement") or {}).get("values") or {}
    for key, rows in values.items():
        if str(key).endswith("::libraries") and isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    for path in row.get("paths") or []:
                        candidate(path, False, key.partition("::")[0])
    result_queue = queue.Queue()
    task_queue = queue.Queue()
    checks = []
    pending = {}

    def worker():
        try:
            while time.monotonic() < deadline:
                try:
                    index, path, write, owner, create_owner, read_owner = (
                        task_queue.get_nowait()
                    )
                except queue.Empty:
                    return
                try:
                    evidence = _probe(
                        path, *owner, write, mounts, create_owner=create_owner
                    )
                    if (
                        read_owner is not None
                        and evidence.get("present") is True
                        and evidence.get("accessible") is not False
                    ):
                        # A root-owned application need not be service-writable,
                        # but the service must still read/search an existing
                        # tree. An absent tree is prepared by the installer.
                        access = _probe(path, *read_owner, False, mounts)
                        if access.get("accessible") is not True:
                            evidence.update(
                                accessible=access.get("accessible"),
                                reason=(
                                    "The configured service UID/GID cannot read/search the existing application directory."
                                    if access.get("accessible") is False
                                    else "Service access to the existing application directory remains unverified."
                                ),
                            )
                except (OSError, ValueError, UnboundLocalError):
                    evidence = {
                        "present": None,
                        "accessible": None,
                        "reason": "Storage metadata could not be checked within the current container.",
                    }
                result_queue.put((index, evidence))
        finally:
            _SLOTS.release()

    deadline = time.monotonic() + max(0.05, min(float(timeout), 5.0))
    for index, (
        (path, write, owner, create_owner, label, read_owner),
        resource,
    ) in enumerate(list(candidates.items())[:100]):
        record = {
            "key": f"storage:{resource}:{index}",
            "label": f"{label or ('Writable configuration' if write else 'Media/mount access')}: {path}",
            "ok": None,
            "critical": write,
            "resource_id": resource,
            "path": path,
            "content_checked": False,
            "detail": "Storage check pending or at capacity; retry before applying.",
        }
        checks.append(record)
        pending[index] = record
        task_queue.put((index, path, write, owner, create_owner, read_owner))
    for _ in range(min(4, len(pending))):
        if _SLOTS.acquire(blocking=False):
            threading.Thread(
                target=worker,
                name="setup-storage-probe",
                daemon=True,
            ).start()
    while pending and time.monotonic() < deadline:
        try:
            index, evidence = result_queue.get(
                timeout=max(0.001, deadline - time.monotonic())
            )
        except queue.Empty:
            break
        record = pending.pop(index)
        record.update(evidence)
        record["detail"] = record.pop("reason")
        record["ok"] = (
            False
            if evidence.get("accessible") is False
            else (
                evidence.get("accessible")
                if record["critical"] or evidence.get("present")
                else None
            )
        )
        if (
            record["critical"]
            and evidence.get("free_bytes") is not None
            and evidence["free_bytes"] < 128 * 1024 * 1024
        ):
            record.update(
                ok=False,
                detail="Less than 128 MiB is available on the configuration filesystem; free space before installing.",
            )
    for record in pending.values():
        record.update(
            detail="Storage metadata check timed out. The path remains unverified; no remote directory was enumerated."
        )
    return checks
