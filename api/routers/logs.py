from fastapi import APIRouter, Depends, Query
from pathlib import Path
from pydantic import BaseModel
from typing import Optional
from utils.dependencies import get_logger, resolve_path, get_optional_current_user
from utils.config_loader import CONFIG_MANAGER
import os, re, asyncio

logs_router = APIRouter()


class LogFileResponse(BaseModel):
    process_name: str
    size: int
    cursor: int
    chunk: str
    reset: bool
    log: Optional[str] = None


def find_log_file(process_name: str, logger):
    logger.debug(f"Looking up process: {process_name}")

    if process_name.lower() in {"plex dbrepair", "dbrepair"}:
        plex_cfg = CONFIG_MANAGER.get("plex", {}) or {}
        log_file = plex_cfg.get("dbrepair", {}).get("log_file")
        if log_file:
            return resolve_path(log_file)

    if "dumb" in process_name.lower() or "dmb" in process_name.lower():
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


def _read_chunk(path: Path, start: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(max(0, start))
        return f.read()


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
            }

        size = log_path.stat().st_size

        # Initial load (no cursor): for DUMB/DMB return from the last startup banner once,
        # otherwise a tail slice. Mark as reset so the client replaces its buffer.
        if cursor is None:
            if "dumb" in process_name.lower() or "dmb" in process_name.lower():
                text = filter_dumb_log(log_path, logger)
                # After initial snapshot, cursor should point to EOF
                return {
                    "process_name": process_name,
                    "size": size,
                    "cursor": size,
                    "chunk": text,
                    "reset": True,
                }
            start = max(0, size - int(tail_bytes))
            data = _read_chunk(log_path, start)
            return {
                "process_name": process_name,
                "size": size,
                "cursor": size,
                "chunk": data.decode("utf-8", "replace"),
                "reset": True,
            }

        # Incremental load: handle truncation/rotation
        if cursor > size:
            # file rotated/truncated; tail fresh bytes
            start = max(0, size - int(tail_bytes))
            data = _read_chunk(log_path, start)
            return {
                "process_name": process_name,
                "size": size,
                "cursor": size,
                "chunk": data.decode("utf-8", "replace"),
                "reset": True,
            }

        # Normal delta
        data = _read_chunk(log_path, cursor)
        new_cursor = cursor + len(data)
        return {
            "process_name": process_name,
            "size": size,
            "cursor": new_cursor,
            "chunk": data.decode("utf-8", "replace"),
            "reset": False,
        }

    result = await loop.run_in_executor(None, work)

    # Back-compat for any callers expecting {log: "..."} on first fetch
    if result.get("reset"):
        result["log"] = result.get("chunk", "")

    return result
