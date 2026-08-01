from fastapi import APIRouter, Depends, Query
from pathlib import Path
from pydantic import BaseModel
from typing import Optional
from utils.dependencies import get_logger, resolve_path, get_optional_current_user
from utils.logger import redact_sensitive_log_data
from utils.config_loader import CONFIG_MANAGER
import os, re, asyncio

logs_router = APIRouter()

DUMB_API_PROCESS_NAMES = {
    "dmb",
    "dmb api",
    "dmb api service",
    "dumb",
    "dumb api",
    "dumb api service",
}


class LogFileResponse(BaseModel):
    process_name: str
    size: int
    cursor: int
    chunk: str
    reset: bool
    file_id: Optional[str] = None
    log: Optional[str] = None


def _is_dumb_api_process(process_name: str) -> bool:
    normalized = " ".join(str(process_name or "").lower().replace("_", " ").split())
    return normalized in DUMB_API_PROCESS_NAMES


def find_log_file(process_name: str, logger):
    logger.debug(f"Looking up process: {process_name}")

    if process_name.lower() in {"plex dbrepair", "dbrepair"}:
        plex_cfg = CONFIG_MANAGER.get("plex", {}) or {}
        log_file = plex_cfg.get("dbrepair", {}).get("log_file")
        if log_file:
            return resolve_path(log_file)

    if _is_dumb_api_process(process_name):
        log_dir = resolve_path("/log")
        if log_dir.exists():
            log_files = sorted(
                log_dir.glob("DUMB-*.log"), key=os.path.getmtime, reverse=True
            )
            return log_files[0] if log_files else None

    if process_name.lower() == "traefik":
        traefik_log = resolve_path("/log/traefik.log")
        if traefik_log.exists():
            return traefik_log

    if process_name.lower() in {"traefik access", "traefik_access"}:
        access_log = resolve_path("/log/traefik_access.log")
        if access_log.exists():
            return access_log

    key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
    logger.debug(f"Found key: {key}, instance: {instance_name}")
    if not key:
        logger.debug(f"No log file found for {process_name}")
        return None

    service_config = CONFIG_MANAGER.get_instance(instance_name, key)
    if not service_config:
        logger.debug(f"No service config found for {process_name}")
        return None

    if "log_file" in service_config:
        return resolve_path(service_config["log_file"])

    if "config_file" in service_config:
        log_dir = resolve_path(service_config["config_file"]).parent / "logs"
        if log_dir.exists():
            log_files = sorted(
                log_dir.glob("*.log"), key=os.path.getmtime, reverse=True
            )
            return log_files[0] if log_files else None

    if "config_dir" in service_config:
        log_dir = resolve_path(service_config["config_dir"]) / "logs"
        if log_dir.exists():
            log_files = sorted(
                log_dir.glob("*.log"), key=os.path.getmtime, reverse=True
            )
            return log_files[0] if log_files else None

    if "zurg" in process_name.lower() and "config_dir" in service_config:
        log_path = resolve_path(service_config["config_dir"]) / "logs" / "zurg.log"
        if log_path.exists():
            return log_path

    logger.debug(f"No log file found for {process_name}")
    return None


def filter_dumb_log(log_path, logger):
    logger.debug(f"Filtering DUMB log for latest startup from {log_path}")
    try:
        with open(log_path, "r") as log_file:
            lines = log_file.readlines()

        for i in range(len(lines) - 1, -1, -1):
            if i + 2 < len(lines):
                try:
                    if re.match(r"^.* - INFO - ", lines[i]) and re.match(
                        r"^\s*DDDDDDDDDDDDD", lines[i + 2]
                    ):
                        logger.debug(f"Found latest DUMB startup banner at line {i}")
                        return "".join(lines[i:])
                except Exception as e:
                    logger.warning(f"Error matching log lines at index {i}: {e}")

        logger.warning("No DUMB startup banner found; returning full log")
        return "".join(lines)

    except Exception as e:
        logger.error(f"Error filtering DUMB log file: {e}")
        return ""


def _read_complete_chunk_from_handle(f, start: int, snapshot_size: int):
    safe_start = max(0, start)
    if safe_start:
        f.seek(safe_start - 1)
        if f.read(1) not in {b"\n", b"\r"}:
            # An arbitrary/tail cursor can land inside a sensitive line.
            # Skip that fragment rather than returning an unredactable tail.
            f.readline(max(0, snapshot_size - safe_start))
            safe_start = f.tell()

    f.seek(safe_start)
    data = f.read(max(0, snapshot_size - safe_start))

    final_newline = data.rfind(b"\n")
    if final_newline < 0:
        return b"", safe_start
    complete = data[: final_newline + 1]
    return complete, safe_start + len(complete)


def _read_complete_chunk(
    path: Path, start: int, snapshot_size: int | None = None
) -> tuple[bytes, int]:
    """Read complete log lines from a fixed-size file snapshot."""

    with open(path, "rb") as f:
        if snapshot_size is None:
            snapshot_size = os.fstat(f.fileno()).st_size
        return _read_complete_chunk_from_handle(f, start, snapshot_size)


def _bounded_log_start(size: int, cursor: int, tail_bytes: int) -> tuple[int, bool]:
    """Return a bounded read offset and whether the client buffer must reset."""

    reset = cursor > size or size - cursor > tail_bytes
    if reset:
        return max(0, size - tail_bytes), True
    return cursor, False


def _log_file_id(stat_result: os.stat_result) -> str:
    """Return an opaque generation identifier for one opened log file."""

    return f"{stat_result.st_dev:x}:{stat_result.st_ino:x}"


def _read_log_snapshot(
    path: Path,
    cursor: int | None,
    tail_bytes: int,
    expected_file_id: str | None = None,
) -> tuple[bytes, int, int, str, bool]:
    """Read a bounded snapshot and detect rotation by opened-file identity."""

    with open(path, "rb") as f:
        snapshot_stat = os.fstat(f.fileno())
        size = snapshot_stat.st_size
        file_id = _log_file_id(snapshot_stat)

        if cursor is None:
            start = max(0, size - tail_bytes)
            reset = True
        elif expected_file_id is not None and expected_file_id != file_id:
            start = max(0, size - tail_bytes)
            reset = True
        else:
            start, reset = _bounded_log_start(size, cursor, tail_bytes)

        data, safe_cursor = _read_complete_chunk_from_handle(f, start, size)
        return data, safe_cursor, size, file_id, reset


@logs_router.get("", response_model=LogFileResponse)
async def get_log_file(
    process_name: str = Query(..., description="The process name"),
    cursor: int | None = Query(
        None, description="Last byte offset the client has read"
    ),
    tail_bytes: int = Query(
        131072,
        ge=1024,
        le=8_388_608,
        description="Initial bytes from end when no cursor",
    ),
    file_id: str | None = Query(
        None,
        max_length=128,
        description="Opaque file generation returned by the previous request",
    ),
    logger=Depends(get_logger),
    current_user: str = Depends(get_optional_current_user),
):
    loop = asyncio.get_running_loop()

    def work():
        log_path = find_log_file(process_name, logger)
        if not log_path or not log_path.exists():
            return {
                "process_name": process_name,
                "size": 0,
                "cursor": 0,
                "chunk": "",
                "reset": True,
                "file_id": None,
            }

        # Initial load (no cursor): for DUMB/DMB return from the last startup banner once,
        # otherwise a tail slice. Mark as reset so the client replaces its buffer.
        if cursor is None and _is_dumb_api_process(process_name):
            snapshot_stat = log_path.stat()
            size = snapshot_stat.st_size
            text = redact_sensitive_log_data(filter_dumb_log(log_path, logger))
            return {
                "process_name": process_name,
                "size": size,
                "cursor": size,
                "chunk": text,
                "reset": True,
                "file_id": _log_file_id(snapshot_stat),
            }

        data, new_cursor, size, current_file_id, reset = _read_log_snapshot(
            log_path,
            cursor,
            int(tail_bytes),
            expected_file_id=file_id,
        )
        return {
            "process_name": process_name,
            "size": size,
            "cursor": new_cursor,
            "chunk": redact_sensitive_log_data(data.decode("utf-8", "replace")),
            "reset": reset,
            "file_id": current_file_id,
        }

    result = await loop.run_in_executor(None, work)

    # Back-compat for callers expecting {log: "..."} on their first fetch.
    # Incremental rotation resets already use the cursor protocol; duplicating
    # a large tail there makes the browser decode the same payload twice.
    if cursor is None:
        result["log"] = result.get("chunk", "")

    return result
