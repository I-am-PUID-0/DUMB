"""DUMB-managed Authelia installation and configuration helpers."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from passlib.hash import pbkdf2_sha512

from utils.auth import get_password_hash
from utils.private_files import atomic_write_private_text

AUTHELIA_REPOSITORY = "authelia/authelia"
AUTHELIA_DEFAULT_VERSION = "v4.39.20"
AUTHELIA_CONFIG_PATH = "/config/authelia/configuration.yml"
AUTHELIA_USERS_PATH = "/config/authelia/users_database.yml"
AUTHELIA_STATE_PATH = "/config/authelia/dumb-managed.json"


class AutheliaSetupError(RuntimeError):
    """Safe managed-Authelia setup error."""


def _write_private(path: str, value: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write_private_text(path, value)


def _secret(path: str, length: int = 64) -> str:
    try:
        existing = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    value = secrets.token_urlsafe(length)
    _write_private(path, value)
    return value


def _load_state(config_dir: str) -> dict[str, Any]:
    path = os.path.join(config_dir, "dumb-managed.json")
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(config_dir: str, state: dict[str, Any]) -> None:
    _write_private(
        os.path.join(config_dir, "dumb-managed.json"),
        json.dumps(state, indent=2, sort_keys=True),
    )


def normalize_public_url(value: str) -> tuple[str, str]:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError as exc:
        raise AutheliaSetupError("Authelia public URL is invalid") from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise AutheliaSetupError("Authelia public URL must be an HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AutheliaSetupError("Authelia public URL contains unsupported components")
    return parsed.geturl().rstrip("/"), parsed.hostname.lower()


def validate_cookie_domain(public_host: str, cookie_domain: str) -> str:
    domain = str(cookie_domain or "").strip().lower().lstrip(".")
    if "." not in domain:
        raise AutheliaSetupError("Cookie domain must contain at least one dot")
    if public_host != domain and not public_host.endswith(f".{domain}"):
        raise AutheliaSetupError(
            "Cookie domain must match or be a parent of the Authelia hostname"
        )
    return domain


def bootstrap_user(
    config_dir: str,
    *,
    username: str,
    password: str,
    display_name: str,
    email: str,
    groups: list[str],
) -> None:
    username = username.strip()
    if len(username) < 3:
        raise AutheliaSetupError("Authelia username must be at least 3 characters")
    if len(password) < 12:
        raise AutheliaSetupError("Authelia password must be at least 12 characters")
    if len(password.encode("utf-8")) > 72:
        raise AutheliaSetupError("Authelia bootstrap password cannot exceed 72 bytes")
    users_path = os.path.join(config_dir, "users_database.yml")
    try:
        document = yaml.safe_load(Path(users_path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        document = {}
    users = document.setdefault("users", {})
    users[username] = {
        "disabled": False,
        "displayname": display_name.strip() or username,
        "password": get_password_hash(password),
        "email": email.strip(),
        "groups": list(
            dict.fromkeys(group.strip() for group in groups if group.strip())
        )
        or ["admins"],
    }
    _write_private(users_path, yaml.safe_dump(document, sort_keys=True))


def ensure_oidc_client(
    config_dir: str,
    *,
    key: str,
    client_id: str,
    client_name: str,
    redirect_uri: str,
    authorization_policy: str = "two_factor",
) -> dict[str, str]:
    if key not in {"dumb", "tpa"}:
        raise AutheliaSetupError("Unsupported managed OIDC client")
    parsed = urlparse(redirect_uri)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AutheliaSetupError("OIDC redirect URI must be an absolute HTTP(S) URL")
    state = _load_state(config_dir)
    clients = state.setdefault("clients", {})
    client = clients.get(key) if isinstance(clients.get(key), dict) else {}
    secret_value = str(client.get("client_secret") or secrets.token_urlsafe(48))
    clients[key] = {
        "client_id": client_id,
        "client_name": client_name,
        "client_secret": secret_value,
        "client_secret_hash": pbkdf2_sha512.using(rounds=310000).hash(secret_value),
        "redirect_uri": redirect_uri,
        "authorization_policy": authorization_policy,
    }
    _save_state(config_dir, state)
    return {"client_id": client_id, "client_secret": secret_value}


def managed_tpa_sso_allow_hosts(config: dict[str, Any]) -> set[str]:
    """Return private-resolution host exceptions required by the managed TPA client."""
    config_dir = str(config.get("config_dir") or "/config/authelia")
    state = _load_state(config_dir)
    clients = state.get("clients") if isinstance(state.get("clients"), dict) else {}
    tpa_client = clients.get("tpa") if isinstance(clients.get("tpa"), dict) else {}
    if not str(tpa_client.get("client_id") or "").strip():
        return set()

    try:
        _, public_host = normalize_public_url(str(config.get("public_url") or ""))
    except AutheliaSetupError:
        return set()
    return {public_host}


def _private_jwk(config_dir: str) -> str:
    path = os.path.join(config_dir, "secrets", "oidc-jwk.pem")
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if value:
        return value
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    value = key.private_bytes(
        Encoding.PEM,
        PrivateFormat.PKCS8,
        NoEncryption(),
    ).decode("ascii")
    _write_private(path, value)
    return value.strip()


def _notifier(config: dict[str, Any], config_dir: str) -> dict[str, Any]:
    notifier = (
        config.get("notifier") if isinstance(config.get("notifier"), dict) else {}
    )
    kind = str(notifier.get("type") or "filesystem").strip().lower()
    if kind == "smtp":
        address = str(notifier.get("address") or "").strip()
        sender = str(notifier.get("sender") or "").strip()
        if not address or not sender:
            raise AutheliaSetupError("SMTP notifier requires address and sender")
        return {
            "smtp": {
                "address": address,
                "username": str(notifier.get("username") or ""),
                "password": "",
                "sender": sender,
                "startup_check_address": str(
                    notifier.get("startup_check_address") or sender
                ),
                "disable_require_tls": bool(notifier.get("disable_require_tls", False)),
            }
        }
    notification_path = os.path.join(config_dir, "notification.txt")
    Path(notification_path).touch(mode=0o600, exist_ok=True)
    os.chmod(notification_path, 0o600)
    return {"filesystem": {"filename": notification_path}}


def render_configuration(config: dict[str, Any], postgres: dict[str, Any]) -> str:
    config_dir = str(config.get("config_dir") or "/config/authelia")
    public_url, public_host = normalize_public_url(str(config.get("public_url") or ""))
    cookie_domain = validate_cookie_domain(
        public_host, str(config.get("cookie_domain") or "")
    )
    policy = str(config.get("authorization_policy") or "two_factor")
    if policy not in {"one_factor", "two_factor"}:
        raise AutheliaSetupError(
            "Authorization policy must be one_factor or two_factor"
        )

    secrets_dir = os.path.join(config_dir, "secrets")
    _secret(os.path.join(secrets_dir, "session"))
    _secret(os.path.join(secrets_dir, "storage"))
    _secret(os.path.join(secrets_dir, "reset-password"))
    _secret(os.path.join(secrets_dir, "oidc-hmac"))
    _write_private(
        os.path.join(secrets_dir, "postgres-password"),
        str(postgres.get("password") or ""),
    )
    notifier = _notifier(config, config_dir)
    notifier_cfg = (
        config.get("notifier") if isinstance(config.get("notifier"), dict) else {}
    )
    if str(notifier_cfg.get("type") or "").lower() == "smtp":
        smtp_secret_path = os.path.join(secrets_dir, "smtp-password")
        supplied_password = str(notifier_cfg.get("password") or "")
        if supplied_password or not os.path.exists(smtp_secret_path):
            _write_private(smtp_secret_path, supplied_password)

    state = _load_state(config_dir)
    managed_clients = []
    for value in (state.get("clients") or {}).values():
        if not isinstance(value, dict):
            continue
        managed_clients.append(
            {
                "client_id": value["client_id"],
                "client_name": value["client_name"],
                "client_secret": value["client_secret_hash"],
                "redirect_uris": [value["redirect_uri"]],
                "scopes": ["openid", "profile", "email", "groups"],
                "authorization_policy": value.get("authorization_policy", policy),
                "token_endpoint_auth_method": "client_secret_basic",
            }
        )

    default_redirect = str(config.get("default_redirection_url") or "").strip()
    cookie = {
        "domain": cookie_domain,
        "authelia_url": public_url,
        "same_site": "lax",
        "inactivity": "15m",
        "expiration": "8h",
        "remember_me": "30d",
    }
    if default_redirect:
        redirect, _ = normalize_public_url(default_redirect)
        if redirect == public_url:
            raise AutheliaSetupError(
                "Default redirection URL must differ from the Authelia URL"
            )
        cookie["default_redirection_url"] = redirect

    document: dict[str, Any] = {
        "theme": "auto",
        "server": {
            "address": f"tcp://0.0.0.0:{int(config.get('port', 9091))}/",
            "disable_healthcheck": True,
        },
        "log": {
            "level": str(config.get("log_level") or "info").lower(),
            "format": "text",
            "file_path": str(config.get("log_file") or "/log/authelia.log"),
            "keep_stdout": True,
        },
        "totp": {"issuer": public_host},
        "authentication_backend": {
            "password_reset": {"disable": False},
            "password_change": {"disable": False},
            "file": {
                "path": os.path.join(config_dir, "users_database.yml"),
                "watch": True,
                "password": {"algorithm": "bcrypt", "bcrypt": {"cost": 12}},
            },
        },
        "access_control": {
            "default_policy": "deny",
            "rules": [
                {
                    "domain": [cookie_domain, f"*.{cookie_domain}"],
                    "policy": policy,
                }
            ],
        },
        "session": {"secret": "", "cookies": [cookie]},
        "regulation": {
            "max_retries": 5,
            "find_time": "2m",
            "ban_time": "5m",
        },
        "storage": {
            "encryption_key": "",
            "postgres": {
                "address": (
                    f"tcp://{postgres.get('host', '127.0.0.1')}:"
                    f"{int(postgres.get('port', 5432))}"
                ),
                "database": "authelia",
                "schema": "public",
                "username": str(postgres.get("user") or "DUMB"),
                "password": "",
                "timeout": "5s",
            },
        },
        "notifier": notifier,
        "identity_validation": {"reset_password": {"jwt_secret": ""}},
    }
    # Authelia rejects an enabled OIDC provider with an empty client list. The
    # initial bootstrap intentionally starts the portal before the optional
    # DUMB and TPA clients are registered, so omit OIDC until the first client
    # exists instead of generating a configuration Authelia cannot start.
    if managed_clients:
        document["identity_providers"] = {
            "oidc": {
                "hmac_secret": "",
                "jwks": [
                    {
                        "algorithm": "RS256",
                        "use": "sig",
                        "key": _private_jwk(config_dir),
                    }
                ],
                "clients": managed_clients,
            }
        }
    path = os.path.join(config_dir, "configuration.yml")
    _write_private(path, yaml.safe_dump(document, sort_keys=False))
    users_path = os.path.join(config_dir, "users_database.yml")
    if not os.path.exists(users_path):
        _write_private(users_path, "users: {}\n")
    return path


def authelia_environment(config: dict[str, Any]) -> dict[str, str]:
    config_dir = str(config.get("config_dir") or "/config/authelia")
    secret_dir = os.path.join(config_dir, "secrets")
    env = {
        "AUTHELIA_SESSION_SECRET_FILE": os.path.join(secret_dir, "session"),
        "AUTHELIA_STORAGE_ENCRYPTION_KEY_FILE": os.path.join(secret_dir, "storage"),
        "AUTHELIA_STORAGE_POSTGRES_PASSWORD_FILE": os.path.join(
            secret_dir, "postgres-password"
        ),
        "AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET_FILE": os.path.join(
            secret_dir, "reset-password"
        ),
    }
    state = _load_state(config_dir)
    clients = state.get("clients") if isinstance(state.get("clients"), dict) else {}
    if clients:
        # Setting this secret enables Authelia's OIDC provider even when the
        # YAML section is absent. Keep it in lockstep with the managed client
        # list so the required Step 1 bootstrap can run before optional OIDC
        # clients are added in Steps 2 and 3.
        env["AUTHELIA_IDENTITY_PROVIDERS_OIDC_HMAC_SECRET_FILE"] = os.path.join(
            secret_dir, "oidc-hmac"
        )
    notifier = (
        config.get("notifier") if isinstance(config.get("notifier"), dict) else {}
    )
    if str(notifier.get("type") or "").lower() == "smtp":
        env["AUTHELIA_NOTIFIER_SMTP_PASSWORD_FILE"] = os.path.join(
            secret_dir, "smtp-password"
        )
    return env


def _architecture() -> str:
    machine = platform.machine().lower()
    mapping = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "arm",
    }
    try:
        return mapping[machine]
    except KeyError as exc:
        raise AutheliaSetupError(
            f"Unsupported Authelia architecture: {machine}"
        ) from exc


def install_release(install_dir: str, version: str) -> str:
    selected = str(version or "latest").strip()
    if selected.lower() == "latest":
        response = requests.get(
            f"https://api.github.com/repos/{AUTHELIA_REPOSITORY}/releases/latest",
            timeout=20,
        )
        response.raise_for_status()
        selected = str(response.json().get("tag_name") or "")
    if not selected.startswith("v"):
        selected = f"v{selected}"
    if not selected or "/" in selected or "\\" in selected:
        raise AutheliaSetupError("Authelia release version is invalid")

    asset = f"authelia-{selected}-linux-{_architecture()}.tar.gz"
    base = f"https://github.com/{AUTHELIA_REPOSITORY}/releases/download/{selected}"
    with tempfile.TemporaryDirectory(prefix="dumb-authelia-") as temp_dir:
        archive = os.path.join(temp_dir, asset)
        checksums_path = os.path.join(temp_dir, "checksums.sha256")
        for url, destination in (
            (f"{base}/{asset}", archive),
            (f"{base}/checksums.sha256", checksums_path),
        ):
            try:
                with requests.get(url, stream=True, timeout=60) as response:
                    response.raise_for_status()
                    with open(destination, "wb") as output:
                        for chunk in response.iter_content(1024 * 1024):
                            if chunk:
                                output.write(chunk)
            except requests.RequestException as exc:
                raise AutheliaSetupError("Failed to download Authelia release") from exc
        expected = ""
        for line in Path(checksums_path).read_text(encoding="utf-8").splitlines():
            digest, _, filename = line.partition("  ")
            if filename.strip().lstrip("*") == asset:
                expected = digest.strip().lower()
                break
        actual = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
        if not expected or not secrets.compare_digest(actual, expected):
            raise AutheliaSetupError("Authelia release checksum verification failed")

        extract_dir = os.path.join(temp_dir, "extract")
        os.makedirs(extract_dir)
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                target = os.path.realpath(os.path.join(extract_dir, member.name))
                if not target.startswith(os.path.realpath(extract_dir) + os.sep):
                    raise AutheliaSetupError("Authelia archive contains an unsafe path")
                if member.issym() or member.islnk() or member.isdev():
                    raise AutheliaSetupError(
                        "Authelia archive contains an unsupported entry"
                    )
            bundle.extractall(extract_dir, filter="data")
        binary = next(
            (
                str(path)
                for path in Path(extract_dir).rglob("authelia")
                if path.is_file()
            ),
            "",
        )
        if not binary:
            raise AutheliaSetupError("Authelia release did not contain its binary")
        os.makedirs(install_dir, exist_ok=True)
        temp_binary = os.path.join(install_dir, ".authelia.new")
        shutil.copy2(binary, temp_binary)
        os.chmod(temp_binary, 0o755)
        os.replace(temp_binary, os.path.join(install_dir, "authelia"))
    return selected
