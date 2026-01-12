import os
import json
import secrets
from typing import Optional, Dict, List
from pydantic import BaseModel, Field
from utils.auth import get_password_hash, verify_password


class User(BaseModel):
    """User model"""

    username: str
    hashed_password: str = Field(..., alias="password")
    disabled: bool = False

    class Config:
        populate_by_name = True


class UserConfig(BaseModel):
    """User configuration structure"""

    enabled: bool = False
    users: List[User] = []
    jwt_secret: Optional[str] = None  # JWT secret key for token signing
    setup_skipped: bool = False  # Track if user explicitly skipped auth setup


class AuthConfigManager:
    """Manages user authentication configuration"""

    def __init__(self, config_path: str = "/config/users.json"):
        """
        Initialize the auth config manager.

        Args:
            config_path: Path to the users.json configuration file
        """
        self.config_path = os.path.abspath(config_path)
        self._ensure_config_exists()
        self.config = self._load_config()

    def _ensure_config_exists(self):
        """Create default config file if it doesn't exist"""
        if not os.path.exists(self.config_path):
            # Create default config with auth disabled and auto-generated JWT secret
            default_config = {
                "enabled": False,
                "users": [],
                "jwt_secret": self._generate_jwt_secret(),
            }
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, "w") as f:
                json.dump(default_config, f, indent=2)

    def _generate_jwt_secret(self) -> str:
        """
        Generate a cryptographically secure random JWT secret.

        Returns:
            A URL-safe base64-encoded secret (43 characters)
        """
        return secrets.token_urlsafe(32)

    def _load_config(self) -> UserConfig:
        """
        Load user configuration from file.

        Returns:
            UserConfig object
        """
        try:
            with open(self.config_path, "r") as f:
                data = json.load(f)
                config = UserConfig(**data)

                # Auto-generate JWT secret if missing (for existing configs)
                if not config.jwt_secret:
                    config.jwt_secret = self._generate_jwt_secret()
                    self.config = config
                    self.save_config()

                return config
        except (json.JSONDecodeError, FileNotFoundError) as e:
            # Return default config if file is invalid
            return UserConfig(
                enabled=False, users=[], jwt_secret=self._generate_jwt_secret()
            )

    def save_config(self):
        """Save current configuration to file"""
        config_dict = {
            "enabled": self.config.enabled,
            "users": [
                {
                    "username": user.username,
                    "password": user.hashed_password,
                    "disabled": user.disabled,
                }
                for user in self.config.users
            ],
            "jwt_secret": self.config.jwt_secret,
            "setup_skipped": self.config.setup_skipped,
        }

        with open(self.config_path, "w") as f:
            json.dump(config_dict, f, indent=2)

    def is_auth_enabled(self) -> bool:
        """
        Check if authentication is enabled.

        Returns:
            True if authentication is enabled, False otherwise
        """
        return self.config.enabled

    def get_user(self, username: str) -> Optional[User]:
        """
        Get user by username.

        Args:
            username: The username to look up

        Returns:
            User object if found, None otherwise
        """
        for user in self.config.users:
            if user.username == username:
                return user
        return None

    def authenticate_user(self, username: str, password: str) -> Optional[User]:
        """
        Authenticate a user with username and password.

        Args:
            username: The username
            password: The plain text password

        Returns:
            User object if authentication succeeds, None otherwise
        """
        if not self.config.enabled:
            # If auth is disabled, deny all authentication attempts
            return None

        user = self.get_user(username)
        if not user:
            return None

        if user.disabled:
            return None

        if not verify_password(password, user.hashed_password):
            return None

        return user

    def add_user(self, username: str, password: str, disabled: bool = False) -> bool:
        """
        Add a new user.

        Args:
            username: The username
            password: The plain text password (will be hashed)
            disabled: Whether the user is disabled

        Returns:
            True if user was added, False if username already exists
        """
        if self.get_user(username):
            return False

        hashed_password = get_password_hash(password)
        user = User(
            username=username, hashed_password=hashed_password, disabled=disabled
        )
        self.config.users.append(user)
        self.save_config()
        return True

    def update_user_password(self, username: str, new_password: str) -> bool:
        """
        Update a user's password.

        Args:
            username: The username
            new_password: The new plain text password (will be hashed)

        Returns:
            True if password was updated, False if user not found
        """
        user = self.get_user(username)
        if not user:
            return False

        user.hashed_password = get_password_hash(new_password)
        self.save_config()
        return True

    def remove_user(self, username: str) -> bool:
        """
        Remove a user.

        Args:
            username: The username to remove

        Returns:
            True if user was removed, False if user not found
        """
        user = self.get_user(username)
        if not user:
            return False

        self.config.users.remove(user)
        self.save_config()
        return True

    def enable_auth(self):
        """Enable authentication"""
        self.config.enabled = True
        self.save_config()

    def disable_auth(self):
        """Disable authentication"""
        self.config.enabled = False
        self.save_config()

    def get_all_users(self) -> List[Dict[str, any]]:
        """
        Get all users (without password hashes).

        Returns:
            List of user dictionaries
        """
        return [
            {"username": user.username, "disabled": user.disabled}
            for user in self.config.users
        ]

    def get_jwt_secret(self) -> str:
        """
        Get the JWT secret key for token signing/verification.

        Returns:
            The JWT secret key
        """
        return self.config.jwt_secret

    def mark_setup_skipped(self):
        """Mark that the user explicitly skipped auth setup"""
        self.config.setup_skipped = True
        self.save_config()

    def is_setup_skipped(self) -> bool:
        """
        Check if auth setup was explicitly skipped.

        Returns:
            True if setup was skipped, False otherwise
        """
        return self.config.setup_skipped
