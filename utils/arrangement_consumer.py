"""Explicit, bounded checks from a running managed consumer's actual namespace."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time

from utils.arrangement_probe_child import start_time

_SLOTS = threading.BoundedSemaphore(2)
_REAP = []
_REAP_LOCK = threading.Lock()


def schema_fields(resource, config, payload=None):
    del config, payload
    if resource.get("external"):
        return []
    return [
        {
            "key": f"{resource['id']}::consumer_paths",
            "resource_id": resource["id"],
            "service_key": resource["service_key"],
            "type": "list",
            "label": "Verify paths as this running service",
            "description": "Optional read-only verification in the service's mount namespace and effective UID/groups. Remote access can contact storage; choose an exact sample to test content. No scans or writes are performed.",
            "max_items": 20,
            "item_fields": [
                {
                    "key": "path",
                    "type": "string",
                    "label": "Absolute media directory",
                    "required": True,
                },
                {
                    "key": "sample_file",
                    "type": "string",
                    "label": "Relative sample filename (optional)",
                },
                {
                    "key": "allow_remote",
                    "type": "boolean",
                    "label": "Authorize reading this remote mount",
                },
                {
                    "key": "max_bytes",
                    "type": "number",
                    "min": 1,
                    "max": 1048576,
                    "default": 65536,
                    "label": "Maximum sample bytes",
                },
            ],
        }
    ]


def _result(execution, reason, *, verification="not_checked", evidence=None):
    return {
        "execution": execution,
        "verification": verification,
        "reason": reason,
        "retryable": False,
        "evidence": evidence or {},
    }


def _rows(action, payload):
    values = (payload.get("arrangement") or {}).get("values") or {}
    rows = values.get(f"{action['source']}::consumer_paths") or []
    if not isinstance(rows, list) or len(rows) > 20:
        raise ValueError("Choose at most twenty consumer paths.")
    result = []
    for row in rows:
        path = row.get("path") if isinstance(row, dict) else None
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or ".." in path.split("/")
            or path == "/"
            or path.split("/")[1]
            in {"config", "etc", "proc", "sys", "dev", "root", "run"}
        ):
            raise ValueError(
                "Choose absolute media directories outside system and configuration storage."
            )
        sample = row.get("sample_file") or ""
        if (
            not isinstance(sample, str)
            or sample.startswith("/")
            or ".." in sample.split("/")
        ):
            raise ValueError("Use a relative sample-file path without traversal.")
        limit = row.get("max_bytes", 65536)
        if type(limit) is not int or not 1 <= limit <= 1048576:
            raise ValueError("Use a sample budget of 1–1048576 bytes.")
        result.append(
            {
                "path": path,
                "sample_file": sample,
                "max_bytes": limit,
                "allow_remote": row.get("allow_remote") is True,
            }
        )
    return result


def _tracked_pid(handler, process_name):
    matches = [
        int(pid)
        for pid, item in list(getattr(handler, "processes", {}).items())
        if item.get("name") == process_name and item.get("managed_service", True)
    ]
    if len(matches) != 1:
        raise ValueError(
            "Start the selected managed service before checking its namespace."
        )
    return matches[0]


def _reap_finished():
    with _REAP_LOCK:
        _REAP[:] = [child for child in _REAP if child.poll() is None]
        return bool(_REAP)


def verify_consumer(
    action,
    config,
    payload,
    operation,
    *,
    process_handler=None,
    read_only=False,
    timeout=8,
    cancelled=lambda: False,
):
    from utils.arrangement_plan import resource_config

    if action.get("action") != "consumer_paths":
        return None
    try:
        rows = _rows(action, payload)
        if not rows:
            return _result("declined", "No optional consumer path check was selected.")
        cfg = resource_config(config, action["source"])
        pid = _tracked_pid(process_handler, cfg.get("process_name"))
        expected = start_time(pid)
    except (OSError, ValueError) as error:
        return _result("waiting_input", str(error))
    if read_only and operation != "recheck":
        return _result(
            "planned",
            "Review the exact paths and sample budgets, then explicitly run this read-only consumer check.",
            evidence={
                "level": "probe_preview",
                "paths": rows,
                "side_effects": [
                    "Read directory access and optional media samples; no writes or library scans."
                ],
            },
        )
    if operation not in {"apply", "test", "recheck"}:
        return _result(
            "waiting_input",
            "Run this optional check explicitly; background setup never probes remote content.",
        )
    if _reap_finished() or not _SLOTS.acquire(blocking=False):
        return _result(
            "waiting_dependency",
            "A previous bounded consumer probe is still being reaped. No additional child was started.",
        )
    child = None
    try:
        request = json.dumps({"pid": pid, "start_time": expected, "paths": rows})
        child = subprocess.Popen(
            [sys.executable, "-B", "-m", "utils.arrangement_probe_child"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + min(max(float(timeout), 1), 15)
        pending_input = request
        while True:
            if cancelled():
                raise TimeoutError
            try:
                output, _ = child.communicate(
                    input=pending_input,
                    timeout=min(0.2, max(0.01, deadline - time.monotonic())),
                )
                break
            except subprocess.TimeoutExpired:
                pending_input = None
                if time.monotonic() >= deadline:
                    raise TimeoutError
        if child.returncode != 0 or len(output) > 65536 or start_time(pid) != expected:
            return _result(
                "waiting_dependency",
                "The consumer changed or its isolated check did not complete.",
            )
        evidence = json.loads(output)
        passed = (
            evidence.get("namespace_verified") is True
            and len(evidence.get("paths", [])) == len(rows)
            and all(row.get("accessible") is True for row in evidence["paths"])
        )
        return _result(
            "already_present" if passed else "waiting_input",
            (
                "Selected paths were opened as the actual running consumer; sample reads are recorded separately."
                if passed
                else "The running consumer could not verify all selected paths or samples."
            ),
            verification="passed" if passed else "failed",
            evidence={"level": "consumer_namespace", **evidence},
        )
    except (OSError, ValueError, TimeoutError):
        return _result(
            "waiting_dependency",
            "The bounded consumer check was interrupted or timed out; no success is assumed.",
        )
    finally:
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                with _REAP_LOCK:
                    _REAP.append(child)
        if child is not None and child.poll() is not None:
            for stream in (child.stdin, child.stdout):
                if stream is not None:
                    stream.close()
        _SLOTS.release()
