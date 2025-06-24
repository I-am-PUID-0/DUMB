from fastapi import APIRouter, Depends, Query
from pathlib import Path
from utils.dependencies import get_logger, resolve_path
from utils.config_loader import CONFIG_MANAGER
import os, re, asyncio

logs_router = APIRouter()


def find_log_file(process_name: str, logger):
    logger.debug(f"Looking up process: {process_name}")

    if "dumb" in process_name.lower() or "dmb" in process_name.lower():
        log_dir = resolve_path("/log")
        if log_dir.exists():
            log_files = sorted(
                log_dir.glob("DUMB-*.log"), key=os.path.getmtime, reverse=True
            )
            return log_files[0] if log_files else None

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


def _read_log_for_process(process_name: str, logger):
    log_path = find_log_file(process_name, logger)
    logger.debug(f"Resolved log path: {log_path}")
    if not log_path or not log_path.exists():
        return ""

    try:
        if "dumb" in process_name.lower() or "dmb" in process_name.lower():
            return filter_dumb_log(log_path, logger)
        else:
            with open(log_path, "r") as log_file:
                return log_file.read()
    except Exception as e:
        logger.error(f"Error reading log file for {process_name}: {e}")
        return ""


@logs_router.get("")
async def get_log_file(
    process_name: str = Query(..., description="The process name"),
    logger=Depends(get_logger),
):
    loop = asyncio.get_running_loop()
    log_content = await loop.run_in_executor(
        None, lambda: _read_log_for_process(process_name, logger)
    )

    return {
        "process_name": process_name,
        "log": log_content,
    }
