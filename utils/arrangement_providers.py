"""NNTP account bootstrap and import-layout adapters.

Contracts inspected against official source on 2026-10-14:
InfiniDysk 28447abac5e7688ab4d6c779d02108d46dcae645:
  backend/Api/Controllers/{GetConfig,UpdateConfig,TestUsenetConnection}
  backend/Config/{UsenetProviderConfig,ConfigSecretMasker}.cs
AltMount fdfa7ee8f5e4f5716cbf432236cc4c8ad9c50334:
  internal/api/config_handlers.go; internal/config/manager.go
Decypharr ea020e6db500571a3556c096d50d1e4b0db69907:
  internal/config/usenet.go; pkg/server/{api,auth}.go

Unknown response shapes fail closed. NNTP checks authenticate one connection;
they never request articles, download content, run speed tests or change quotas.
"""

from __future__ import annotations

import copy
from contextvars import ContextVar
import json
import os
from pathlib import Path
import re
import socket
import ssl
import uuid

from utils.arrangement_http import ApiClient, ApiError, validate_endpoint
from utils.integration_ownership import fingerprint, read_ownership, write_ownership

PROVIDERS = frozenset({"infinidysk", "altmount", "decypharr"})
_SETTINGS_HANDOFF = ContextVar("provider_settings_handoff", default=None)


class ProviderInputError(ValueError):
    pass


class ProviderOwnershipConflict(ValueError):
    pass


class UnsupportedProviderVersion(ValueError):
    pass


def _state(
    execution,
    reason,
    *,
    verification="not_checked",
    retryable=False,
    missing_inputs=(),
    evidence=None,
):
    return {
        "execution": execution,
        "verification": verification,
        "reason": reason,
        "retryable": retryable,
        "missing_inputs": list(missing_inputs),
        "evidence": evidence or {},
    }


def _resource(config, identifier):
    key, _, instance = str(identifier).partition(".")
    block = config.get(key) or {}
    return (block.get("instances") or {}).get(instance, {}) if instance else block


def _values(payload):
    return (payload.get("arrangement") or {}).get("values") or {}


def schema_fields(resource, config, payload=None):
    del config, payload
    if resource["service_key"] not in PROVIDERS:
        return []
    rid = resource["id"]

    def field(name, label, kind, description, **extra):
        return {
            "key": f"{rid}::{name}",
            "resource_id": rid,
            "service_key": resource["service_key"],
            "label": label,
            "type": kind,
            "description": description,
            "required": False,
            **extra,
        }

    fields = [
        field(
            "allow_private_nntp",
            "Allow a private NNTP server",
            "boolean",
            "Authorize authentication checks against the private NNTP endpoints you supplied. Public provider endpoints need no exception.",
        )
    ]
    if resource["service_key"] == "altmount":
        fields += [
            field(
                "admin_username",
                "AltMount administrator name",
                "string",
                "Authorize an existing administrator, or explicitly create the first administrator through the guarded fresh-server account action.",
            ),
            field(
                "admin_password",
                "AltMount temporary administrator password",
                "password",
                "Exchanged for a short-lived settings session and removed from retained inputs after successful authorization. The download-client API key does not authorize administrator settings.",
            ),
            field(
                "settings_token",
                "AltMount settings session",
                "password",
                "Usually obtained by the temporary administrator login. This protected session expires; authorize again when AltMount rejects it.",
            ),
            field(
                "import_strategy",
                "AltMount library output",
                "select",
                "NONE leaves import output disabled. SYMLINK and STRM require an explicit library directory; no existing files are moved.",
                options=[
                    {"value": value, "label": value}
                    for value in ("NONE", "SYMLINK", "STRM")
                ],
            ),
            field(
                "import_dir",
                "AltMount import library directory",
                "string",
                "Absolute directory as seen inside DUMB. Existing library contents and scan/sync settings are preserved.",
            ),
        ]
    return fields


def _normalize_provider(item):
    if not isinstance(item, dict):
        raise ProviderInputError("Each NNTP provider must be an object.")
    host = item.get("host")
    username, password = item.get("username", ""), item.get("password", "")
    if (
        not isinstance(host, str)
        or not host.strip()
        or len(host) > 253
        or any(ch in host for ch in "/@?#\\\r\n\t ")
    ):
        raise ProviderInputError(
            "Use an NNTP hostname or IP address without URL components."
        )
    if (
        not isinstance(username, str)
        or not isinstance(password, str)
        or not username
        or not password
        or any(ch in username + password for ch in "\r\n\0")
        or len(username) > 1024
        or len(password) > 4096
    ):
        raise ProviderInputError(
            "Provide an NNTP username and password without control characters."
        )
    port = item.get("port", 563)
    connections = item.get("connections", 5)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ProviderInputError("Choose an NNTP port from 1 to 65535.")
    if (
        isinstance(connections, bool)
        or not isinstance(connections, int)
        or not 1 <= connections <= 200
    ):
        raise ProviderInputError("Choose 1–200 connections for each NNTP allocation.")
    tls = item.get("tls", True)
    if not isinstance(tls, bool):
        raise ProviderInputError("Choose whether this NNTP endpoint uses TLS.")
    ceiling = item.get("account_connection_limit")
    if ceiling is not None and (
        isinstance(ceiling, bool)
        or not isinstance(ceiling, int)
        or not 1 <= ceiling <= 500
    ):
        raise ProviderInputError(
            "The shared account connection limit must be between 1 and 500."
        )
    account = item.get("account_id") or ""
    if not isinstance(account, str) or len(account) > 100:
        raise ProviderInputError(
            "Use a short account label for shared account allocations."
        )
    name = item.get("name") or ""
    if not isinstance(name, str) or len(name) > 100:
        raise ProviderInputError("Use a short provider label.")
    return {
        "host": host.strip().strip("[]").lower(),
        "port": port,
        "username": username,
        "password": password,
        "tls": tls,
        "connections": connections,
        "account_connection_limit": ceiling,
        "account_id": account,
        "name": name,
    }


def validate_connection_budgets(payload):
    accounts = {}
    total = 0
    for key, supplied in _values(payload).items():
        if not str(key).endswith("::nntp"):
            continue
        if not isinstance(supplied, list) or len(supplied) > 50:
            raise ProviderInputError("Provide a bounded list of NNTP accounts.")
        seen = set()
        for entry in supplied:
            item = _normalize_provider(entry)
            endpoint = (item["host"], item["port"], item["username"])
            if endpoint in seen:
                raise ProviderInputError(
                    "An NNTP account endpoint is listed twice for one application."
                )
            seen.add(endpoint)
            # Explicit account labels handle one account with regional host aliases.
            identity = (
                ("label", item["account_id"])
                if item["account_id"]
                else ("host", item["host"], item["username"])
            )
            account = accounts.setdefault(
                identity, {"connections": 0, "limits": [], "allocations": 0}
            )
            account["connections"] += item["connections"]
            account["allocations"] += 1
            if item["account_connection_limit"] is not None:
                account["limits"].append(item["account_connection_limit"])
            total += item["connections"]
    for account in accounts.values():
        if account["limits"] and account["connections"] > min(account["limits"]):
            raise ProviderInputError(
                "The combined NNTP allocations exceed a declared account connection limit. Reduce connections or correct the account ceiling."
            )
    if total > 500:
        raise ProviderInputError(
            "Review the total NNTP connection budget; more than 500 connections were requested."
        )
    return {
        "requested_connections": total,
        "shared_accounts": sum(item["allocations"] > 1 for item in accounts.values()),
        "accounts_without_declared_limit": sum(
            not item["limits"] for item in accounts.values()
        ),
    }


def _request(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise UnsupportedProviderVersion(
            "The application returned an unsupported response shape."
        )
    if value.get("success") is False or value.get("status") is False:
        raise ApiError("The application rejected the setup request.")
    return value


def _unwrap(value):
    if "data" in value:
        if not isinstance(value["data"], dict):
            raise UnsupportedProviderVersion("Unsupported application data envelope.")
        return value["data"]
    return value


def _client(service, cfg, factory, *, payload=None, allow_login=False):
    if service == "infinidysk" and str(cfg.get("runtime_generation") or "1") == "2":
        raise UnsupportedProviderVersion(
            "InfiniDysk 2 settings require its distinct native API/UI; the legacy provider adapter is not applied."
        )
    port = (
        cfg.get("backend_port", 8080)
        if service == "infinidysk"
        else cfg.get("port", 8088 if service == "altmount" else 8282)
    )
    token = (
        (cfg.get("env") or {}).get("FRONTEND_BACKEND_API_KEY", "")
        if service == "infinidysk"
        else (cfg.get("env") or {}).get("ALTMOUNT_API_KEY", "")
    )
    base = ""
    settings_cookie = False
    if service == "decypharr":
        path = Path(cfg.get("config_dir") or "/decypharr") / "auth.json"
        if path.is_symlink():
            raise ProviderInputError(
                "Decypharr credentials must be a regular configuration file."
            )
        try:
            token = json.loads(path.read_text()).get("api_token") or ""
        except FileNotFoundError:
            token = cfg.get("api_key") or ""
        config_path = Path(cfg.get("config_file") or str(path.parent / "config.json"))
        if config_path.is_file() and not config_path.is_symlink():
            loaded = json.loads(config_path.read_text())
            base = str(loaded.get("url_base") or "").rstrip("/")
    if base and (
        not base.startswith("/")
        or base.startswith("//")
        or any(ch in base for ch in "?#\\\r\n")
    ):
        raise ProviderInputError("The application URL base must be a path.")
    if service == "altmount":
        values = _values(payload or {})
        session = values.get("altmount::settings_token")
        username, password = values.get("altmount::admin_username"), values.get(
            "altmount::admin_password"
        )
        if (password and not username) or (username and not password and not session):
            raise ProviderInputError(
                "Provide both the AltMount administrator name and temporary password, or reuse a valid settings session."
            )
        if username and password:
            if not allow_login:
                raise ProviderInputError(
                    "Apply this action to authorize an AltMount settings session; preview does not sign in or change accounts."
                )
            if (
                not isinstance(username, str)
                or not isinstance(password, str)
                or len(username) > 200
                or len(password) > 4096
            ):
                raise ProviderInputError(
                    "Use valid bounded AltMount administrator credentials."
                )
            login = factory(f"http://127.0.0.1:{int(port)}", managed=True, timeout=10)
            response = _unwrap(
                _request(
                    login,
                    "POST",
                    "/api/auth/login",
                    json={"username": username, "password": password},
                )
            )
            user = response.get("user")
            if not isinstance(user, dict) or user.get("is_admin") is not True:
                raise ProviderInputError(
                    "AltMount settings changes require an existing administrator account."
                )
            session = getattr(login, "cookies", {}).get("JWT")
            if not isinstance(session, str) or not re.fullmatch(
                r"[A-Za-z0-9_.-]{20,4096}", session
            ):
                raise UnsupportedProviderVersion(
                    "AltMount did not return a supported administrator settings session."
                )
            handoff = _SETTINGS_HANDOFF.get()
            if handoff is not None:
                handoff["altmount::settings_token"] = session
                handoff["_altmount_session_unverified"] = True
        if session:
            if not isinstance(session, str) or not re.fullmatch(
                r"[A-Za-z0-9_.-]{20,4096}", session
            ):
                raise ProviderInputError(
                    "The protected AltMount settings session is invalid; authorize again."
                )
            token = session
            settings_cookie = True
    if not token:
        raise ProviderInputError(
            "Wait for the application's API key or finish its first account authorization, then retry."
        )
    headers = (
        {"Cookie": "JWT=" + token}
        if settings_cookie
        else (
            {"X-Api-Key": token}
            if service == "infinidysk"
            else {"Authorization": f"Bearer {token}"}
        )
    )
    return factory(
        f"http://127.0.0.1:{int(port)}{base}", headers=headers, managed=True, timeout=10
    )


def _ownership_path(cfg):
    directory = cfg.get("config_dir") or os.path.dirname(cfg.get("config_file") or "")
    if not directory:
        raise ProviderInputError(
            "The application configuration directory is not ready."
        )
    return str(Path(directory) / ".dumb-nntp-ownership.json")


def _account_identity(item, service):
    if service == "infinidysk":
        return str(item.get("Host", "")).lower(), item.get("Port"), item.get("User")
    return str(item.get("host", "")).lower(), item.get("port"), item.get("username")


def _managed_snapshot(item, service):
    keys = (
        ("Host", "Port", "User", "UseSsl", "MaxConnections")
        if service == "infinidysk"
        else (
            "host",
            "port",
            "username",
            "tls" if service == "altmount" else "ssl",
            "max_connections",
        )
    )
    snapshot = {key: item.get(key) for key in keys}
    if service == "decypharr":
        # Go's ssl,omitempty omits false in native readback. Absence means
        # plaintext, not a user modification or a failed configuration write.
        snapshot["ssl"] = item.get("ssl", False)
    return snapshot


def _desired(item, service):
    if service == "infinidysk":
        return {
            "Host": item["host"],
            "Port": item["port"],
            "User": item["username"],
            "Pass": item["password"],
            "UseSsl": item["tls"],
            "MaxConnections": item["connections"],
        }
    return {
        "host": item["host"],
        "port": item["port"],
        "username": item["username"],
        "password": item["password"],
        "tls" if service == "altmount" else "ssl": item["tls"],
        "max_connections": item["connections"],
    }


def _read_config(client, service, cfg=None):
    if service == "infinidysk":
        value = _request(
            client,
            "POST",
            "/api/get-config",
            data={"config-keys": ["usenet.providers"]},
        )
        items = value.get("configItems", value.get("ConfigItems"))
        if not isinstance(items, list):
            raise UnsupportedProviderVersion(
                "InfiniDysk does not expose the supported configuration API."
            )
        raw = next(
            (
                item.get("configValue", item.get("ConfigValue"))
                for item in items
                if item.get("configName", item.get("ConfigName")) == "usenet.providers"
            ),
            None,
        )
        if raw is None:
            return {"Providers": []}, []
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or not isinstance(
            parsed.get("Providers"), list
        ):
            raise UnsupportedProviderVersion(
                "InfiniDysk provider settings use an unsupported schema."
            )
        return parsed, parsed["Providers"]
    try:
        value = _unwrap(_request(client, "GET", "/api/config"))
    except ApiError as exc:
        if service == "decypharr" and exc.status_code == 503:
            path = Path(
                (cfg or {}).get("config_file")
                or str(
                    Path((cfg or {}).get("config_dir") or "/decypharr") / "config.json"
                )
            )
            if path.is_file() and not path.is_symlink():
                local = json.loads(path.read_text())
                if (
                    isinstance(local, dict)
                    and local.get("url_base")
                    and not local.get("debrids")
                    and not (local.get("usenet") or {}).get("providers")
                ):
                    raise ProviderInputError(
                        "Decypharr's first-run API is blocked by its configured URL base. Complete its initial setup at the application root before restoring that URL base, or select an upstream version that fixes this limitation."
                    ) from None
        raise
    if service == "altmount":
        handoff = _SETTINGS_HANDOFF.get()
        if handoff is not None:
            # A successful authenticated settings GET proves the login cookie
            # works before discarding the temporary administrator password.
            handoff.pop("_altmount_session_unverified", None)
        providers = value.get("providers")
    else:
        providers = (value.get("usenet") or {}).get("providers", [])
        if "usenet" not in value:
            # Empty Usenet structs are omitted from fresh v2 JSON responses.
            try:
                installed = (
                    (
                        Path((cfg or {}).get("config_dir") or "/decypharr")
                        / "version.txt"
                    )
                    .read_text()
                    .strip()
                )
            except FileNotFoundError:
                installed = ""
            version = re.match(r"^v?(\d+)\.(\d+)", installed)
            if not version or tuple(map(int, version.groups())) < (2, 3):
                raise UnsupportedProviderVersion(
                    "This Decypharr version does not expose a supported Usenet configuration section."
                )
            value["usenet"] = {}
    if not isinstance(providers, list) or any(
        not isinstance(item, dict) for item in providers
    ):
        raise UnsupportedProviderVersion("Provider settings use an unsupported schema.")
    return value, providers


def authenticate_nntp(item, *, allow_private=False):
    """Authenticate one pinned NNTP connection, without content commands."""
    host = item["host"]
    bracketed = f"[{host}]" if ":" in host else host
    _parts, port, addresses = validate_endpoint(
        f"https://{bracketed}:{item['port']}", allow_private=allow_private
    )
    connection = socket.create_connection((addresses[0], port), timeout=8)
    stream = None
    try:
        if item["tls"]:
            context = ssl.create_default_context()
            # Keep certificate/hostname verification and any stricter platform
            # policy; never lower an existing TLS 1.3 minimum to TLS 1.2.
            minimum = context.minimum_version
            context.minimum_version = (
                ssl.TLSVersion.TLSv1_2 if minimum < ssl.TLSVersion.TLSv1_2 else minimum
            )
            connection = context.wrap_socket(connection, server_hostname=host)
        connection.settimeout(8)
        stream = connection.makefile("rwb")

        def response():
            line = stream.readline(8193)
            if len(line) > 8192 or len(line) < 3 or not line[:3].isdigit():
                raise ApiError("The NNTP server returned an invalid greeting.")
            return int(line[:3])

        def command(value):
            stream.write(value.encode("utf-8") + b"\r\n")
            stream.flush()
            return response()

        if response() not in (200, 201):
            return False
        code = command("AUTHINFO USER " + item["username"])
        if code == 381:
            code = command("AUTHINFO PASS " + item["password"])
        if code != 281:
            return False
        try:
            stream.write(b"QUIT\r\n")
            stream.flush()
        except OSError:
            pass
        return True
    finally:
        try:
            if stream is not None:
                stream.close()
        finally:
            connection.close()


def _authenticate(client, service, supplied, *, allow_private, nntp_authenticator):
    for item in supplied:
        # Validate endpoints before asking an application to contact them.
        host = f"[{item['host']}]" if ":" in item["host"] else item["host"]
        validate_endpoint(f"https://{host}:{item['port']}", allow_private=allow_private)
        if service == "infinidysk":
            test = _request(
                client,
                "POST",
                "/api/test-usenet-connection",
                data={
                    "host": item["host"],
                    "port": item["port"],
                    "user": item["username"],
                    "pass": item["password"],
                    "use-ssl": str(item["tls"]).lower(),
                    "skip-tls-verification": "false",
                },
            )
            if test.get("connected", test.get("Connected")) is not True:
                return False
        elif service == "altmount":
            test = _unwrap(
                _request(
                    client,
                    "POST",
                    "/api/providers/test",
                    json={
                        "host": item["host"],
                        "port": item["port"],
                        "username": item["username"],
                        "password": item["password"],
                        "tls": item["tls"],
                        "insecure_tls": False,
                    },
                )
            )
            if test.get("success") is not True:
                return False
        elif not nntp_authenticator(item, allow_private=allow_private):
            return False
    return True


def _nntp(action, config, payload, *, apply, client_factory, nntp_authenticator):
    service, source = action["service_key"], action["source"]
    cfg = _resource(config, source)
    values = _values(payload)
    key = f"{source}::nntp"
    supplied = values.get(key) or []
    if not supplied:
        return _state(
            "waiting_input",
            "Provide the NNTP accounts to configure and verify. Existing accounts remain untouched.",
            missing_inputs=[key],
        )
    supplied = [_normalize_provider(item) for item in supplied]
    budget = validate_connection_budgets(payload)
    client = _client(service, cfg, client_factory, payload=payload, allow_login=apply)
    current, existing = _read_config(client, service, cfg)
    ledger_path = _ownership_path(cfg)
    ownership = read_ownership(ledger_path)
    desired_list = copy.deepcopy(existing)
    changes = []
    for entry in supplied:
        target = _desired(entry, service)
        identity = _account_identity(target, service)
        matches = [
            item
            for item in desired_list
            if _account_identity(item, service) == identity
        ]
        if len(matches) > 1:
            raise ProviderOwnershipConflict(
                "Multiple provider entries match an account. Resolve the duplicate in the application before retrying."
            )
        item = matches[0] if matches else None
        owner_key = fingerprint(identity)
        record = ownership.get(owner_key) or {}
        public_target = _managed_snapshot(target, service)
        if item is not None:
            current_snapshot = _managed_snapshot(item, service)
            stable_id = (
                item.get("ProviderId")
                if service == "infinidysk"
                else item.get("id") if service == "altmount" else fingerprint(identity)
            )
            owned = record.get("id") == stable_id and record.get(
                "fingerprint"
            ) == fingerprint(current_snapshot)
            if item.get("enabled") is False or (
                service == "infinidysk" and item.get("Type") == 0
            ):
                raise ProviderOwnershipConflict(
                    "An existing account was deliberately disabled. Enable it in the application when ready."
                )
            if service == "decypharr":
                owned = owned and record.get("credential_fingerprint") == fingerprint(
                    item.get("password")
                )
            credential_matches = (
                item.get("password") == entry["password"]
                if service == "decypharr"
                else record.get("credential_fingerprint")
                == fingerprint(entry["password"])
            )
            if current_snapshot == public_target and credential_matches:
                continue
            if not owned:
                raise ProviderOwnershipConflict(
                    "An existing account is user-managed or was edited outside DUMB. It was preserved; update it in the application or choose a distinct account."
                )
            if item.get("enabled") is False or (
                service == "infinidysk" and item.get("Type") == 0
            ):
                raise ProviderOwnershipConflict(
                    "An existing account was deliberately disabled. Enable it in the application when ready."
                )
            merged = {**item, **target}
            desired_list[desired_list.index(item)] = merged
        else:
            merged = dict(target)
            if service == "infinidysk":
                merged.update(ProviderId=str(uuid.uuid4()), Type=1)
            elif service == "altmount":
                merged["enabled"] = True
                if entry["name"]:
                    merged["name"] = entry["name"]
            desired_list.append(merged)
        changes.append((owner_key, item, merged))
    if not apply and changes:
        return _state(
            "planned",
            "NNTP provider settings are ready to apply; existing accounts and unrelated policy will be preserved.",
            evidence=budget,
        )
    allow_private = bool(values.get(f"{source}::allow_private_nntp", False))
    if not _authenticate(
        client,
        service,
        supplied,
        allow_private=allow_private,
        nntp_authenticator=nntp_authenticator,
    ):
        return _state(
            "waiting_input",
            "An NNTP authentication check failed. Check credentials, TLS, endpoint reachability and remaining account connections.",
            verification="failed",
            evidence=budget,
        )
    restart_pending = False
    if changes:
        if service == "altmount":
            for owner_key, previous, merged in changes:
                body = {
                    key: value
                    for key, value in merged.items()
                    if key
                    in {
                        "name",
                        "host",
                        "port",
                        "username",
                        "password",
                        "tls",
                        "max_connections",
                        "enabled",
                    }
                }
                path = "/api/providers" + (
                    "/" + str(previous["id"]) if previous else ""
                )
                saved = _unwrap(
                    _request(client, "PUT" if previous else "POST", path, json=body)
                )
                if not saved.get("id"):
                    raise UnsupportedProviderVersion(
                        "AltMount did not return a stable provider identifier."
                    )
                ownership[owner_key] = {
                    "id": saved["id"],
                    "fingerprint": fingerprint(_managed_snapshot(saved, service)),
                    "credential_fingerprint": fingerprint(merged["password"]),
                }
                write_ownership(ledger_path, ownership)
        elif service == "infinidysk":
            replacement = {**current, "Providers": desired_list}
            result = _request(
                client,
                "POST",
                "/api/update-config",
                data={
                    "usenet.providers": json.dumps(replacement, separators=(",", ":"))
                },
            )
            if result.get("status", result.get("Status")) is not True:
                raise ApiError("InfiniDysk did not confirm its configuration update.")
            for owner_key, _previous, merged in changes:
                ownership[owner_key] = {
                    "id": merged["ProviderId"],
                    "fingerprint": fingerprint(_managed_snapshot(merged, service)),
                    "credential_fingerprint": fingerprint(
                        merged["Pass"]
                        if service == "infinidysk"
                        else merged["password"]
                    ),
                }
            write_ownership(ledger_path, ownership)
        else:
            replacement = copy.deepcopy(current)
            replacement["usenet"] = {
                **(replacement.get("usenet") or {}),
                "providers": desired_list,
            }
            # Full read/merge/write also preserves settings on older v2 endpoints
            # that replaced rather than merged partial API updates.
            update_result = _request(client, "POST", "/api/config", json=replacement)
            restart_pending = update_result.get("restarted") is True
            for owner_key, _previous, merged in changes:
                identity = _account_identity(merged, service)
                ownership[owner_key] = {
                    "id": fingerprint(identity),
                    "fingerprint": fingerprint(_managed_snapshot(merged, service)),
                    "credential_fingerprint": fingerprint(
                        merged["Pass"]
                        if service == "infinidysk"
                        else merged["password"]
                    ),
                }
            write_ownership(ledger_path, ownership)
    _saved_config, saved_providers = _read_config(client, service, cfg)
    for entry in supplied:
        target = _desired(entry, service)
        matches = [
            item
            for item in saved_providers
            if _account_identity(item, service) == _account_identity(target, service)
        ]
        if len(matches) != 1 or _managed_snapshot(
            matches[0], service
        ) != _managed_snapshot(target, service):
            if restart_pending:
                return _state(
                    "waiting_dependency",
                    "Decypharr accepted the provider settings and is restarting. Recheck after its configuration reload finishes.",
                    retryable=True,
                    evidence=budget,
                )
            return _state(
                "failed",
                "Provider settings did not match their authenticated readback. Retry after the application is ready.",
                verification="failed",
                retryable=True,
                evidence=budget,
            )
    return _state(
        "applied" if changes else "already_present",
        "NNTP accounts authenticated and their connection settings were read back. No content was downloaded.",
        verification="passed",
        evidence={
            **budget,
            "authentication": "passed",
            "configuration": "passed",
            "content_tested": False,
        },
    )


def _import_layout(action, config, payload, *, apply, client_factory):
    source = action["source"]
    cfg, values = _resource(config, source), _values(payload)
    strategy, directory = values.get(f"{source}::import_strategy"), values.get(
        f"{source}::import_dir"
    )
    if strategy not in {"NONE", "SYMLINK", "STRM"}:
        return _state(
            "waiting_input",
            "Choose the AltMount library output strategy.",
            missing_inputs=[f"{source}::import_strategy"],
        )
    if strategy != "NONE" and (
        not isinstance(directory, str)
        or not os.path.isabs(directory)
        or directory == "/"
        or ".." in directory.split("/")
        or directory.split("/")[1] in {"etc", "config", "proc", "sys", "dev"}
    ):
        return _state(
            "waiting_input",
            "Choose an absolute media library directory inside DUMB.",
            missing_inputs=[f"{source}::import_dir"],
        )
    client = _client(
        "altmount", cfg, client_factory, payload=payload, allow_login=apply
    )
    current, _providers = _read_config(client, "altmount")
    section = current.get("import")
    if not isinstance(section, dict) or "import_strategy" not in section:
        raise UnsupportedProviderVersion(
            "This AltMount version does not expose the supported import configuration API."
        )
    desired = {**section, "import_strategy": strategy}
    if strategy != "NONE":
        desired["import_dir"] = directory
    changed = desired != section
    if changed and not apply:
        return _state(
            "planned",
            "The selected output strategy and directory are ready to apply; existing files will not be moved.",
        )
    if changed:
        _request(client, "PATCH", "/api/config/import", json={"import": desired})
    saved, _providers = _read_config(client, "altmount")
    saved_section = saved.get("import") or {}
    if any(
        saved_section.get(key) != desired.get(key)
        for key in ("import_strategy", "import_dir")
    ):
        return _state(
            "failed",
            "AltMount import settings did not match their readback.",
            verification="failed",
            retryable=True,
        )
    return _state(
        "applied" if changed else "already_present",
        "AltMount output settings were read back; existing files and library sync settings were preserved.",
        verification="passed",
        evidence={"configuration": "passed", "files_moved": False},
    )


def _dispatch_result(
    action, config, payload, operation, *, apply, client_factory, nntp_authenticator
):
    del operation
    service, kind = action.get("service_key"), action.get("action")
    if (
        service not in PROVIDERS
        or kind not in {"nntp", "import_layout"}
        or (kind == "import_layout" and service != "altmount")
    ):
        return None
    try:
        if kind == "nntp":
            return _nntp(
                action,
                config,
                payload,
                apply=apply,
                client_factory=client_factory,
                nntp_authenticator=nntp_authenticator,
            )
        return _import_layout(
            action, config, payload, apply=apply, client_factory=client_factory
        )
    except (ProviderInputError, ProviderOwnershipConflict) as exc:
        return _state("waiting_input", str(exc))
    except UnsupportedProviderVersion:
        return _state(
            "unsupported",
            "The installed application does not expose a supported setup schema/API. Complete this configuration in its UI or select a supported version.",
            verification="unavailable",
        )
    except ApiError as exc:
        if exc.status_code in (401, 403):
            return _state(
                "waiting_input",
                (
                    "AltMount rejected the administrator settings session. Supply its administrator name and temporary password to authorize again; the download-client API key cannot authorize settings."
                    if service == "altmount"
                    else "The application rejected its API credentials. Finish account authorization or correct its API key, then retry."
                ),
                verification="failed",
            )
        if exc.status_code in (404, 405):
            return _state(
                "unsupported",
                "This installed application version does not expose the required setup endpoint. Configure it in the application UI.",
                verification="unavailable",
            )
        return _state(
            "waiting_dependency",
            "The application API is not ready. Retry after startup; no credentials are included in this result.",
            retryable=True,
        )
    except (OSError, ValueError, TypeError, KeyError):
        return _state(
            "waiting_dependency",
            "Application configuration or connection verification is not ready. Check the service UI and retry.",
            retryable=True,
        )


def _dispatch(
    action, config, payload, operation, *, apply, client_factory, nntp_authenticator
):
    handoff = {}
    token = _SETTINGS_HANDOFF.set(handoff)
    try:
        result = _dispatch_result(
            action,
            config,
            payload,
            operation,
            apply=apply,
            client_factory=client_factory,
            nntp_authenticator=nntp_authenticator,
        )
    finally:
        _SETTINGS_HANDOFF.reset(token)
    unverified = handoff.pop("_altmount_session_unverified", False)
    if result is not None and handoff and not unverified:
        # The coordinator retains these only in its protected input store and
        # strips both fields before public state/API serialization.
        result["_private_values"] = handoff
        result["_clear_inputs"] = ["altmount::admin_password"]
    return result


def apply_provider(
    action,
    config,
    payload,
    operation=None,
    *,
    client_factory=ApiClient,
    nntp_authenticator=authenticate_nntp,
):
    return _dispatch(
        action,
        config,
        payload,
        operation,
        apply=True,
        client_factory=client_factory,
        nntp_authenticator=nntp_authenticator,
    )


def inspect_provider(
    action,
    config,
    payload,
    operation=None,
    *,
    client_factory=ApiClient,
    nntp_authenticator=authenticate_nntp,
):
    return _dispatch(
        action,
        config,
        payload,
        operation,
        apply=False,
        client_factory=client_factory,
        nntp_authenticator=nntp_authenticator,
    )
