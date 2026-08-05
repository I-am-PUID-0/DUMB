#!/usr/bin/env python3
"""Validate credential-free install phases inside a disposable DUMB container."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from utils.config_loader import CONFIG_MANAGER
from utils.setup import zurg_setup


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_zurg() -> dict:
    instance = CONFIG_MANAGER.get("zurg")["instances"]["RealDebrid"]
    binary = Path(instance["config_dir"]) / "zurg"

    success, error = zurg_setup(install_only=True)
    if not success:
        raise RuntimeError(f"Prior Zurg install failed: {error}")
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError("Prior Zurg binary is missing or not executable")
    prior_digest = _sha256(binary)
    subprocess.run(["readelf", "-h", str(binary)], check=True, capture_output=True)

    binary.unlink()
    instance["release_version_enabled"] = False
    instance["release_version"] = "latest"
    success, error = zurg_setup(install_only=True)
    if not success:
        raise RuntimeError(f"Latest Zurg install failed: {error}")
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError("Latest Zurg binary is missing or not executable")
    latest_digest = _sha256(binary)
    subprocess.run(["readelf", "-h", str(binary)], check=True, capture_output=True)
    if prior_digest == latest_digest:
        raise RuntimeError("Prior and latest Zurg binaries have the same digest")

    return {
        "service_key": "zurg",
        "validation_mode": "install_only",
        "prior_sha256": prior_digest,
        "latest_sha256": latest_digest,
        "result": "passed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", choices=("zurg",), required=True)
    args = parser.parse_args()
    result = validate_zurg() if args.key == "zurg" else None
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
