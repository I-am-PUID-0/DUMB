"""OIDC Authorization Code + PKCE support for DUMB authentication."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import secrets
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import jwt
import requests


class OIDCError(RuntimeError):
    """Safe OIDC configuration or flow error."""


@dataclass
class PendingAuthorization:
    nonce: str
    verifier: str
    return_to: str
    expires_at: float


@dataclass
class PendingExchange:
    username: str
    groups: list[str]
    return_to: str
    expires_at: float


def _string_list(value: Any, default: list[str] | None = None) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item for item in value.replace(",", " ").split() if item]
    return list(default or [])


def _safe_return_to(value: str | None) -> str:
    candidate = str(value or "/").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    if "\\" in candidate or "\r" in candidate or "\n" in candidate:
        return "/"
    return candidate[:2048]


def _validate_endpoint_url(url: str, *, allow_private: bool, allow_http: bool) -> str:
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError as exc:
        raise OIDCError("OIDC endpoint URL is invalid") from exc
    if parsed.scheme not in ({"https", "http"} if allow_http else {"https"}):
        raise OIDCError("OIDC endpoints must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise OIDCError("OIDC endpoint URL must contain a valid hostname")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise OIDCError("OIDC endpoint hostname could not be resolved") from exc
    if not addresses:
        raise OIDCError("OIDC endpoint hostname did not resolve")
    if not allow_private:
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                raise OIDCError(
                    "OIDC endpoint resolves to a private or reserved address; "
                    "explicitly allow private endpoints if this is intentional"
                )
    return parsed.geturl()


def _request_json(
    method: str,
    url: str,
    config: dict[str, Any],
    **kwargs,
) -> dict[str, Any]:
    allow_private = bool(config.get("allow_private_endpoints"))
    allow_http = bool(config.get("allow_http"))
    endpoint = _validate_endpoint_url(
        url, allow_private=allow_private, allow_http=allow_http
    )
    timeout = max(2, min(30, int(config.get("timeout_seconds", 10) or 10)))
    try:
        response = requests.request(
            method,
            endpoint,
            timeout=timeout,
            verify=bool(config.get("tls_verify", True)),
            allow_redirects=False,
            **kwargs,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise OIDCError("OIDC provider request failed") from exc
    if not isinstance(payload, dict):
        raise OIDCError("OIDC provider returned an invalid response")
    return payload


def discover(config: dict[str, Any]) -> dict[str, Any]:
    issuer = str(config.get("issuer_url") or "").strip().rstrip("/")
    if not issuer:
        raise OIDCError("OIDC issuer URL is required")
    discovery_url = str(config.get("discovery_url") or "").strip()
    if not discovery_url:
        discovery_url = f"{issuer}/.well-known/openid-configuration"
    metadata = _request_json("GET", discovery_url, config)
    if str(metadata.get("issuer") or "").rstrip("/") != issuer:
        raise OIDCError("OIDC discovery issuer does not match the configured issuer")
    return metadata


def resolved_endpoints(config: dict[str, Any]) -> dict[str, str]:
    metadata = discover(config)
    resolved = {"issuer": str(metadata.get("issuer") or "").rstrip("/")}
    for key in (
        "authorization_endpoint",
        "token_endpoint",
        "userinfo_endpoint",
        "jwks_uri",
    ):
        value = str(config.get(key) or metadata.get(key) or "").strip()
        if key != "userinfo_endpoint" and not value:
            raise OIDCError(f"OIDC provider did not advertise {key}")
        if value:
            resolved[key] = _validate_endpoint_url(
                value,
                allow_private=bool(config.get("allow_private_endpoints")),
                allow_http=bool(config.get("allow_http")),
            )
    return resolved


class OIDCFlowManager:
    """Bounded in-memory OIDC state and one-time token exchanges."""

    def __init__(self) -> None:
        self._authorizations: dict[str, PendingAuthorization] = {}
        self._exchanges: dict[str, PendingExchange] = {}
        self._lock = threading.Lock()

    def _prune(self) -> None:
        now = time.time()
        self._authorizations = {
            key: value
            for key, value in self._authorizations.items()
            if value.expires_at > now
        }
        self._exchanges = {
            key: value
            for key, value in self._exchanges.items()
            if value.expires_at > now
        }

    def begin(self, config: dict[str, Any], return_to: str | None) -> str:
        endpoints = resolved_endpoints(config)
        client_id = str(config.get("client_id") or "").strip()
        redirect_uri = str(config.get("redirect_uri") or "").strip()
        if not client_id or not redirect_uri:
            raise OIDCError("OIDC client ID and redirect URI are required")

        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        with self._lock:
            self._prune()
            if len(self._authorizations) >= 256:
                raise OIDCError("Too many OIDC authorization attempts")
            self._authorizations[state] = PendingAuthorization(
                nonce=nonce,
                verifier=verifier,
                return_to=_safe_return_to(return_to),
                expires_at=time.time() + 600,
            )

        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(
                    _string_list(
                        config.get("scopes"),
                        ["openid", "profile", "email", "groups"],
                    )
                ),
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{endpoints['authorization_endpoint']}?{query}"

    def complete(
        self, config: dict[str, Any], state: str, code: str
    ) -> tuple[str, str]:
        with self._lock:
            self._prune()
            pending = self._authorizations.pop(state, None)
        if not pending:
            raise OIDCError("OIDC state is invalid or expired")
        if not code:
            raise OIDCError("OIDC authorization code is missing")

        endpoints = resolved_endpoints(config)
        client_id = str(config.get("client_id") or "").strip()
        client_secret = str(config.get("client_secret") or "")
        redirect_uri = str(config.get("redirect_uri") or "").strip()
        token = _request_json(
            "POST",
            endpoints["token_endpoint"],
            config,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": pending.verifier,
            },
            auth=(client_id, client_secret),
            headers={"Accept": "application/json"},
        )
        id_token = str(token.get("id_token") or "")
        if not id_token:
            raise OIDCError("OIDC provider did not return an ID token")
        claims = self._validate_id_token(
            id_token,
            endpoints,
            config,
            client_id=client_id,
            nonce=pending.nonce,
        )

        access_token = str(token.get("access_token") or "")
        if endpoints.get("userinfo_endpoint") and access_token:
            userinfo = _request_json(
                "GET",
                endpoints["userinfo_endpoint"],
                config,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
            )
            if userinfo.get("sub") and userinfo.get("sub") != claims.get("sub"):
                raise OIDCError("OIDC userinfo subject does not match the ID token")
            claims.update(userinfo)

        username_claim = str(config.get("username_claim") or "preferred_username")
        username = str(
            claims.get(username_claim) or claims.get("email") or claims.get("sub") or ""
        ).strip()
        if not username:
            raise OIDCError("OIDC identity does not include a usable username")
        groups_claim = str(config.get("groups_claim") or "groups")
        groups = _string_list(claims.get(groups_claim))
        allowed_groups = {
            group.casefold() for group in _string_list(config.get("allowed_groups"))
        }
        if allowed_groups and not allowed_groups.intersection(
            group.casefold() for group in groups
        ):
            raise OIDCError("OIDC identity is not in an allowed group")

        exchange_code = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            self._exchanges[exchange_code] = PendingExchange(
                username=username,
                groups=groups,
                return_to=pending.return_to,
                expires_at=time.time() + 60,
            )
        return exchange_code, pending.return_to

    def _validate_id_token(
        self,
        encoded: str,
        endpoints: dict[str, str],
        config: dict[str, Any],
        *,
        client_id: str,
        nonce: str,
    ) -> dict[str, Any]:
        jwks = _request_json("GET", endpoints["jwks_uri"], config)
        keys = jwks.get("keys")
        if not isinstance(keys, list):
            raise OIDCError("OIDC provider returned an invalid JWKS")
        try:
            header = jwt.get_unverified_header(encoded)
            kid = header.get("kid")
            algorithm = str(header.get("alg") or "")
            if algorithm not in {"RS256", "RS384", "RS512", "ES256", "ES384"}:
                raise OIDCError("OIDC ID token uses an unsupported algorithm")
            candidates = [
                item
                for item in keys
                if isinstance(item, dict) and (not kid or item.get("kid") == kid)
            ]
            if len(candidates) != 1:
                raise OIDCError("OIDC ID token signing key was not uniquely identified")
            key = jwt.PyJWK.from_dict(candidates[0], algorithm=algorithm).key
            claims = jwt.decode(
                encoded,
                key=key,
                algorithms=[algorithm],
                audience=client_id,
                issuer=endpoints["issuer"],
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except OIDCError:
            raise
        except jwt.PyJWTError as exc:
            raise OIDCError("OIDC ID token validation failed") from exc
        if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise OIDCError("OIDC nonce validation failed")
        return claims

    def redeem(self, exchange_code: str) -> PendingExchange:
        with self._lock:
            self._prune()
            exchange = self._exchanges.pop(exchange_code, None)
        if not exchange:
            raise OIDCError("OIDC exchange code is invalid or expired")
        return exchange


OIDC_FLOWS = OIDCFlowManager()
