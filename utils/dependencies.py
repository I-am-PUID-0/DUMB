from api.api_state import APIState
from utils.processes import ProcessHandler
from logging import Logger
from pathlib import Path
import shlex
from api.connection_manager import ConnectionManager

_shared_instances = {}


def initialize_dependencies(process_handler, updater, websocket_manager, logger):
    _shared_instances["process_handler"] = process_handler
    _shared_instances["updater"] = updater
    _shared_instances["websocket_manager"] = websocket_manager
    _shared_instances["logger"] = logger
    _shared_instances["api_state"] = APIState(
        process_handler=process_handler, logger=logger
    )


def get_process_handler() -> ProcessHandler:
    return _shared_instances["process_handler"]


def get_updater() -> object:
    return _shared_instances["updater"]


def get_websocket_manager() -> ConnectionManager:
    return _shared_instances["websocket_manager"]


def get_logger() -> Logger:
    return _shared_instances["logger"]


def get_api_state() -> APIState:
    return _shared_instances["api_state"]


def resolve_path(path_str: str) -> Path:
    path_str = path_str.strip()

    if any(c in path_str for c in ["\\", '"', "'"]):
        try:
            parts = shlex.split(path_str)
            return Path(parts[0]) if parts else Path(path_str)
        except Exception:
            pass

    return Path(path_str)
