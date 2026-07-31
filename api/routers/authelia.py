"""Managed and external Authelia integration endpoints."""

from __future__ import annotations

import os
import re
import secrets
import stat
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from api.routers.auth import AUTH_CONFIG
from utils.authelia_settings import (
    AutheliaSetupError,
    _load_state,
    _write_private,
    bootstrap_user,
    ensure_oidc_client,
    normalize_public_url,
    render_configuration,
    validate_cookie_domain,
)
from utils.config_loader import CONFIG_MANAGER
from utils.dependencies import (
    get_logger,
    get_optional_current_user,
    get_updater,
)
from utils.traefik_setup import (
    get_traefik_dynamic_config_dir,
    write_traefik_config,
)

authelia_router = APIRouter()

_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, private",
    "Pragma": "no-cache",
}
_MAX_NOTIFICATION_BYTES = 256 * 1024
_NOTIFICATION_SUBJECT_PATTERN = re.compile(
    rb"(?im)^Subject:[ \t]*([^\r\n]+?)[ \t]*\r?$"
)
_VERIFICATION_CODE_PATTERN = re.compile(
    rb"(?m)^-{8,}[ \t]*\r?\n[ \t\r\n]*"
    rb"([A-Za-z0-9]{4,64})[ \t]*\r?\n[ \t\r\n]*"
    rb"^-{8,}[ \t]*\r?$"
)


class BootstrapRequest(BaseModel):
    public_url: str
    cookie_domain: str
    default_redirection_url: str = ""
    authorization_policy: Literal["one_factor", "two_factor"] = "two_factor"
    username: str
    password: str
    display_name: str = ""
    email: str
    groups: list[str] = Field(default_factory=lambda: ["admins"])
    notifier_type: Literal["filesystem", "smtp"] = "filesystem"
    smtp_address: str = ""
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_sender: str = ""
    smtp_startup_check_address: str = ""
    smtp_disable_require_tls: bool = False
    start_service: bool = True


class LinkDumbRequest(BaseModel):
    source: Literal["managed", "external_authelia", "custom_oidc"] = "managed"
    mode: Literal["oidc", "hybrid"] = "hybrid"
    provider_name: str = "Authelia"
    dumb_public_url: str
    issuer_url: str = ""
    discovery_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    scopes: list[str] = Field(
        default_factory=lambda: ["openid", "profile", "email", "groups"]
    )
    username_claim: str = "preferred_username"
    groups_claim: str = "groups"
    allowed_groups: list[str] = Field(default_factory=list)
    tls_verify: bool = True
    allow_private_endpoints: bool = False
    allow_http: bool = False
    confirm_oidc_only: bool = False


class LinkTPARequest(BaseModel):
    tpa_public_url: str
    provider_name: str = "DUMB-managed Authelia"
    configure_admin_sso: bool = True
    allow_local_fallback: bool = True
    admin_groups: list[str] = Field(default_factory=lambda: ["admins"])
    restart_tpa: bool = True


class ForwardAuthRequest(BaseModel):
    source: Literal["managed", "external"] = "managed"
    address: str = ""
    allow_http: bool = False


class TPARouteRequest(BaseModel):
    domain_id: str = Field(min_length=1, max_length=128)
    application: Literal["authelia", "dumb", "tpa"] = "authelia"
    public_url: str = ""
    target_host: str = ""
    target_port: int | None = Field(default=None, ge=1, le=65535)


def _service_config() -> dict:
    config = CONFIG_MANAGER.get("authelia")
    if not isinstance(config, dict):
        raise HTTPException(404, detail="Authelia service configuration is unavailable")
    return config


def _managed_notifier_type(config: dict) -> str:
    notifier = config.get("notifier")
    if not isinstance(notifier, dict):
        return "filesystem"
    return str(notifier.get("type") or "filesystem").strip().lower()


def _public_origin_from_redirect_uri(value: object) -> str:
    """Return a browser-safe public origin from a managed OIDC redirect URI."""
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    hostname = parsed.hostname
    if "." not in hostname or hostname.lower() == "localhost":
        return ""
    try:
        ip_address(hostname)
    except ValueError:
        pass
    else:
        return ""
    authority = hostname if port in {None, 443} else f"{hostname}:{port}"
    return f"https://{authority}"


def _latest_filesystem_verification_code(config: dict) -> str:
    """Read only the newest verification code from Authelia's managed notifier."""
    config_dir = Path(str(config.get("config_dir") or "/config/authelia")).resolve()
    notification_path = config_dir / "notification.txt"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(notification_path, flags)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise HTTPException(
            409,
            detail="The managed Authelia notification file cannot be read safely",
            headers=_NO_STORE_HEADERS,
        ) from exc

    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise HTTPException(
                409,
                detail="The managed Authelia notification path is not a regular file",
                headers=_NO_STORE_HEADERS,
            )
        if file_stat.st_size > _MAX_NOTIFICATION_BYTES:
            raise HTTPException(
                409,
                detail="The managed Authelia notification file is unexpectedly large",
                headers=_NO_STORE_HEADERS,
            )
        content = bytearray()
        while len(content) <= _MAX_NOTIFICATION_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, _MAX_NOTIFICATION_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > _MAX_NOTIFICATION_BYTES:
            raise HTTPException(
                409,
                detail="The managed Authelia notification file is unexpectedly large",
                headers=_NO_STORE_HEADERS,
            )
    finally:
        os.close(descriptor)

    raw_content = bytes(content)
    notification_subjects = list(_NOTIFICATION_SUBJECT_PATTERN.finditer(raw_content))
    for index in range(len(notification_subjects) - 1, -1, -1):
        subject = notification_subjects[index].group(1).strip().lower()
        if subject != b"confirm your identity":
            continue
        message_start = notification_subjects[index].start()
        message_end = (
            notification_subjects[index + 1].start()
            if index + 1 < len(notification_subjects)
            else len(raw_content)
        )
        matches = list(
            _VERIFICATION_CODE_PATTERN.finditer(raw_content[message_start:message_end])
        )
        if matches:
            return matches[-1].group(1).decode("ascii")
    return ""


def _absolute_base_url(value: str, label: str) -> str:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        parsed = None
    hostname = str(parsed.hostname or "").lower() if parsed is not None else ""
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
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(400, detail=f"{label} must be a browser-facing HTTPS FQDN")
    return parsed.geturl().rstrip("/")


def _external_route_target_host(value: str) -> str:
    target_host = str(value or "").strip()
    if (
        not target_host
        or len(target_host) > 253
        or not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9._:-]*[A-Za-z0-9])?",
            target_host,
        )
    ):
        raise HTTPException(
            400,
            detail=(
                "DUMB route target host must be a hostname, container name, or IP "
                "address reachable by Traefik, without a scheme or path"
            ),
        )
    return target_host


def _save_service_config(config: dict) -> None:
    CONFIG_MANAGER.save_config(config.get("process_name", "Authelia"))


def _restart_managed_authelia(config: dict, updater, logger) -> None:
    process_name = config.get("process_name", "Authelia")
    updater.stop_process(process_name)
    process, _ = updater.auto_update(
        process_name,
        enable_update=False,
    )
    if process:
        return
    logger.error("Authelia could not restart after its OIDC clients changed")
    raise HTTPException(
        500,
        detail=(
            "The OIDC client was saved, but Authelia could not restart with its "
            "updated configuration. DUMB authentication was not changed."
        ),
    )


def _ensure_tpa_token(tpa: dict) -> tuple[str, bool]:
    env = tpa.get("env") if isinstance(tpa.get("env"), dict) else {}
    token = str(env.get("DUMB_INTEGRATION_TOKEN") or "")
    created = False
    if len(token) < 32:
        token = secrets.token_urlsafe(48)
        created = True
        env["DUMB_INTEGRATION_TOKEN"] = token
        tpa["env"] = env
        CONFIG_MANAGER.save_config(tpa.get("process_name", "Traefik Proxy Admin"))
    return token, created


def _safe_tpa_link_result(result: object) -> dict:
    """Return only the non-secret fields expected from TPA's link endpoint."""
    data = result if isinstance(result, dict) else {}
    return {
        "linked": bool(data.get("linked")),
        "providerId": str(data.get("providerId") or ""),
        "providerName": str(data.get("providerName") or ""),
        "adminSsoConfigured": bool(data.get("adminSsoConfigured")),
        "localFallbackEnabled": bool(data.get("localFallbackEnabled")),
    }


def _tpa_integration_context() -> tuple[dict, str, str]:
    tpa = CONFIG_MANAGER.get("traefik_proxy_admin") or {}
    if not tpa.get("enabled"):
        raise HTTPException(400, detail="Enable Traefik Proxy Admin first")
    token = str((tpa.get("env") or {}).get("DUMB_INTEGRATION_TOKEN") or "")
    if len(token) < 32:
        raise HTTPException(
            409,
            detail=(
                "TPA integration is not initialized. Configure and restart Traefik "
                "Proxy Admin once, then retry domain discovery."
            ),
        )
    base_url = f"http://127.0.0.1:{int(tpa.get('port', 3004))}"
    return tpa, token, base_url


def _safe_tpa_domains(result: object) -> list[dict]:
    data = result if isinstance(result, dict) else {}
    domains = data.get("domains") if isinstance(data.get("domains"), list) else []
    safe = []
    for domain in domains:
        if not isinstance(domain, dict):
            continue
        domain_id = str(domain.get("id") or "").strip()
        fqdn = str(domain.get("domain") or "").strip().lower()
        if not domain_id or not fqdn:
            continue
        safe.append(
            {
                "id": domain_id,
                "name": str(domain.get("name") or fqdn),
                "domain": fqdn,
                "is_default": bool(domain.get("isDefault")),
                "use_wildcard_cert": bool(domain.get("useWildcardCert")),
                "cert_resolver": str(domain.get("certResolver") or ""),
                "service_count": int(domain.get("serviceCount") or 0),
            }
        )
    return safe


def _safe_tpa_route_applications(result: object) -> list[str]:
    data = result if isinstance(result, dict) else {}
    applications = (
        data.get("routeApplications")
        if isinstance(data.get("routeApplications"), list)
        else []
    )
    allowed = {"authelia", "dumb", "tpa"}
    return sorted(
        {
            str(application)
            for application in applications
            if str(application) in allowed
        }
    )


def _safe_tpa_public_routes(result: object) -> list[dict]:
    """Return the minimal non-secret route data needed for public UI links."""
    data = result if isinstance(result, dict) else {}
    routes = (
        data.get("publicRoutes") if isinstance(data.get("publicRoutes"), list) else []
    )
    safe = []
    for route in routes[:500]:
        if not isinstance(route, dict):
            continue
        name = str(route.get("name") or "").strip()
        try:
            target_port = int(route.get("targetPort") or 0)
        except (TypeError, ValueError):
            continue
        if not name or len(name) > 255 or not 1 <= target_port <= 65535:
            continue
        candidates = route.get("publicUrls")
        if not isinstance(candidates, list):
            continue
        public_urls = []
        for candidate in candidates[:20]:
            public_url = _public_origin_from_redirect_uri(candidate)
            if public_url and public_url not in public_urls:
                public_urls.append(public_url)
        if not public_urls:
            continue
        safe.append(
            {
                "name": name,
                "enabled": bool(route.get("enabled")),
                "target_port": target_port,
                "target_loopback": bool(route.get("targetLoopback")),
                "public_urls": public_urls,
            }
        )
    return safe


def _safe_tpa_route_result(result: object) -> dict:
    data = result if isinstance(result, dict) else {}
    domain = data.get("domain") if isinstance(data.get("domain"), dict) else {}
    return {
        "application": str(data.get("application") or "authelia"),
        "configured": bool(data.get("configured")),
        "created": bool(data.get("created")),
        "reused": bool(data.get("reused")),
        "service_id": str(data.get("serviceId") or ""),
        "hostname": str(data.get("hostname") or ""),
        "domain": str(domain.get("domain") or ""),
        "target_host": str(data.get("targetHost") or ""),
        "target_port": int(data.get("targetPort") or 0),
        "target_https": bool(data.get("targetHttps")),
        "authentication": str(data.get("authentication") or ""),
    }


def _tpa_response_error(response: requests.Response, fallback: str) -> HTTPException:
    detail = fallback
    if response.status_code in {400, 404, 409}:
        try:
            payload = response.json()
            candidate = (
                str(payload.get("error") or "").strip()
                if isinstance(payload, dict)
                else ""
            )
            if candidate and len(candidate) <= 300 and "\n" not in candidate:
                detail = candidate
        except ValueError:
            pass
    status_code = (
        response.status_code if response.status_code in {400, 404, 409} else 502
    )
    return HTTPException(status_code, detail=detail)


@authelia_router.get("/status")
def integration_status(
    _current_user: str = Depends(get_optional_current_user),
):
    config = _service_config()
    config_dir = str(config.get("config_dir") or "/config/authelia")
    notifier_type = _managed_notifier_type(config)
    state = _load_state(config_dir)
    clients = state.get("clients") if isinstance(state.get("clients"), dict) else {}
    tpa_client = clients.get("tpa") if isinstance(clients.get("tpa"), dict) else {}
    users_path = Path(config_dir, "users_database.yml")
    tpa = CONFIG_MANAGER.get("traefik_proxy_admin") or {}
    dumb = CONFIG_MANAGER.get("dumb") or {}
    frontend = dumb.get("frontend") if isinstance(dumb, dict) else {}
    if not isinstance(frontend, dict):
        frontend = {}
    return {
        "managed": {
            "enabled": bool(config.get("enabled")),
            "configured": bool(
                config.get("public_url") and config.get("cookie_domain")
            ),
            "public_url": str(config.get("public_url") or ""),
            "cookie_domain": str(config.get("cookie_domain") or ""),
            "port": int(config.get("port", 9091) or 9091),
            "authorization_policy": str(
                config.get("authorization_policy") or "two_factor"
            ),
            "notifier_type": notifier_type,
            "verification_code_helper": notifier_type == "filesystem",
            "users_file_present": users_path.is_file(),
            "clients": sorted(clients.keys()),
        },
        "dumb_auth": {
            "enabled": AUTH_CONFIG.is_auth_enabled(),
            "mode": AUTH_CONFIG.get_auth_mode(),
            "oidc": {
                "enabled": AUTH_CONFIG.oidc_login_enabled(),
                "source": str(AUTH_CONFIG.get_oidc_config().get("source") or ""),
                "provider_name": str(
                    AUTH_CONFIG.get_oidc_config().get("provider_name") or ""
                ),
            },
        },
        "tpa": {
            "enabled": bool(tpa.get("enabled")),
            "public_url": _public_origin_from_redirect_uri(
                tpa_client.get("redirect_uri")
            ),
            "integration_token_configured": bool(
                (tpa.get("env") or {}).get("DUMB_INTEGRATION_TOKEN")
            ),
        },
        "dumb_frontend": {
            "enabled": bool(frontend.get("enabled")),
            "port": int(frontend.get("port", 3005) or 3005),
        },
        "forward_auth": {
            "middleware": "dumb-authelia-forward-auth@file",
            "configured": Path(
                get_traefik_dynamic_config_dir(), "authelia.yml"
            ).is_file(),
        },
    }


@authelia_router.get("/verification-code")
def latest_verification_code(
    response: Response,
    _current_user: str | None = Depends(get_optional_current_user),
):
    """Reveal only the newest filesystem-delivered Authelia enrollment code."""
    response.headers.update(_NO_STORE_HEADERS)
    if not _current_user:
        raise HTTPException(
            403,
            detail=(
                "A signed-in DUMB session is required to reveal an Authelia "
                "verification code"
            ),
            headers=_NO_STORE_HEADERS,
        )
    config = _service_config()
    if not config.get("public_url") or not config.get("cookie_domain"):
        raise HTTPException(
            409,
            detail="Complete managed Authelia bootstrap first",
            headers=_NO_STORE_HEADERS,
        )
    if _managed_notifier_type(config) != "filesystem":
        raise HTTPException(
            409,
            detail="Verification codes are delivered by the configured SMTP notifier",
            headers=_NO_STORE_HEADERS,
        )
    code = _latest_filesystem_verification_code(config)
    return {
        "available": bool(code),
        "code": code,
        "delivery": "filesystem",
    }


@authelia_router.get("/tpa-domains")
def discover_tpa_domains(
    _current_user: str = Depends(get_optional_current_user),
    logger=Depends(get_logger),
):
    _, token, base_url = _tpa_integration_context()
    try:
        response = requests.get(
            f"{base_url}/api/integrations/dumb/authelia/route",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException:
        logger.error("TPA domain discovery failed")
        raise HTTPException(
            502, detail="Unable to reach Traefik Proxy Admin for domain discovery"
        ) from None
    if not response.ok:
        logger.error("TPA domain discovery returned status %s", response.status_code)
        raise _tpa_response_error(response, "Unable to discover TPA domains") from None
    try:
        result = response.json()
    except ValueError:
        logger.error("TPA domain discovery returned an invalid response")
        raise HTTPException(
            502, detail="TPA returned an invalid domain response"
        ) from None
    return {
        "domains": _safe_tpa_domains(result),
        "route_applications": _safe_tpa_route_applications(result),
        "public_routes": _safe_tpa_public_routes(result),
    }


@authelia_router.post("/tpa-route")
def configure_tpa_route(
    request: TPARouteRequest,
    _current_user: str = Depends(get_optional_current_user),
    logger=Depends(get_logger),
):
    authelia = _service_config()
    tpa, token, base_url = _tpa_integration_context()
    route_label = {
        "authelia": "Authelia",
        "dumb": "DUMB",
        "tpa": "TPA",
    }[request.application]
    if request.application == "authelia":
        public_url = str(authelia.get("public_url") or "").strip()
        if not public_url:
            raise HTTPException(400, detail="Bootstrap managed Authelia first")
        target_port = int(authelia.get("port", 9091) or 9091)
    elif request.application == "dumb":
        public_url = _absolute_base_url(request.public_url, "DUMB public URL")
        dumb = CONFIG_MANAGER.get("dumb") or {}
        frontend = dumb.get("frontend") if isinstance(dumb, dict) else {}
        if not isinstance(frontend, dict):
            frontend = {}
        if frontend.get("enabled"):
            target_host = "127.0.0.1"
            target_port = int(frontend.get("port", 3005) or 3005)
        else:
            target_host = _external_route_target_host(request.target_host)
            target_port = int(request.target_port or 3005)
    else:
        public_url = _absolute_base_url(request.public_url, "TPA public URL")
        target_host = "127.0.0.1"
        target_port = int(tpa.get("port", 3004) or 3004)
    if request.application == "authelia":
        target_host = "127.0.0.1"
    try:
        response = requests.post(
            f"{base_url}/api/integrations/dumb/authelia/route",
            json={
                "domainId": request.domain_id,
                "publicUrl": public_url,
                "targetHost": target_host,
                "targetPort": target_port,
                "application": request.application,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
    except requests.RequestException:
        logger.error("TPA %s route configuration failed", route_label)
        raise HTTPException(
            502, detail=f"Unable to reach TPA to configure the {route_label} route"
        ) from None
    if not response.ok:
        logger.error(
            "TPA %s route configuration returned status %s",
            route_label,
            response.status_code,
        )
        raise _tpa_response_error(
            response, f"Unable to configure the {route_label} route in TPA"
        ) from None
    try:
        result = response.json()
    except ValueError:
        logger.error(
            "TPA %s route configuration returned an invalid response",
            route_label,
        )
        raise HTTPException(
            502, detail="TPA returned an invalid route response"
        ) from None
    safe_result = _safe_tpa_route_result(result)
    if not safe_result["configured"]:
        raise HTTPException(502, detail=f"TPA did not confirm the {route_label} route")
    logger.info(
        "%s public route configured in Traefik Proxy Admin",
        request.application,
    )
    return safe_result


@authelia_router.post("/bootstrap")
def bootstrap_managed_authelia(
    request: BootstrapRequest,
    _current_user: str = Depends(get_optional_current_user),
    updater=Depends(get_updater),
    logger=Depends(get_logger),
):
    config = _service_config()
    public_url, public_host = normalize_public_url(request.public_url)
    cookie_domain = validate_cookie_domain(public_host, request.cookie_domain)
    default_redirect = ""
    if request.default_redirection_url.strip():
        default_redirect, _ = normalize_public_url(request.default_redirection_url)

    notifier = {
        "type": request.notifier_type,
        "address": request.smtp_address.strip(),
        "username": request.smtp_username.strip(),
        "sender": request.smtp_sender.strip(),
        "startup_check_address": request.smtp_startup_check_address.strip(),
        "disable_require_tls": request.smtp_disable_require_tls,
    }
    config.update(
        {
            "enabled": True,
            "public_url": public_url,
            "cookie_domain": cookie_domain,
            "default_redirection_url": default_redirect,
            "authorization_policy": request.authorization_policy,
            "notifier": notifier,
        }
    )
    config_dir = str(config.get("config_dir") or "/config/authelia")
    if request.notifier_type == "smtp":
        if not request.smtp_address.strip() or not request.smtp_sender.strip():
            raise HTTPException(400, detail="SMTP address and sender are required")
        if request.smtp_password:
            _write_private(
                os.path.join(config_dir, "secrets", "smtp-password"),
                request.smtp_password,
            )
    try:
        bootstrap_user(
            config_dir,
            username=request.username,
            password=request.password,
            display_name=request.display_name,
            email=request.email,
            groups=request.groups,
        )
        render_configuration(config, CONFIG_MANAGER.get("postgres") or {})
    except AutheliaSetupError as exc:
        raise HTTPException(400, detail=str(exc)) from None
    _save_service_config(config)

    started = False
    if request.start_service:
        process, error = updater.auto_update(
            config.get("process_name", "Authelia"),
            enable_update=bool(config.get("auto_update")),
        )
        if not process:
            raise HTTPException(
                500,
                detail=f"Authelia was configured but failed to start: {error or ''}",
            )
        started = True
    logger.info("DUMB-managed Authelia bootstrap completed")
    return {
        "configured": True,
        "started": started,
        "public_url": public_url,
        "cookie_domain": cookie_domain,
    }


@authelia_router.post("/link-dumb")
def link_dumb_auth(
    request: LinkDumbRequest,
    _current_user: str = Depends(get_optional_current_user),
    updater=Depends(get_updater),
    logger=Depends(get_logger),
):
    dumb_base = _absolute_base_url(request.dumb_public_url, "DUMB public URL")
    redirect_uri = f"{dumb_base}/api/auth/oidc/callback"
    if request.mode == "oidc" and not request.confirm_oidc_only:
        raise HTTPException(
            400, detail="OIDC-only mode requires explicit lockout-risk confirmation"
        )

    if request.source == "managed":
        config = _service_config()
        issuer = str(config.get("public_url") or "")
        if not issuer:
            raise HTTPException(400, detail="Bootstrap managed Authelia first")
        credentials = ensure_oidc_client(
            str(config.get("config_dir") or "/config/authelia"),
            key="dumb",
            client_id="dumb",
            client_name="DUMB",
            redirect_uri=redirect_uri,
            authorization_policy=str(
                config.get("authorization_policy") or "two_factor"
            ),
        )
        render_configuration(config, CONFIG_MANAGER.get("postgres") or {})
        _restart_managed_authelia(config, updater, logger)
        source = "managed"
    else:
        issuer = request.issuer_url.strip().rstrip("/")
        credentials = {
            "client_id": request.client_id.strip(),
            "client_secret": request.client_secret,
        }
        source = request.source
    if not issuer or not credentials["client_id"] or not credentials["client_secret"]:
        raise HTTPException(
            400, detail="Issuer URL and OIDC client credentials are required"
        )

    AUTH_CONFIG.update_auth_provider(
        request.mode,
        {
            "enabled": True,
            "provider_name": request.provider_name.strip() or "Single Sign-On",
            "source": source,
            "issuer_url": issuer,
            "discovery_url": request.discovery_url.strip(),
            "authorization_endpoint": "",
            "token_endpoint": "",
            "userinfo_endpoint": "",
            "jwks_uri": "",
            "client_id": credentials["client_id"],
            "client_secret": credentials["client_secret"],
            "redirect_uri": redirect_uri,
            "scopes": request.scopes,
            "username_claim": request.username_claim,
            "groups_claim": request.groups_claim,
            "allowed_groups": request.allowed_groups,
            "tls_verify": request.tls_verify,
            "allow_private_endpoints": request.allow_private_endpoints,
            "allow_http": request.allow_http,
            "timeout_seconds": 10,
        },
    )
    AUTH_CONFIG.enable_auth()
    logger.warning(
        "DUMB authentication linked to %s OIDC in %s mode", source, request.mode
    )
    return {
        "linked": True,
        "mode": request.mode,
        "source": source,
        "redirect_uri": redirect_uri,
        "local_fallback_enabled": request.mode == "hybrid",
    }


@authelia_router.post("/link-tpa")
def link_tpa(
    request: LinkTPARequest,
    _current_user: str = Depends(get_optional_current_user),
    updater=Depends(get_updater),
    logger=Depends(get_logger),
):
    config = _service_config()
    issuer = str(config.get("public_url") or "")
    if not issuer:
        raise HTTPException(400, detail="Bootstrap managed Authelia first")
    tpa = CONFIG_MANAGER.get("traefik_proxy_admin") or {}
    if not tpa.get("enabled"):
        raise HTTPException(400, detail="Enable Traefik Proxy Admin before linking it")
    tpa_base = _absolute_base_url(request.tpa_public_url, "TPA public URL")
    redirect_uri = f"{tpa_base}/api/auth/sso/callback"
    credentials = ensure_oidc_client(
        str(config.get("config_dir") or "/config/authelia"),
        key="tpa",
        client_id="traefik-proxy-admin",
        client_name="Traefik Proxy Admin",
        redirect_uri=redirect_uri,
        authorization_policy=str(config.get("authorization_policy") or "two_factor"),
    )
    render_configuration(config, CONFIG_MANAGER.get("postgres") or {})
    _restart_managed_authelia(config, updater, logger)
    original_env = (
        dict(tpa.get("env") or {}) if isinstance(tpa.get("env"), dict) else {}
    )
    token, token_created = _ensure_tpa_token(tpa)

    env = dict(tpa.get("env") or {}) if isinstance(tpa.get("env"), dict) else {}
    if request.configure_admin_sso:
        env["ADMIN_AUTH_PROVIDER"] = "sso"
    auth_host = urlparse(issuer).hostname or ""
    allowed_hosts = {
        item.strip()
        for item in str(env.get("SSO_ENDPOINT_ALLOW_HOSTS") or "").split(",")
        if item.strip()
    }
    if auth_host:
        allowed_hosts.add(auth_host)
    env["SSO_ENDPOINT_ALLOW_HOSTS"] = ",".join(sorted(allowed_hosts))
    tpa["env"] = env
    CONFIG_MANAGER.save_config(tpa.get("process_name", "Traefik Proxy Admin"))
    environment_changed = env != original_env

    restarted = False
    if token_created:
        updater.stop_process(tpa.get("process_name", "Traefik Proxy Admin"))
        process, _ = updater.auto_update(
            tpa.get("process_name", "Traefik Proxy Admin"),
            enable_update=False,
        )
        if not process:
            logger.error(
                "TPA could not restart after its integration environment changed"
            )
            raise HTTPException(
                500,
                detail="TPA integration environment was saved but TPA could not restart",
            )
        restarted = True
    endpoint = (
        f"http://127.0.0.1:{int(tpa.get('port', 3004))}"
        "/api/integrations/dumb/authelia/link"
    )
    try:
        response = requests.post(
            endpoint,
            json={
                "providerName": request.provider_name,
                "issuerUrl": issuer,
                "authorizationUrl": f"{issuer}/api/oidc/authorization",
                "tokenUrl": f"{issuer}/api/oidc/token",
                "userinfoUrl": f"{issuer}/api/oidc/userinfo",
                "clientId": credentials["client_id"],
                "clientSecret": credentials["client_secret"],
                "tokenEndpointAuthMethod": "client_secret_basic",
                "redirectUri": redirect_uri,
                "scopes": ["openid", "profile", "email", "groups"],
                "configureAdminSso": request.configure_admin_sso,
                "allowLocalFallback": request.allow_local_fallback,
                "adminGroups": request.admin_groups,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError):
        logger.error("TPA Authelia linking failed")
        raise HTTPException(
            502,
            detail=(
                "TPA link request failed. Ensure TPA has been restarted once after "
                "DUMB generated its integration token."
            ),
        ) from None

    if request.restart_tpa and not restarted:
        updater.stop_process(tpa.get("process_name", "Traefik Proxy Admin"))
        process, _ = updater.auto_update(
            tpa.get("process_name", "Traefik Proxy Admin"),
            enable_update=False,
        )
        if not process:
            logger.error("TPA could not restart after Authelia linking")
            raise HTTPException(500, detail="TPA linked but its restart failed")
        restarted = True
    logger.info("Traefik Proxy Admin linked to DUMB-managed Authelia")
    restart_required = environment_changed and not restarted
    safe_result = _safe_tpa_link_result(result)
    return {
        **safe_result,
        "environmentPersisted": True,
        "restartCompleted": restarted,
        "restartRequired": restart_required,
        "integrationActive": safe_result["linked"] and not restart_required,
        "redirectUri": redirect_uri,
    }


@authelia_router.post("/forward-auth")
def configure_forward_auth(
    request: ForwardAuthRequest,
    _current_user: str = Depends(get_optional_current_user),
):
    if request.source == "managed":
        config = _service_config()
        address = (
            f"http://127.0.0.1:{int(config.get('port', 9091))}"
            "/api/authz/forward-auth"
        )
    else:
        address = request.address.strip()
        parsed = urlparse(address)
        schemes = {"https", "http"} if request.allow_http else {"https"}
        if parsed.scheme not in schemes or not parsed.hostname:
            raise HTTPException(
                400, detail="External ForwardAuth address must be a valid HTTPS URL"
            )
    path = Path(get_traefik_dynamic_config_dir(), "authelia.yml")
    write_traefik_config(
        path,
        {
            "http": {
                "middlewares": {
                    "dumb-authelia-forward-auth": {
                        "forwardAuth": {
                            "address": address,
                            "trustForwardHeader": True,
                            "authResponseHeaders": [
                                "Remote-User",
                                "Remote-Groups",
                                "Remote-Name",
                                "Remote-Email",
                            ],
                        }
                    }
                }
            }
        },
    )
    return {
        "configured": True,
        "middleware": "dumb-authelia-forward-auth@file",
        "source": request.source,
    }
