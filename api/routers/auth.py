from fastapi import APIRouter, HTTPException, status, Depends
from pydantic import BaseModel
from typing import Optional
from utils.auth import create_token_pair, decode_token, TokenResponse
from utils.auth_config import AuthConfigManager
from utils.dependencies import get_logger, get_optional_current_user


auth_router = APIRouter()

# Global auth config manager instance
AUTH_CONFIG = AuthConfigManager()


class LoginRequest(BaseModel):
    """Login request body"""

    username: str
    password: str


class RefreshRequest(BaseModel):
    """Token refresh request body"""

    refresh_token: str


class VerifyResponse(BaseModel):
    """Token verification response"""

    valid: bool
    username: Optional[str] = None


class AuthStatusResponse(BaseModel):
    """Auth status response"""

    enabled: bool
    has_users: bool
    setup_skipped: bool = False


class InitialSetupRequest(BaseModel):
    """Initial setup request for creating first user"""

    username: str
    password: str


class UserCreateRequest(BaseModel):
    """User creation request"""

    username: str
    password: str


class UserUpdateRequest(BaseModel):
    """User update request"""

    disabled: bool


class UserResponse(BaseModel):
    """User response (without password)"""

    username: str
    disabled: bool


class UsersListResponse(BaseModel):
    """List of users response"""

    users: list[UserResponse]


@auth_router.post("/login", response_model=TokenResponse)
def login(request: LoginRequest, logger=Depends(get_logger)):
    """
    Authenticate user and return access and refresh tokens.

    Args:
        request: Login credentials

    Returns:
        TokenResponse with access and refresh tokens

    Raises:
        HTTPException: 401 if authentication fails or auth is disabled
    """
    if not AUTH_CONFIG.is_auth_enabled():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is not enabled",
        )

    user = AUTH_CONFIG.authenticate_user(request.username, request.password)

    if not user:
        logger.warning(f"Failed login attempt for username: {request.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )

    logger.info(f"User {request.username} logged in successfully")
    return create_token_pair(request.username)


@auth_router.post("/refresh", response_model=TokenResponse)
def refresh_token(request: RefreshRequest, logger=Depends(get_logger)):
    """
    Refresh an access token using a valid refresh token.

    Args:
        request: Refresh token

    Returns:
        TokenResponse with new access and refresh tokens

    Raises:
        HTTPException: 401 if refresh token is invalid or expired
    """
    if not AUTH_CONFIG.is_auth_enabled():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is not enabled",
        )

    payload = decode_token(request.refresh_token)

    if not payload or payload.type != "refresh":
        logger.warning("Invalid or expired refresh token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    # Verify user still exists and is not disabled
    user = AUTH_CONFIG.get_user(payload.sub)
    if not user or user.disabled:
        logger.warning(f"Token refresh failed for disabled/missing user: {payload.sub}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is disabled or does not exist",
        )

    logger.info(f"Token refreshed for user: {payload.sub}")
    return create_token_pair(payload.sub)


@auth_router.post("/verify", response_model=VerifyResponse)
def verify_token(token: str):
    """
    Verify if an access token is valid.

    Args:
        token: JWT access token to verify

    Returns:
        VerifyResponse with validation result and username if valid
    """
    if not AUTH_CONFIG.is_auth_enabled():
        return VerifyResponse(valid=False)

    payload = decode_token(token)

    if not payload or payload.type != "access":
        return VerifyResponse(valid=False)

    # Verify user still exists and is not disabled
    user = AUTH_CONFIG.get_user(payload.sub)
    if not user or user.disabled:
        return VerifyResponse(valid=False)

    return VerifyResponse(valid=True, username=payload.sub)


@auth_router.get("/status", response_model=AuthStatusResponse)
def get_auth_status():
    """
    Get authentication status (enabled/disabled and if users exist).

    Returns:
        AuthStatusResponse with current auth status
    """
    return AuthStatusResponse(
        enabled=AUTH_CONFIG.is_auth_enabled(),
        has_users=len(AUTH_CONFIG.config.users) > 0,
        setup_skipped=AUTH_CONFIG.is_setup_skipped(),
    )


@auth_router.post("/skip-setup")
def skip_auth_setup(logger=Depends(get_logger)):
    """
    Skip authentication setup and continue without auth.
    This endpoint only works if no users exist yet.

    Returns:
        Success message

    Raises:
        HTTPException: 400 if users already exist
    """
    if len(AUTH_CONFIG.config.users) > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot skip setup - users already exist. Use the disable endpoint instead.",
        )

    # Ensure auth remains disabled and mark that setup was explicitly skipped
    AUTH_CONFIG.disable_auth()
    AUTH_CONFIG.mark_setup_skipped()
    logger.info("Auth setup skipped - user chose to continue without authentication")

    return {"message": "Authentication setup skipped successfully"}


@auth_router.post("/enable")
def enable_auth(
    current_user: str = Depends(get_optional_current_user),
    logger=Depends(get_logger),
):
    """
    Enable authentication for the system.
    Requires at least one user to exist.

    Returns:
        Success message

    Raises:
        HTTPException: 400 if no users exist
    """
    if len(AUTH_CONFIG.config.users) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot enable authentication - no users exist. Create a user first.",
        )

    AUTH_CONFIG.enable_auth()
    logger.info(
        f"Authentication enabled by user: {current_user if current_user else 'system'}"
    )

    return {"message": "Authentication enabled successfully"}


@auth_router.post("/disable")
def disable_auth(
    current_user: str = Depends(get_optional_current_user),
    logger=Depends(get_logger),
):
    """
    Disable authentication for the system.
    Requires authentication if auth is currently enabled.

    Returns:
        Success message

    Raises:
        HTTPException: 401 if not authenticated (when auth is enabled)
    """
    if AUTH_CONFIG.is_auth_enabled() and not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required to disable auth",
        )

    AUTH_CONFIG.disable_auth()
    logger.warning(
        f"Authentication disabled by user: {current_user if current_user else 'system'}"
    )

    return {"message": "Authentication disabled successfully"}


@auth_router.post("/setup", response_model=TokenResponse)
def initial_setup(request: InitialSetupRequest, logger=Depends(get_logger)):
    """
    Create the first user account and enable authentication.
    This endpoint only works if no users exist yet.

    Args:
        request: Initial setup credentials

    Returns:
        TokenResponse with access and refresh tokens

    Raises:
        HTTPException: 400 if users already exist or setup fails
    """
    # Check if users already exist
    if len(AUTH_CONFIG.config.users) > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Users already exist. Use the login endpoint instead.",
        )

    # Validate username and password
    if len(request.username) < 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username must be at least 3 characters long",
        )

    if len(request.password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters long",
        )

    if len(request.password) > 72:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password cannot be longer than 72 characters",
        )

    # Create the first user
    success = AUTH_CONFIG.add_user(request.username, request.password, disabled=False)

    if not success:
        logger.error(f"Failed to create initial user: {request.username}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create user account",
        )

    # Enable authentication
    AUTH_CONFIG.enable_auth()

    logger.info(f"Initial setup completed. First user created: {request.username}")

    # Return tokens for immediate login
    return create_token_pair(request.username)


@auth_router.get("/users", response_model=UsersListResponse)
def list_users(
    current_user: str = Depends(get_optional_current_user), logger=Depends(get_logger)
):
    """
    List all users.
    - If auth is enabled: requires authentication
    - If auth is disabled: allows listing users without auth (for setup purposes)

    Returns:
        UsersListResponse with list of all users

    Raises:
        HTTPException: 401 if authentication required but not provided
    """
    if AUTH_CONFIG.is_auth_enabled():
        # Auth is enabled - require authentication
        if not current_user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
            )

    users = [
        UserResponse(username=user.username, disabled=user.disabled)
        for user in AUTH_CONFIG.config.users
    ]

    return UsersListResponse(users=users)


@auth_router.post("/users", response_model=UserResponse)
def create_user(
    request: UserCreateRequest,
    current_user: str = Depends(get_optional_current_user),
    logger=Depends(get_logger),
):
    """
    Create a new user.
    - If auth is enabled: requires authentication
    - If auth is disabled and no users exist: allows creating first user without auth

    Args:
        request: User creation details

    Returns:
        UserResponse with created user details

    Raises:
        HTTPException: 400 if validation fails, 401 if not authenticated when required
    """
    # Allow creating first user when auth is disabled and no users exist
    is_first_user = not AUTH_CONFIG.is_auth_enabled() and len(AUTH_CONFIG.config.users) == 0

    if AUTH_CONFIG.is_auth_enabled():
        # Auth is enabled - require authentication
        if not current_user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
            )

    # Validate username and password
    if len(request.username) < 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username must be at least 3 characters long",
        )

    if len(request.password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters long",
        )

    if len(request.password) > 72:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password cannot be longer than 72 characters",
        )

    # Check if user already exists
    if AUTH_CONFIG.get_user(request.username):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Username already exists"
        )

    # Create the user
    success = AUTH_CONFIG.add_user(request.username, request.password, disabled=False)

    if not success:
        logger.error(f"Failed to create user: {request.username}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create user account",
        )

    if is_first_user:
        logger.info(f"First user created: {request.username} (auth was disabled)")
        # Clear setup_skipped flag and enable auth now that a user exists
        AUTH_CONFIG.config.setup_skipped = False
        AUTH_CONFIG.enable_auth()
        logger.info("Authentication automatically enabled after first user creation")
    else:
        logger.info(f"User created: {request.username} by {current_user}")

    return UserResponse(username=request.username, disabled=False)


@auth_router.put("/users/{username}", response_model=UserResponse)
def update_user(
    username: str,
    request: UserUpdateRequest,
    current_user: str = Depends(get_optional_current_user),
    logger=Depends(get_logger),
):
    """
    Update a user (authentication required).

    Args:
        username: Username to update
        request: User update details

    Returns:
        UserResponse with updated user details

    Raises:
        HTTPException: 400 if user not found, 401 if not authenticated
    """
    if not AUTH_CONFIG.is_auth_enabled():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is not enabled",
        )

    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )

    # Get the user
    user = AUTH_CONFIG.get_user(username)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="User not found"
        )

    # Prevent disabling the last active user
    if request.disabled and not user.disabled:
        # Count active users (non-disabled)
        active_users = [u for u in AUTH_CONFIG.config.users if not u.disabled]
        logger.info(f"Attempting to disable user {username}. Active users count: {len(active_users)}")
        if len(active_users) <= 1:
            logger.warning(f"Blocked attempt to disable last active user: {username}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot disable the last active user account. This would lock you out of the system.",
            )

    # Update the user
    user.disabled = request.disabled
    AUTH_CONFIG.save_config()

    logger.info(f"User updated: {username} by {current_user}")

    return UserResponse(username=user.username, disabled=user.disabled)


@auth_router.delete("/users/{username}")
def delete_user(
    username: str,
    current_user: str = Depends(get_optional_current_user),
    logger=Depends(get_logger),
):
    """
    Delete a user (authentication required).

    Args:
        username: Username to delete

    Returns:
        Success message

    Raises:
        HTTPException: 400 if validation fails, 401 if not authenticated
    """
    if not AUTH_CONFIG.is_auth_enabled():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is not enabled",
        )

    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )

    # Prevent deleting yourself
    if username == current_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )

    # Check if user exists
    user = AUTH_CONFIG.get_user(username)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="User not found"
        )

    # Prevent deleting the last user
    if len(AUTH_CONFIG.config.users) <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete the last user",
        )

    # Delete the user
    AUTH_CONFIG.config.users = [
        u for u in AUTH_CONFIG.config.users if u.username != username
    ]
    AUTH_CONFIG.save_config()

    logger.info(f"User deleted: {username} by {current_user}")

    return {"message": "User deleted successfully"}
