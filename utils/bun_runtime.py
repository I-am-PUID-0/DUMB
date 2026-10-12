"""Provision the shared Bun runtime without replacing an unverified executable."""

import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from utils.global_logger import logger
from utils.user_management import chown_recursive

BUN_RUNTIME_VERSION = "1.4.3"
_BUN_RUNTIME_LOCK = threading.Lock()


def _bun_version(binary, env):
    """Return a released version; missing, broken, and prerelease runtimes need repair."""
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:\+[\w.-]+)?", result.stdout.strip())
    return tuple(map(int, match.groups())) if match else None


def ensure_bun_runtime(process_handler, config_dir, env, user_id, group_id):
    """Upgrade old persistent runtimes, retaining newer versions and running processes."""
    install_dir = Path(env["BUN_INSTALL"])
    binary = install_dir / "bin" / "bun"
    required = tuple(map(int, BUN_RUNTIME_VERSION.split(".")))
    with _BUN_RUNTIME_LOCK:
        current = _bun_version(binary, env) if binary.is_file() else None
        if current is not None and current >= required:
            return True, None
        logger.info(
            "Installing Bun %s in %s (existing version: %s).",
            BUN_RUNTIME_VERSION,
            install_dir,
            ".".join(map(str, current)) if current else "missing or unreadable",
        )
        # Stage on the same filesystem, validate, then rename. Existing services
        # retain their executable, and a failed download leaves the old binary intact.
        with tempfile.TemporaryDirectory(
            prefix=".dumb-bun-", dir=install_dir
        ) as staging:
            chown_recursive(staging, user_id, group_id)
            install_env = env.copy()
            install_env["BUN_INSTALL"] = staging
            install_env["HOME"] = staging
            success, error = process_handler.start_process(
                "bun_install_runtime",
                config_dir,
                [
                    "bash",
                    "-o",
                    "pipefail",
                    "-c",
                    'curl -fsSL https://bun.sh/install | bash -s -- "$1"',
                    "dumb-bun-install",
                    f"bun-v{BUN_RUNTIME_VERSION}",
                ],
                env=install_env,
            )
            if not success:
                return False, f"Error starting Bun installation: {error}"
            process_handler.wait("bun_install_runtime")
            if process_handler.returncode != 0:
                detail = process_handler.stderr or process_handler.stdout
                return False, f"Error installing Bun {BUN_RUNTIME_VERSION}: {detail}"
            candidate = Path(staging) / "bin" / "bun"
            installed = _bun_version(candidate, install_env)
            if installed != required:
                actual = ".".join(map(str, installed)) if installed else "unreadable"
                return False, (
                    f"Bun runtime validation failed: expected {BUN_RUNTIME_VERSION}, "
                    f"found {actual}. Existing runtime was preserved."
                )
            binary.parent.mkdir(parents=True, exist_ok=True)
            chown_recursive(staging, user_id, group_id)
            os.replace(candidate, binary)
        return True, None
