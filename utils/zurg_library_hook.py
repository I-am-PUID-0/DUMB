"""Opt-in Zurg update receiver: scoped Plex partial refreshes, never content I/O."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import sys

# Supports direct invocation from Zurg's own working directory.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.arrangement_http import ApiClient


def _absolute(value):
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value == "/"
        or any(ord(char) < 32 for char in value)
        or ".." in PurePosixPath(value).parts
    ):
        raise ValueError("Use a scoped absolute media path without parent traversal.")
    return PurePosixPath(value)


def validate_hook_configuration(config):
    mount = _absolute(config.get("mount_path"))
    libraries = config.get("libraries")
    if (
        not isinstance(libraries, dict)
        or not libraries
        or len(libraries) > 50
        or not isinstance(config.get("api_key"), str)
        or not config["api_key"]
    ):
        raise ValueError(
            "Select authenticated Plex libraries for the optional update hook."
        )
    for key, roots in libraries.items():
        if (
            not re.fullmatch(r"[0-9]{1,12}", str(key))
            or not isinstance(roots, list)
            or not roots
        ):
            raise ValueError("Choose valid Plex library IDs and their existing roots.")
        if not any(
            mount.is_relative_to(_absolute(root))
            or _absolute(root).is_relative_to(mount)
            for root in roots
        ):
            raise ValueError(
                "The selected mount path does not overlap each selected Plex library root."
            )
    return mount


def refresh_changed_paths(config, changed, *, client_factory=ApiClient):
    """Return number of accepted refresh requests, without reading media bytes."""
    mount = validate_hook_configuration(config)
    if not isinstance(changed, (list, tuple)) or len(changed) > 100:
        raise ValueError("The update event exceeds its supported path limit.")
    paths = []
    for value in changed:
        if (
            not isinstance(value, str)
            or len(value) > 4096
            or any(ord(ch) < 32 for ch in value)
            or "\\" in value
        ):
            raise ValueError("Invalid Zurg update path.")
        relative = PurePosixPath(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(
                "Zurg update paths must stay beneath their selected mount."
            )
        path = mount / relative
        if path not in paths:
            paths.append(path)
    client = client_factory(
        config["url"],
        managed=True,
        headers={"X-Plex-Token": config["api_key"]},
        timeout=8,
    )
    count = 0
    for key, roots in config["libraries"].items():
        for path in paths:
            # An event outside a selected library cannot cause a broad scan.
            if any(path.is_relative_to(_absolute(root)) for root in roots):
                response = client.request(
                    "GET",
                    f"/library/sections/{key}/refresh",
                    params={"path": str(path)},
                )
                response.raise_for_status()
                count += 1
    return count


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return 1
    path = Path(args[0])
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > 65536
            or path.stat().st_mode & 0o077
        ):
            return 1
        config = json.loads(path.read_text())
        refresh_changed_paths(config, args[1:])
        return 0
    except Exception:
        # Vendor event logs may be public: never print private paths, tokens,
        # upstream response bodies or configuration parser exceptions.
        return 1


if __name__ == "__main__":
    os._exit(main())
