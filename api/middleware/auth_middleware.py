from fastapi import Request, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
from utils.auth import decode_token
from utils.auth_config import AuthConfigManager


security = HTTPBearer(auto_error=False)
auth_config = AuthConfigManager()


async def get_current_user(
    request: Request, credentials: Optional[HTTPAuthorizationCredentials] = None
) -> Optional[str]:
    """
    Extract and validate the current user from the Authorization header.
    Returns username if valid, None if auth is disabled, raises HTTPException if auth fails.

    Args:
        request: The FastAPI request object
        credentials: HTTP Authorization credentials from Bearer token

    Returns:
        Username if authenticated, None if auth is disabled

    Raises:
        HTTPException: 401 if auth is enabled but token is missing or invalid
    """
    # If auth is not enabled, allow all requests
    if not auth_config.is_auth_enabled():
        return None

    # Auth is enabled, so require a valid token
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    payload = decode_token(token)

    if not payload or payload.type != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Verify user still exists and is not disabled
    user = auth_config.get_user(payload.sub)
    if not user or user.disabled:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is disabled or does not exist",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return payload.sub


def require_auth(username: Optional[str] = None):
    """
    Dependency to require authentication on a route.
    Use this with FastAPI's Depends() to protect specific endpoints.

    Args:
        username: The authenticated username (injected by get_current_user)

    Raises:
        HTTPException: 401 if not authenticated and auth is enabled
    """
    # If auth is enabled and we got here without a username, something went wrong
    if auth_config.is_auth_enabled() and username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )
    return username
