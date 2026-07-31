from ipaddress import ip_address
from typing import Any, Optional
from urllib.parse import quote, urlencode, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from utils.auth import create_token_pair, decode_token, TokenResponse
from utils.auth_config import AuthConfigManager
from utils.dependencies import get_logger, get_optional_current_user
from utils.oidc import OIDCError, OIDC_FLOWS, resolved_endpoints

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
    mode: str = "local"
    local_login_enabled: bool = True
    oidc_login_enabled: bool = False
    oidc_provider_name: str = ""


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


class OIDCProviderRequest(BaseModel):
    mode: str = "hybrid"
    enabled: bool = True
    provider_name: str = "Single Sign-On"
    source: str = "custom_oidc"
    issuer_url: str = ""
    discovery_url: str = ""
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    userinfo_endpoint: str = ""
    jwks_uri: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    scopes: list[str] = Field(
        default_factory=lambda: ["openid", "profile", "email", "groups"]
    )
    username_claim: str = "preferred_username"
    groups_claim: str = "groups"
    allowed_groups: list[str] = Field(default_factory=list)
    tls_verify: bool = True
    allow_private_endpoints: bool = False
    allow_http: bool = False
    timeout_seconds: int = 10
    confirm_oidc_only: bool = False


class OIDCExchangeRequest(BaseModel):
    code: str


def _redact_oidc(config: dict[str, Any]) -> dict[str, Any]:
    safe = dict(config or {})
    safe.pop("client_secret", None)
    safe["client_secret_configured"] = bool((config or {}).get("client_secret"))
    return safe


def _validated_oidc_redirect_uri(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlparse(raw)
        hostname = str(parsed.hostname or "").lower()
        parsed.port
    except ValueError:
        parsed = None
        hostname = ""

    is_ip = False
    if hostname:
        try:
            ip_address(hostname)
            is_ip = True
        except ValueError:
            pass

    if (
        parsed is None
        or parsed.scheme != "https"
        or not hostname
        or hostname == "localhost"
        or hostname.endswith(".localhost")
        or is_ip
        or "." not in hostname
        or parsed.username
        or parsed.password
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.path != "/api/auth/oidc/callback"
    ):
        raise HTTPException(
            400,
            detail=(
                "OIDC redirect URI must use DUMB's browser-facing HTTPS FQDN "
                "and exact /api/auth/oidc/callback path; localhost and IP "
                "addresses are not accepted"
            ),
        )
    return raw


def _validate_oidc_provider(request: OIDCProviderRequest) -> dict[str, Any]:
    mode = request.mode.strip().lower()
    if mode not in {"local", "oidc", "hybrid"}:
        raise HTTPException(
            400, detail="Authentication mode must be local, oidc, or hybrid"
        )
    if mode == "oidc" and not request.confirm_oidc_only:
        raise HTTPException(
            400,
            detail="OIDC-only mode requires explicit lockout-risk confirmation",
        )
    if mode == "local":
        return {"mode": mode, "oidc": AUTH_CONFIG.get_oidc_config()}

    current = AUTH_CONFIG.get_oidc_config()
    data = request.model_dump(exclude={"mode", "confirm_oidc_only"})
    data["source"] = str(data.get("source") or "external").strip().lower()
    if data["source"] == "external":
        data["source"] = "custom_oidc"
    if data["source"] not in {"managed", "external_authelia", "custom_oidc"}:
        raise HTTPException(400, detail="Unsupported OIDC provider source")
    current_source = str(current.get("source") or "").strip().lower()
    if current_source == "external":
        current_source = "custom_oidc"
    source_changed = data["source"] != current_source
    if not source_changed and not str(data.get("client_secret") or "").strip():
        data["client_secret"] = current.get("client_secret", "")
    required = ("issuer_url", "client_id", "client_secret", "redirect_uri")
    missing = [field for field in required if not str(data.get(field) or "").strip()]
    if missing:
        raise HTTPException(
            400, detail=f"Missing required OIDC field(s): {', '.join(missing)}"
        )
    data["provider_name"] = str(data.get("provider_name") or "Single Sign-On").strip()
    data["issuer_url"] = str(data["issuer_url"]).strip().rstrip("/")
    data["redirect_uri"] = _validated_oidc_redirect_uri(data["redirect_uri"])
    data["scopes"] = list(dict.fromkeys(request.scopes))
    data["allowed_groups"] = list(dict.fromkeys(request.allowed_groups))
    data["timeout_seconds"] = max(2, min(30, request.timeout_seconds))
    return {"mode": mode, "oidc": data}


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
    if not AUTH_CONFIG.local_login_enabled():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Local password login is disabled",
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

    if not AUTH_CONFIG.validate_token_principal(payload):
        logger.warning("Token refresh failed for an unauthorized principal")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account or identity provider is no longer authorized",
        )

    logger.info(f"Token refreshed for user: {payload.sub}")
    return create_token_pair(
        payload.sub, provider=payload.provider, groups=payload.groups
    )


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

    if not AUTH_CONFIG.validate_token_principal(payload):
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
        mode=AUTH_CONFIG.get_auth_mode(),
        local_login_enabled=AUTH_CONFIG.local_login_enabled(),
        oidc_login_enabled=AUTH_CONFIG.oidc_login_enabled(),
        oidc_provider_name=str(
            AUTH_CONFIG.get_oidc_config().get("provider_name") or ""
        ),
    )


@auth_router.get("/provider")
def get_auth_provider(
    _current_user: str = Depends(get_optional_current_user),
):
    return {
        "mode": AUTH_CONFIG.get_auth_mode(),
        "oidc": _redact_oidc(AUTH_CONFIG.get_oidc_config()),
    }


@auth_router.put("/provider")
def update_auth_provider(
    request: OIDCProviderRequest,
    current_user: str = Depends(get_optional_current_user),
    logger=Depends(get_logger),
):
    validated = _validate_oidc_provider(request)
    AUTH_CONFIG.update_auth_provider(validated["mode"], validated["oidc"])
    logger.warning(
        "Authentication provider updated by %s; mode=%s source=%s",
        current_user or "system",
        validated["mode"],
        validated["oidc"].get("source", ""),
    )
    return {
        "mode": AUTH_CONFIG.get_auth_mode(),
        "oidc": _redact_oidc(AUTH_CONFIG.get_oidc_config()),
    }


@auth_router.post("/oidc/test")
def test_oidc_provider(
    request: OIDCProviderRequest,
    _current_user: str = Depends(get_optional_current_user),
):
    validated = _validate_oidc_provider(request)
    if validated["mode"] == "local":
        raise HTTPException(400, detail="Select OIDC or hybrid mode to test SSO")
    try:
        endpoints = resolved_endpoints(validated["oidc"])
    except OIDCError as exc:
        raise HTTPException(400, detail=str(exc)) from None
    return {
        "ok": True,
        "issuer": endpoints["issuer"],
        "endpoints": sorted(key for key in endpoints if key != "issuer"),
    }


@auth_router.get("/oidc/start")
def start_oidc_login(return_to: str = Query("/")):
    if not AUTH_CONFIG.is_auth_enabled() or not AUTH_CONFIG.oidc_login_enabled():
        raise HTTPException(404, detail="OIDC login is not enabled")
    try:
        authorization_url = OIDC_FLOWS.begin(AUTH_CONFIG.get_oidc_config(), return_to)
    except OIDCError as exc:
        raise HTTPException(400, detail=str(exc)) from None
    return {"authorization_url": authorization_url}


@auth_router.get("/oidc/callback")
def oidc_callback(
    state: str = Query(""),
    code: str = Query(""),
    error: str = Query(""),
    logger=Depends(get_logger),
):
    if error:
        return RedirectResponse(
            url=f"/login#oidc_error={quote('Authorization was denied')}",
            status_code=303,
        )
    if not AUTH_CONFIG.is_auth_enabled() or not AUTH_CONFIG.oidc_login_enabled():
        return RedirectResponse(
            url=f"/login#oidc_error={quote('OIDC login is not enabled')}",
            status_code=303,
        )
    try:
        exchange_code, return_to = OIDC_FLOWS.complete(
            AUTH_CONFIG.get_oidc_config(), state, code
        )
    except OIDCError as exc:
        logger.warning("OIDC callback rejected: %s", exc)
        return RedirectResponse(
            url=f"/login#oidc_error={quote(str(exc))}",
            status_code=303,
        )
    fragment = urlencode({"oidc_code": exchange_code, "return_to": return_to})
    return RedirectResponse(url=f"/login#{fragment}", status_code=303)


@auth_router.post("/oidc/exchange", response_model=TokenResponse)
def exchange_oidc_code(request: OIDCExchangeRequest, logger=Depends(get_logger)):
    if not AUTH_CONFIG.is_auth_enabled() or not AUTH_CONFIG.oidc_login_enabled():
        raise HTTPException(404, detail="OIDC login is not enabled")
    try:
        exchange = OIDC_FLOWS.redeem(request.code)
    except OIDCError as exc:
        raise HTTPException(401, detail=str(exc)) from None
    logger.info("OIDC login completed for user: %s", exchange.username)
    return create_token_pair(exchange.username, provider="oidc", groups=exchange.groups)


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
    if len(AUTH_CONFIG.config.users) == 0 and not AUTH_CONFIG.oidc_login_enabled():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Cannot enable authentication - configure OIDC or create a local "
                "user first."
            ),
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
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
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
    is_first_user = (
        not AUTH_CONFIG.is_auth_enabled() and len(AUTH_CONFIG.config.users) == 0
    )

    if AUTH_CONFIG.is_auth_enabled():
        # Auth is enabled - require authentication
        if not current_user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
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
        logger.info(
            f"Attempting to disable user {username}. Active users count: {len(active_users)}"
        )
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
