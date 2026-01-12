import os
from datetime import datetime, timedelta, timezone
from typing import Optional
import jwt
from pydantic import BaseModel

# Workaround for passlib + bcrypt 5.x compatibility issue
# passlib tries to access bcrypt.__about__.__version__ which doesn't exist in bcrypt 5.x
try:
    import bcrypt

    if not hasattr(bcrypt, "__about__"):
        # Create a mock __about__ module with version info
        class MockAbout:
            __version__ = "5.0.0"

        bcrypt.__about__ = MockAbout()
except ImportError:
    pass

from passlib.context import CryptContext

# Password hashing context using bcrypt
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT Configuration
# Note: JWT_SECRET_KEY is loaded from the auth config file (users.json) and auto-generated on first run.
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(
    os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60")
)
JWT_REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("JWT_REFRESH_TOKEN_EXPIRE_DAYS", "30"))

# Global auth config manager for accessing JWT secret
# This is initialized lazily to avoid circular imports
_auth_config_manager = None


def _get_auth_config():
    """Get or initialize the global auth config manager"""
    global _auth_config_manager
    if _auth_config_manager is None:
        from utils.auth_config import AuthConfigManager

        _auth_config_manager = AuthConfigManager()
    return _auth_config_manager


def get_jwt_secret() -> str:
    """
    Get the JWT secret key from the config file.

    Returns:
        The JWT secret key (auto-generated and stored in users.json)
    """
    return _get_auth_config().get_jwt_secret()


class TokenPayload(BaseModel):
    """JWT token payload structure"""

    sub: str  # username
    exp: datetime
    type: str  # "access" or "refresh"


class TokenResponse(BaseModel):
    """Token response structure"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plain password against a hashed password using bcrypt directly.

    Args:
        plain_password: The plain text password
        hashed_password: The hashed password to verify against

    Returns:
        True if the password matches, False otherwise
    """
    # Use bcrypt directly instead of passlib due to compatibility issues
    # with bcrypt 5.x and passlib 1.7.4
    import bcrypt as _bcrypt

    # Bcrypt has a maximum password length of 72 bytes
    password_bytes = plain_password.encode("utf-8")
    if len(password_bytes) > 72:
        password_bytes = password_bytes[:72]

    hashed_bytes = hashed_password.encode("utf-8")
    return _bcrypt.checkpw(password_bytes, hashed_bytes)


def get_password_hash(password: str) -> str:
    """
    Hash a password using bcrypt directly to avoid passlib compatibility issues.

    Args:
        password: The plain text password to hash

    Returns:
        The hashed password
    """
    # Bcrypt has a maximum password length of 72 bytes
    # Truncate if necessary to avoid errors
    password_bytes = password.encode("utf-8")
    if len(password_bytes) > 72:
        password_bytes = password_bytes[:72]

    # Use bcrypt directly instead of passlib due to compatibility issues
    # with bcrypt 5.x and passlib 1.7.4
    import bcrypt as _bcrypt

    salt = _bcrypt.gensalt()
    hashed = _bcrypt.hashpw(password_bytes, salt)
    return hashed.decode("utf-8")


def create_access_token(
    username: str, expires_delta: Optional[timedelta] = None
) -> str:
    """
    Create a JWT access token.

    Args:
        username: The username to encode in the token
        expires_delta: Optional custom expiration time

    Returns:
        The encoded JWT token
    """
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=JWT_ACCESS_TOKEN_EXPIRE_MINUTES
        )

    to_encode = {"sub": username, "exp": expire, "type": "access"}
    encoded_jwt = jwt.encode(to_encode, get_jwt_secret(), algorithm=JWT_ALGORITHM)
    return encoded_jwt


def create_refresh_token(
    username: str, expires_delta: Optional[timedelta] = None
) -> str:
    """
    Create a JWT refresh token.

    Args:
        username: The username to encode in the token
        expires_delta: Optional custom expiration time

    Returns:
        The encoded JWT refresh token
    """
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            days=JWT_REFRESH_TOKEN_EXPIRE_DAYS
        )

    to_encode = {"sub": username, "exp": expire, "type": "refresh"}
    encoded_jwt = jwt.encode(to_encode, get_jwt_secret(), algorithm=JWT_ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> Optional[TokenPayload]:
    """
    Decode and validate a JWT token.

    Args:
        token: The JWT token to decode

    Returns:
        TokenPayload if valid, None if invalid or expired
    """
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        username: str = payload.get("sub")
        token_type: str = payload.get("type")

        if username is None or token_type is None:
            return None

        return TokenPayload(
            sub=username,
            exp=datetime.fromtimestamp(payload.get("exp"), tz=timezone.utc),
            type=token_type,
        )
    except jwt.ExpiredSignatureError:
        return None
    except jwt.JWTError:
        return None


def create_token_pair(username: str) -> TokenResponse:
    """
    Create both access and refresh tokens for a user.

    Args:
        username: The username to create tokens for

    Returns:
        TokenResponse containing both access and refresh tokens
    """
    access_token = create_access_token(username)
    refresh_token = create_refresh_token(username)

    return TokenResponse(access_token=access_token, refresh_token=refresh_token)
