from api.api_state import APIState
from utils.metrics import MetricsCollector
from utils.processes import ProcessHandler
from logging import Logger
from pathlib import Path
import shlex
from api.connection_manager import ConnectionManager
from fastapi import Depends, WebSocket, WebSocketException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional

_shared_instances = {}


def initialize_dependencies(
    process_handler, updater, websocket_manager, metrics_manager, status_manager, logger
):
    _shared_instances["process_handler"] = process_handler
    _shared_instances["updater"] = updater
    _shared_instances["websocket_manager"] = websocket_manager
    _shared_instances["metrics_manager"] = metrics_manager
    _shared_instances["status_manager"] = status_manager
    _shared_instances["logger"] = logger
    _shared_instances["api_state"] = APIState(
        process_handler=process_handler, logger=logger
    )
    _shared_instances["metrics_collector"] = MetricsCollector(
        process_handler=process_handler, logger=logger
    )


def get_process_handler() -> ProcessHandler:
    return _shared_instances["process_handler"]


def get_updater() -> object:
    return _shared_instances["updater"]


def get_websocket_manager() -> ConnectionManager:
    return _shared_instances["websocket_manager"]


def get_metrics_manager() -> ConnectionManager:
    return _shared_instances["metrics_manager"]


def get_status_manager() -> ConnectionManager:
    return _shared_instances["status_manager"]


def get_logger() -> Logger:
    return _shared_instances["logger"]


def get_api_state() -> APIState:
    return _shared_instances["api_state"]


def get_metrics_collector() -> MetricsCollector:
    return _shared_instances["metrics_collector"]


def resolve_path(path_str: str) -> Path:
    path_str = path_str.strip()

    if any(c in path_str for c in ["\\", '"', "'"]):
        try:
            parts = shlex.split(path_str)
            return Path(parts[0]) if parts else Path(path_str)
        except Exception:
            pass

    return Path(path_str)


def get_optional_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False))
) -> Optional[str]:
    """
    Optional authentication dependency.
    Returns username if authenticated, None if not authenticated or auth is disabled.
    Raises HTTPException only if auth is enabled and an invalid token is provided.

    Use this for endpoints that should work with or without authentication enabled.
    """
    from utils.auth_config import AuthConfigManager
    from utils.auth import decode_token
    from fastapi import HTTPException

    auth_config = AuthConfigManager()

    # If auth is not enabled, allow all requests
    if not auth_config.is_auth_enabled():
        return None

    # Auth is enabled but no credentials provided - require auth
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    payload = decode_token(token)

    if not payload or payload.type != "access":
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Verify user still exists and is not disabled
    user = auth_config.get_user(payload.sub)
    if not user or user.disabled:
        raise HTTPException(
            status_code=401,
            detail="User account is disabled or does not exist",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return payload.sub


async def get_websocket_current_user(websocket: WebSocket) -> Optional[str]:
    """
    WebSocket authentication dependency.
    Checks for token in query parameters (?token=xxx).
    Returns username if authenticated, None if auth is disabled.
    Closes WebSocket connection if auth is enabled but token is invalid.
    """
    from utils.auth_config import AuthConfigManager
    from utils.auth import decode_token

    auth_config = AuthConfigManager()

    # If auth is not enabled, allow all websocket connections
    if not auth_config.is_auth_enabled():
        return None

    # Auth is enabled, check for token in query params
    token = websocket.query_params.get("token")
    if not token:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Authentication required")

    payload = decode_token(token)

    if not payload or payload.type != "access":
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired token")

    # Verify user still exists and is not disabled
    user = auth_config.get_user(payload.sub)
    if not user or user.disabled:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="User account is disabled")

    return payload.sub
