"""Private ownership records; application routing metadata is never repurposed."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from utils.private_files import atomic_write_private_text, read_private_text


def fingerprint(value):
    """Legacy-compatible private configuration equality, never password storage."""
    return hashlib.sha256(
        # Private configuration equality in ownership sidecars, not a login verifier.
        # Keep legacy bytes; file access, not this digest, protects credential secrecy.
        # codeql[py/weak-sensitive-data-hashing]
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def read_ownership(path, *, owner_ids=()):
    if os.path.islink(path):
        raise ValueError("Integration ownership path must be a regular file")
    try:
        value = json.loads(read_private_text(path, owner_ids=owner_ids))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Integration ownership records must be an object")
    return value


def write_ownership(path, records):
    if os.path.islink(path):
        raise ValueError("Integration ownership path must be a regular file")
    atomic_write_private_text(
        path, json.dumps(records, sort_keys=True, indent=2) + "\n"
    )


def arr_ownership_path(host, manager):
    port = urlsplit(host).port
    for service in ("radarr", "sonarr", "lidarr", "whisparr"):
        for instance in ((manager.get(service) or {}).get("instances") or {}).values():
            try:
                matches = (
                    int(instance.get("port") or instance.get("host_port") or 0) == port
                )
            except (TypeError, ValueError):
                matches = False
            if matches and instance.get("config_file"):
                return str(
                    Path(instance["config_file"]).parent / ".dumb-integrations.json"
                )
    raise ValueError("The Arr configuration directory is not available")


def ensure_media_directory(path, user_id=None, group_id=None, mode=0o777):
    """Own only directories created by this call; never traverse existing media."""
    target = Path(path)
    if any(parent.is_symlink() for parent in (target, *target.parents)):
        return {"created": False, "ownership_mismatch": True}
    missing = []
    created = False
    current = target
    while not current.exists():
        if current.is_symlink():
            return {"created": False, "ownership_mismatch": True}
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        return {"created": False, "ownership_mismatch": True}
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=mode)
        except FileExistsError:
            # Another caller created it. Its ownership is no longer ours.
            continue
        created = True
        if user_id is not None and group_id is not None:
            os.chown(directory, int(user_id), int(group_id))
        directory.chmod(mode)
    info = target.stat()
    mismatch = (
        user_id is not None
        and group_id is not None
        and (info.st_uid, info.st_gid) != (int(user_id), int(group_id))
    )
    return {"created": created, "ownership_mismatch": mismatch}
