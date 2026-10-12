#!/usr/bin/env python3
"""Probe orphan reaping and child wait ownership below the container init."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ORPHAN_CHILD = """
import os
import time

spawner_pid = os.getpid()
pid = os.fork()
if pid:
    print(pid, flush=True)
    os._exit(19)
deadline = time.monotonic() + 5
while os.getppid() == spawner_pid and time.monotonic() < deadline:
    time.sleep(0.001)
print(f"adopted:{os.getppid()}", flush=True)
os.close(1)
os.close(2)
time.sleep(0.025)
os._exit(0)
"""


def check_init(*, iterations: int = 16, require_pid_one: bool = False) -> dict:
    parent_pid = os.getppid()
    parent_name = Path(f"/proc/{parent_pid}/comm").read_text().strip()
    if parent_name not in {"tini", "docker-init"}:
        raise RuntimeError("Run this probe directly under Tini or Docker --init")
    if require_pid_one and parent_pid != 1:
        raise RuntimeError("The probe's init parent must run as PID 1")

    orphan_pids = []
    unexpected_adopters = []
    for _ in range(iterations):
        child = subprocess.run(
            [sys.executable, "-B", "-c", ORPHAN_CHILD],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if child.returncode != 19:
            raise RuntimeError(f"Spawner exit code was lost: {child.returncode}")
        pid_line, adoption_line = child.stdout.strip().splitlines()
        orphan_pids.append(int(pid_line))
        adopter = int(adoption_line.removeprefix("adopted:"))
        if adopter != parent_pid:
            unexpected_adopters.append(adopter)

    # An adopted descendant belongs to init, so the controller must never wait
    # on it. Only inspect disappearance, allowing init's asynchronous reaping.
    deadline = time.monotonic() + 5
    remaining = orphan_pids
    while remaining and time.monotonic() < deadline:
        remaining = [pid for pid in remaining if Path(f"/proc/{pid}").exists()]
        if remaining:
            time.sleep(0.01)
    if remaining:
        raise RuntimeError(f"Init failed to reap {len(remaining)} orphan(s)")

    if unexpected_adopters:
        raise RuntimeError("Orphan adoption bypassed the target init")

    def wait_for_child(exit_code: int) -> int:
        # Service children use a separate session; transient helper children
        # are concurrently owned by their individual subprocess.run callers.
        result = subprocess.run(
            [sys.executable, "-B", "-c", f"raise SystemExit({exit_code})"],
            start_new_session=True,
            capture_output=True,
            timeout=5,
        )
        return result.returncode

    expected_codes = [0, 7, 19, 31] * 4
    with ThreadPoolExecutor(max_workers=4) as executor:
        actual_codes = list(executor.map(wait_for_child, expected_codes))
    if actual_codes != expected_codes:
        raise RuntimeError(f"Child exit statuses were lost: {actual_codes}")
    return {
        "init_parent": parent_name,
        "parent_is_pid_one": parent_pid == 1,
        "orphans_reaped": len(orphan_pids),
        "adopted_by_target_init": True,
        "child_exit_codes": actual_codes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=16)
    parser.add_argument("--require-pid-one", action="store_true")
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    print(
        json.dumps(
            check_init(
                iterations=args.iterations,
                require_pid_one=args.require_pid_one,
            )
        )
    )


if __name__ == "__main__":
    main()
