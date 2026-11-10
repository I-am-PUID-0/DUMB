"""Reviewed TPA domain/routes through the authenticated native 1.24 API.

Audited source: I-am-PUID-0/traefik-proxy-admin 1.24, native domain/service
DTOs, local administrator login, admin-session/CSRF proxy and security-config
routes. No generated Traefik files, DNS records or global policies are edited.
"""

from __future__ import annotations

import copy
import ipaddress
import json
from http.cookies import SimpleCookie
from pathlib import Path
import re
import time
from urllib.parse import quote

from utils.arrangement_adoption import preview
from utils.arrangement_http import ApiClient, ApiError
from utils.integration_ownership import fingerprint, read_ownership, write_ownership

SOURCE = "traefik_proxy_admin"
COOKIE = "tpa-admin-session"
SERVICE_FIELDS = (
    "name",
    "serviceGroup",
    "subdomain",
    "hostnameMode",
    "customHostnames",
    "domainId",
    "targetIp",
    "targetPort",
    "entrypoint",
    "isHttps",
    "insecureSkipVerify",
    "passHostHeader",
    "enabled",
    "enableDurationMinutes",
    "middlewares",
    "requestHeaders",
    "managedMiddlewares",
    "advancedRouters",
)


class RouteInput(ValueError):
    """A safe, actionable routing input or contract error."""


def _state(execution, reason, *, verification="not_checked", evidence=None, **extra):
    return {
        "execution": execution,
        "reason": reason,
        "verification": verification,
        "retryable": False,
        "evidence": evidence or {},
        **extra,
    }


def _source(resource):
    return resource.get("source") or resource.get("id") or SOURCE


def _values(payload):
    return (payload.get("arrangement") or {}).get("values") or {}


def _field(source, key, label, kind="string", **extra):
    return {
        "key": source + "::" + key,
        "resource_id": source,
        "service_key": SOURCE,
        "label": label,
        "type": kind,
        "required": False,
        **extra,
    }


def _targets(config):
    options = []
    for key, cfg in config.items():
        if key in {
            "dumb",
            "postgres",
            "traefik",
            "rclone",
            "zurg",
            "phalanx_db",
        } or not isinstance(cfg, dict):
            continue
        resources = cfg.get("instances")
        rows = (
            [(f"{key}.{name}", instance) for name, instance in resources.items()]
            if isinstance(resources, dict)
            else [(key, cfg)]
        )
        for rid, item in rows:
            if (
                isinstance(item, dict)
                and item.get("enabled")
                and type(item.get("port")) is int
                and 1 <= item["port"] <= 65535
            ):
                options.append({"value": rid, "label": item.get("process_name") or rid})
    return options


def schema_fields(resource, config, payload=None):
    if resource.get("service_key") != SOURCE:
        return []
    source = _source(resource)
    domains = _values(payload or {}).get(source + "::route_domains") or []
    choices = [
        {"value": row.get("domain"), "label": row.get("domain")}
        for row in domains
        if isinstance(row, dict) and row.get("domain")
    ]
    return [
        _field(source, "admin_username", "TPA administrator username"),
        _field(
            source,
            "admin_password",
            "TPA temporary administrator password",
            "password",
            secret=True,
        ),
        _field(
            source,
            "settings_session",
            "Protected TPA settings session",
            "password",
            secret=True,
            internal=True,
        ),
        _field(
            source,
            "route_domains",
            "Create or reuse route domains",
            "list",
            default=[],
            max_items=16,
            refresh_on_change=True,
            item_label="Domain",
            add_label="Add route domain",
            item_fields=[
                {
                    "key": "domain",
                    "label": "Root domain",
                    "type": "string",
                    "required": True,
                },
                {"key": "name", "label": "Domain display name", "type": "string"},
                {
                    "key": "cert_resolver",
                    "label": "Existing Traefik certificate resolver",
                    "type": "string",
                },
                {
                    "key": "wildcard_cert",
                    "label": "Request a wildcard certificate",
                    "type": "boolean",
                    "default": False,
                },
            ],
            description="Existing domains and their shared certificate/default settings are reused unchanged. New domains are never made the global default.",
        ),
        _field(
            source,
            "route_services",
            "Selected application routes",
            "list",
            default=[],
            max_items=32,
            refresh_on_change=True,
            item_label="Route",
            add_label="Add application route",
            item_fields=[
                {
                    "key": "name",
                    "label": "Route display name",
                    "type": "string",
                    "required": True,
                },
                {
                    "key": "domain",
                    "label": "Root domain",
                    "type": "select",
                    "options": choices,
                    "required": True,
                },
                {
                    "key": "hostname",
                    "label": "Full application hostname",
                    "type": "string",
                    "required": True,
                },
                {
                    "key": "target",
                    "label": "Selected managed application",
                    "type": "select",
                    "options": _targets(config),
                    "required": True,
                },
                {
                    "key": "entrypoint",
                    "label": "Existing Traefik HTTPS entrypoint",
                    "type": "string",
                    "default": "websecure",
                },
                {
                    "key": "authentication",
                    "label": "Route authentication",
                    "type": "select",
                    "options": [
                        {
                            "value": "preserve",
                            "label": "Preserve this existing route's protection",
                        },
                        {
                            "value": "application",
                            "label": "Application handles login; no extra TPA authentication",
                        },
                    ],
                    "required": True,
                },
                {
                    "key": "sso_groups",
                    "label": "Allowed SSO groups (comma separated)",
                    "type": "string",
                },
                {
                    "key": "sso_users",
                    "label": "Allowed SSO users (comma separated)",
                    "type": "string",
                },
                {
                    "key": "allow_all_sso_users",
                    "label": "Allow every authenticated user of this SSO provider",
                    "type": "boolean",
                    "default": False,
                },
                {
                    "key": "enabled",
                    "label": "Enable this hostname route",
                    "type": "boolean",
                    "default": False,
                },
            ],
        ),
        _field(
            source,
            "authorize_public_exposure",
            "Authorize the reviewed hostname routes and their public exposure",
            "boolean",
            default=False,
            description="Saving routes can publish applications through Traefik. DNS, host port mapping and certificate-resolver credentials remain separate.",
        ),
        _field(
            source,
            "adopt_existing",
            "Accept the exact reviewed routing changes",
            "boolean",
            default=False,
        ),
        _field(
            source, "adoption_preview_id", "Accepted routing preview", internal=True
        ),
    ]


def _request(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else None


def _context(config):
    cfg = config.get(SOURCE) or {}
    if not cfg.get("enabled"):
        raise RouteInput("Start the selected Traefik Proxy Admin instance first.")
    path = Path(cfg.get("config_dir") or "/traefik-proxy-admin") / "package.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise RouteInput("Wait for the managed TPA installation's version metadata.")
    version = json.loads(path.read_text()).get("version", "")
    if not isinstance(version, str) or not re.fullmatch(
        r"1\.24\.\d+(?:[-+][A-Za-z0-9.-]+)?", version
    ):
        raise RouteInput(
            "This TPA version has no recognized 1.24 domain/routing contract. Use its native UI."
        )
    port = cfg.get("port") or 3004
    if type(port) is not int or not 1 <= port <= 65535:
        raise RouteInput("Use a valid managed TPA listener port.")
    return cfg, f"http://127.0.0.1:{port}"


def _authenticated(config, payload, source, factory, *, token=None):
    _, origin = _context(config)
    token = (
        token
        if token is not None
        else _values(payload).get(source + "::settings_session")
    )
    if not isinstance(token, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,8192}\.[A-Za-z0-9_-]{43}", token
    ):
        raise RouteInput(
            "Authorize TPA routing with its temporary administrator login first."
        )
    client = factory(
        origin,
        managed=True,
        timeout=10,
        headers={"Cookie": COOKIE + "=" + token, "Origin": origin},
    )
    document = _request(client, "GET", "/api/auth/admin/me")
    session = document.get("session") if isinstance(document, dict) else None
    if (
        not isinstance(session, dict)
        or document.get("enabled") is not True
        or session.get("role") != "admin"
        or type(session.get("exp")) is not int
        or session["exp"] <= time.time()
    ):
        raise RouteInput(
            "An authenticated TPA administrator session is required; disabled auth or lesser roles do not authorize setup."
        )
    return client, origin


def _account(action, config, payload, operation, apply, factory):
    source, values = _source(action), _values(payload)
    try:
        _authenticated(config, payload, source, factory)
        return _state(
            "already_present",
            "The retained TPA administrator session is valid.",
            verification="passed",
        )
    except (RouteInput, ApiError):
        pass
    _, origin = _context(config)
    if not apply or operation != "apply":
        return _state(
            "waiting_input",
            "Complete the first administrator in TPA's native UI, then apply its temporary administrator login. Preview and recheck never sign in.",
        )
    username, password = values.get(source + "::admin_username"), values.get(
        source + "::admin_password"
    )
    if (
        not isinstance(username, str)
        or not username.strip()
        or not isinstance(password, str)
        or not 1 <= len(password) <= 1024
    ):
        raise RouteInput(
            "Enter the existing TPA administrator username and temporary password."
        )
    response = factory(
        origin, managed=True, timeout=10, headers={"Origin": origin}
    ).request(
        "POST",
        "/api/auth/admin/local/login",
        json={"username": username.strip(), "password": password},
    )
    response.raise_for_status()
    document = response.json()
    if (
        not isinstance(document, dict)
        or document.get("ok") is not True
        or (document.get("user") or {}).get("role") != "admin"
    ):
        raise RouteInput("TPA did not confirm an administrator login.")
    cookie = SimpleCookie()
    cookie.load(
        next(
            (str(v) for k, v in response.headers.items() if k.lower() == "set-cookie"),
            "",
        )
    )
    token = cookie[COOKIE].value if COOKIE in cookie else ""
    _authenticated(config, payload, source, factory, token=token)
    return _state(
        "applied",
        "TPA administrator authorization was verified and the temporary password was cleared.",
        verification="passed",
        _private_values={source + "::settings_session": token},
        _clear_inputs=[source + "::admin_password"],
    )


def _inventory(client):
    domains = _request(client, "GET", "/api/domains")
    services = _request(client, "GET", "/api/services")
    if (
        not isinstance(domains, list)
        or not isinstance(services, list)
        or len(domains) > 512
        or len(services) > 1000
    ):
        raise RouteInput("TPA returned an unsupported or oversized route inventory.")
    for row in domains:
        if not isinstance(row, dict) or not all(
            isinstance(row.get(k), str) for k in ("id", "domain", "name")
        ):
            raise RouteInput("TPA returned an unsupported domain record.")
    for row in services:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or not isinstance(row.get("domainId"), str)
            or not isinstance(row.get("hostnameMode"), str)
            or type(row.get("enabled")) is not bool
        ):
            raise RouteInput("TPA returned an unsupported service record.")
    return domains, services


def discover_fields(resource, config, payload, *, client_factory=ApiClient):
    fields = schema_fields(resource, config, payload)
    if not fields:
        return fields
    try:
        client, _ = _authenticated(config, payload, _source(resource), client_factory)
        domains, _ = _inventory(client)
        choices = {row["domain"]: row["domain"] for row in domains}
        domain_field = next(f for f in fields if f["key"].endswith("::route_services"))[
            "item_fields"
        ][1]
        choices.update({row["value"]: row["label"] for row in domain_field["options"]})
        domain_field["options"] = [
            {"value": key, "label": value} for key, value in sorted(choices.items())
        ]
        auth = next(f for f in fields if f["key"].endswith("::route_services"))[
            "item_fields"
        ][5]
        for route, kind, label in (
            ("basic-auth-configs", "basic", "Basic Auth"),
            ("sso-configs", "sso", "SSO"),
        ):
            rows = _request(client, "GET", "/api/security/" + route)
            if not isinstance(rows, list) or len(rows) > 512:
                continue
            auth["options"] += [
                {"value": kind + ":" + row["id"], "label": label + ": " + row["name"]}
                for row in rows
                if isinstance(row, dict)
                and isinstance(row.get("id"), str)
                and isinstance(row.get("name"), str)
                and _usable_profile(row, kind)
            ]
    except (RouteInput, ApiError, ValueError, OSError, TypeError, KeyError):
        pass
    return fields


def _host(value):
    if not isinstance(value, str) or len(value) > 253:
        raise RouteInput(
            "Enter a full DNS hostname without a scheme, wildcard, path or port."
        )
    value = value.strip().lower().rstrip(".")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value) or "." not in value:
        raise RouteInput(
            "Enter a full DNS hostname without a scheme, wildcard, path or port."
        )
    if any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in value.split(".")
    ):
        raise RouteInput("Use valid DNS hostname labels.")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise RouteInput("Application routes require DNS names rather than IP addresses.")


def _text(value, label, *, default=None):
    value = default if value in (None, "") else value
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 200
        or any(ord(c) < 32 for c in value)
    ):
        raise RouteInput("Enter a bounded " + label + ".")
    return value.strip()


def _boolean(row, key, default=False):
    value = row.get(key, default)
    if type(value) is not bool:
        raise RouteInput("Use a checkbox for " + key.replace("_", " ") + ".")
    return value


def _array(value, label, maximum):
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or any(not isinstance(row, dict) for row in value)
    ):
        raise RouteInput("Use a bounded list of " + label + ".")
    return value


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", value):
        raise RouteInput("TPA returned an unsupported native record identifier.")
    return quote(value, safe="")


def _dto(row):
    # The audited native GET maps these two database-null fields to undefined.
    # Its JSON response therefore omits them; other omissions remain unknown.
    omitted_null = {"middlewares", "requestHeaders"}
    if not isinstance(row, dict) or not all(
        key in row for key in SERVICE_FIELDS if key not in omitted_null
    ):
        raise RouteInput(
            "TPA returned an incomplete native routing DTO; use its native UI."
        )
    result = {key: copy.deepcopy(row.get(key)) for key in SERVICE_FIELDS}
    if (
        not isinstance(result["name"], str)
        or not isinstance(result["targetIp"], str)
        or type(result["targetPort"]) is not int
        or any(
            type(result[k]) is not bool
            for k in ("isHttps", "insecureSkipVerify", "passHostHeader", "enabled")
        )
    ):
        raise RouteInput("TPA returned an unsupported native routing DTO.")
    for key in (
        "customHostnames",
        "middlewares",
        "requestHeaders",
        "managedMiddlewares",
        "advancedRouters",
    ):
        if isinstance(result[key], str):
            try:
                result[key] = json.loads(result[key])
            except ValueError:
                raise RouteInput(
                    "The selected route uses an unsupported legacy routing field; review it in TPA."
                ) from None
    return result


def _security(client, identifier):
    rows = _request(
        client, "GET", "/api/services/" + _id(identifier) + "/security-configs"
    )
    if not isinstance(rows, list) or len(rows) > 64:
        raise RouteInput("TPA returned an unsupported security-policy inventory.")
    result = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("type") not in {"sso", "basic_auth", "bypass", "shared_link"}
            or type(row.get("isEnabled")) is not bool
            or type(row.get("priority")) is not int
            or not isinstance(row.get("config"), dict)
        ):
            raise RouteInput("TPA returned an unsupported service security policy.")
        result.append(
            {
                key: copy.deepcopy(row[key])
                for key in ("type", "isEnabled", "priority", "config")
            }
        )
    return sorted(result, key=fingerprint)


def _hostnames(row, domain):
    mode = row.get("hostnameMode")
    if mode == "apex":
        return [_host(domain)]
    if mode == "subdomain":
        return [_host(str(row.get("subdomain") or "") + "." + domain)]
    if mode == "custom":
        value = row.get("customHostnames")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                raise RouteInput(
                    "Review the existing route's hostname format in TPA."
                ) from None
        if isinstance(value, list) and value and len(value) <= 64:
            return [_host(host) for host in value]
    raise RouteInput("TPA returned an unrecognized hostname-routing contract.")


def _selected_target(config, value):
    if value not in {row["value"] for row in _targets(config)}:
        raise RouteInput("Select an enabled managed application's advertised listener.")
    key, _, instance = value.partition(".")
    cfg = config[key]
    if instance:
        cfg = cfg["instances"][instance]
    return {"targetIp": "127.0.0.1", "targetPort": cfg["port"], "isHttps": False}


def _native_identifier(row, domain):
    """Mirror audited TPA 1.24 generated service/router identity, not DNS identity."""
    if row.get("hostnameMode") == "subdomain":
        return row.get("subdomain") or "default"
    if row.get("hostnameMode") == "apex":
        return domain.replace(".", "-")
    value = row.get("customHostnames")
    if isinstance(value, str):
        value = json.loads(value)
    if (
        row.get("hostnameMode") == "custom"
        and isinstance(value, list)
        and value
        and isinstance(value[0], str)
    ):
        return value[0].replace(".", "-")
    raise RouteInput("The native generated route identifier is not recognized.")


def _namespace_overlap(left, right):
    # Advanced/bypass/auth-ticket/IP-jail routers share their base's namespace.
    return left == right or left.startswith(right + "-") or right.startswith(left + "-")


def _native_json(value, kind):
    if value in (None, ""):
        return kind()
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, kind):
        raise RouteInput("Review the unsupported native routing policy in TPA first.")
    return value


def _guard_managed_middleware(generated_id, services):
    exact = {"auth-sso-" + generated_id, "headers-" + generated_id}
    prefixes = (
        "basic-auth-" + generated_id + "-",
        "observe-bypass-" + generated_id + "-",
    )
    for service in services:
        if service.get("enabled") is not True:
            continue
        definitions = _native_json(service.get("managedMiddlewares"), dict)
        if len(definitions) > 512:
            raise RouteInput(
                "The native managed-middleware inventory is too large to review safely."
            )
        for name, definition in definitions.items():
            if (
                not isinstance(definition, dict)
                or not isinstance(name, str)
                or not name.strip()
            ):
                continue
            safe_name = (
                re.sub(r"-+", "-", re.sub(r"[^a-zA-Z0-9-]", "-", name.strip())).strip(
                    "-"
                )
                or "custom"
            )
            if safe_name in exact or safe_name.startswith(prefixes):
                raise RouteInput(
                    "An existing app-managed middleware overlaps this route's generated authentication/header namespace. Resolve the shared policy in TPA before enabling it here."
                )


def _resolver(value):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value.strip().lower() != "none"
    )


def _guard_certificate_host(host, domains):
    for domain in domains:
        if (
            domain.get("useWildcardCert") is True
            and _resolver(domain.get("certResolver"))
            and _host(domain["domain"]) == host
        ):
            raise RouteInput(
                "A native wildcard certificate trigger already serves this apex hostname. Review its shared routing policy in TPA first."
            )
        configs = _native_json(domain.get("certificateConfigs"), list)
        if len(configs) > 512:
            raise RouteInput(
                "The native certificate routing inventory is too large to review safely."
            )
        for cert in configs:
            if not isinstance(cert, dict) or not _resolver(cert.get("certResolver")):
                continue
            sans = cert.get("sans") or []
            if not isinstance(sans, list) or len(sans) > 512:
                raise RouteInput(
                    "Review the unsupported native certificate policy in TPA first."
                )
            names = [cert.get("main"), *sans]
            if any(
                isinstance(name, str) and name.strip().lower().rstrip(".") == host
                for name in names
            ):
                raise RouteInput(
                    "A native certificate-trigger route already serves this hostname. Review the shared certificate routing policy in TPA first."
                )


def _fresh_generated_guard(client, desired, identifier, host, domain):
    domains, services = _inventory(client)
    by_id = {row["id"]: row["domain"] for row in domains}
    generated_id = _native_identifier(desired, domain)
    for service in services:
        if service["id"] == identifier:
            continue
        if service["domainId"] not in by_id:
            raise RouteInput(
                "The native route inventory changed. Recheck and review it again."
            )
        other_domain = by_id[service["domainId"]]
        if host in _hostnames(service, other_domain) or _namespace_overlap(
            generated_id, _native_identifier(service, other_domain)
        ):
            raise RouteInput(
                "A native route now overlaps the reviewed generated routing namespace. Review a fresh plan in TPA."
            )
    _guard_managed_middleware(generated_id, services)
    _guard_certificate_host(host, domains)


def _usable_profile(profile, kind):
    if profile.get("enabled") is False or profile.get("isEnabled") is False:
        return False
    if kind != "basic":
        return True
    # TPA 1.24 emits no basicAuth middleware at all for an empty profile.
    # Native public user DTOs deliberately omit hashes; users have no enable flag.
    users = profile.get("users")
    return bool(
        isinstance(users, list)
        and 1 <= len(users) <= 1000
        and all(
            isinstance(user, dict)
            and isinstance(user.get("id"), str)
            and isinstance(user.get("username"), str)
            and user["username"].strip()
            and user.get("configId") == profile.get("id")
            and not any(
                key in user
                for key in ("enabled", "isEnabled", "disabled", "passwordHash")
            )
            for user in users
        )
    )


def _check_profile(client, kind, identifier):
    route = "basic-auth-configs" if kind == "basic" else "sso-configs"
    _id(identifier)
    profiles = _request(client, "GET", "/api/security/" + route)
    if (
        not isinstance(profiles, list)
        or len(profiles) > 512
        or not any(
            isinstance(profile, dict)
            and profile.get("id") == identifier
            and _usable_profile(profile, kind)
            for profile in profiles
        )
    ):
        raise RouteInput(
            "The selected authentication profile is unavailable, disabled or has no recognized native users. Review it in TPA before enabling a route."
        )


def _check_protection(client, policies):
    for policy in policies:
        if not policy["isEnabled"]:
            continue
        if policy["type"] == "basic_auth":
            _check_profile(client, "basic", policy["config"].get("basicAuthConfigId"))
        elif policy["type"] == "sso":
            _check_profile(client, "sso", policy["config"].get("ssoConfigId"))


def _desired_security(client, choice, row, existing):
    if choice == "preserve":
        if existing is None:
            raise RouteInput(
                "Choose application login or an existing authentication profile for this new route."
            )
        _check_protection(client, existing)
        return existing
    if choice == "application":
        desired = []
    else:
        kind, _, identifier = str(choice or "").partition(":")
        route, policy = {
            "basic": ("basic-auth-configs", "basic_auth"),
            "sso": ("sso-configs", "sso"),
        }.get(kind, (None, None))
        if not route:
            raise RouteInput("Choose an explicit route authentication policy.")
        _check_profile(client, kind, identifier)
        native = {"basicAuthConfigId" if kind == "basic" else "ssoConfigId": identifier}
        if kind == "sso":
            for field in ("groups", "users"):
                value = row.get("sso_" + field, "")
                if not isinstance(value, str) or len(value) > 4096:
                    raise RouteInput("Enter a bounded comma-separated SSO access list.")
                native[field] = list(
                    dict.fromkeys(
                        item.strip() for item in value.split(",") if item.strip()
                    )
                )
            if (
                not native["groups"]
                and not native["users"]
                and not _boolean(row, "allow_all_sso_users")
            ):
                raise RouteInput(
                    "Choose allowed SSO users/groups, or explicitly allow all authenticated users."
                )
        desired = [{"type": policy, "isEnabled": True, "priority": 0, "config": native}]
    if existing is not None and existing != desired:
        raise RouteInput(
            "This existing route has a different protection policy. Preserve it here and review policy changes in TPA's security UI."
        )
    return desired


def _plan(client, config, payload, source, *, owned=None):
    values = _values(payload)
    domain_rows = _array(
        values.get(source + "::route_domains", []), "route domains", 16
    )
    route_rows = _array(
        values.get(source + "::route_services", []), "application routes", 32
    )
    domains, services = _inventory(client)
    by_domain = {}
    for domain in domains:
        name = _host(domain["domain"])
        if name in by_domain:
            raise RouteInput("Resolve duplicate root-domain records in TPA first.")
        by_domain[name] = domain
    before, after, domain_specs = {}, {}, {}
    for row in domain_rows:
        name = _host(row.get("domain"))
        if name in domain_specs:
            raise RouteInput("Select each root domain once.")
        domain_specs[name] = {
            "name": _text(row.get("name"), "domain display name", default=name),
            "domain": name,
            "description": None,
            "useWildcardCert": _boolean(row, "wildcard_cert"),
            "certResolver": row.get("cert_resolver") or None,
            "certificateConfigs": None,
            "isDefault": False,
        }
        resolver = domain_specs[name]["certResolver"]
        if resolver is not None and (
            not isinstance(resolver, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", resolver)
        ):
            raise RouteInput(
                "Use the exact existing Traefik certificate-resolver name."
            )
        if name not in by_domain:
            before["domains." + name] = None
            after["domains." + name] = domain_specs[name]
    domains_by_id = {row["id"]: row["domain"] for row in domains}
    by_host, by_identifier = {}, {}
    for service in services:
        if service["domainId"] not in domains_by_id:
            raise RouteInput("TPA returned a route whose root domain is missing.")
        for host in _hostnames(service, domains_by_id[service["domainId"]]):
            by_host.setdefault(host, []).append(service)
        identifier = _native_identifier(service, domains_by_id[service["domainId"]])
        by_identifier.setdefault(identifier, set()).add(service["id"])
    selected, seen = [], set()
    for row in route_rows:
        host, domain = _host(row.get("hostname")), _host(row.get("domain"))
        if host in seen:
            raise RouteInput("Select each application hostname once.")
        seen.add(host)
        if host != domain and not host.endswith("." + domain):
            raise RouteInput(
                "The full application hostname must belong to its selected root domain."
            )
        if domain not in by_domain and domain not in domain_specs:
            raise RouteInput(
                "Select an existing TPA domain or add this new root domain first."
            )
        matches = by_host.get(host, [])
        if len(matches) > 1:
            raise RouteInput("Resolve competing routes for this hostname in TPA first.")
        existing = matches[0] if matches else None
        identifier = existing["id"] if existing else None
        native = (
            _dto(_request(client, "GET", "/api/services/" + _id(identifier)))
            if existing
            else None
        )
        security = _security(client, identifier) if existing else None
        if native and (
            domain not in by_domain or native["domainId"] != by_domain[domain]["id"]
        ):
            raise RouteInput(
                "This hostname already belongs to another native root-domain record. Review that shared routing change in TPA first."
            )
        if native and (
            len(_hostnames(native, domains_by_id[native["domainId"]])) != 1
            or native.get("advancedRouters")
        ):
            raise RouteInput(
                "This route has additional hostnames or advanced routers. Review its shared routing effects in TPA's native UI."
            )
        desired = (
            copy.deepcopy(native)
            if native
            else {
                "serviceGroup": None,
                "subdomain": None,
                "hostnameMode": "custom",
                "customHostnames": [host],
                "domainId": None,
                "insecureSkipVerify": False,
                "passHostHeader": True,
                "enableDurationMinutes": None,
                "middlewares": None,
                "requestHeaders": None,
                "managedMiddlewares": None,
                "advancedRouters": None,
            }
        )
        desired.update(
            name=_text(row.get("name"), "route display name"),
            entrypoint=_text(
                row.get("entrypoint"), "Traefik entrypoint", default="websecure"
            ),
            enabled=_boolean(row, "enabled"),
            **_selected_target(config, row.get("target")),
        )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", desired["entrypoint"]):
            raise RouteInput("Use the exact existing Traefik entrypoint name.")
        desired["domainId"] = by_domain[domain]["id"] if domain in by_domain else None
        generated_id = _native_identifier(desired, domain)
        if any(
            owner != identifier
            for other_id, owners in by_identifier.items()
            if _namespace_overlap(generated_id, other_id)
            for owner in owners
        ):
            raise RouteInput(
                "This hostname collides with another TPA generated router/service name. Choose another hostname or resolve the native routing collision first."
            )
        by_identifier.setdefault(generated_id, set()).add(
            identifier or "selected:" + host
        )
        _guard_managed_middleware(generated_id, services)
        _guard_certificate_host(host, list({**domain_specs, **by_domain}.values()))
        record = (owned or {}).get(host) or {}
        recover_disabled = bool(
            native
            and native["enabled"] is False
            and security == []
            and record.get("id") == identifier
            and record.get("phase") == "created_disabled"
            and record.get("snapshot") == fingerprint(native)
        )
        policies = _desired_security(
            client,
            row.get("authentication"),
            row,
            None if recover_disabled else security,
        )
        # Full native DTO and policy snapshots bind consent even to preserved fields.
        old = {**(native or {}), "security": security, "rootDomain": domain}
        new = {**desired, "security": policies, "rootDomain": domain}
        for field in sorted(set(old) | set(new)):
            before["routes." + host + "." + field] = old.get(field)
            after["routes." + host + "." + field] = new.get(field)
        selected.append(
            {
                "hostname": host,
                "domain": domain,
                "id": identifier,
                "before": native,
                "security_before": security,
                "desired": desired,
                "security": policies,
                "recover_disabled": recover_disabled,
            }
        )
    return before, after, domain_specs, by_domain, selected


def _routing(action, config, payload, operation, apply, factory, directory):
    source, values = _source(action), _values(payload)
    client, origin = _authenticated(config, payload, source, factory)
    root = Path(directory)
    identity = fingerprint(
        {
            "source": source,
            "origin": origin,
            "config_dir": (config.get(SOURCE) or {}).get("config_dir"),
            "kind": "proxy_routes",
        }
    )
    ledger_path = root / "tpa-routes.json"
    ledger = read_ownership(ledger_path)
    owned = {
        key.removeprefix(identity + ":"): value
        for key, value in ledger.items()
        if key.startswith(identity + ":")
    }
    before, after, domain_specs, domains, selected = _plan(
        client, config, payload, source, owned=owned
    )
    if not selected and not domain_specs:
        return _state(
            "optional_declined",
            "No TPA domains or application routes were selected.",
            verification="not_applicable",
        )
    evidence = preview(
        root / "tpa-previews.json",
        identity,
        before,
        after,
        operation="preview" if operation == "preview" else "apply",
        accepted_id=values.get(source + "::adoption_preview_id"),
        consent=values.get(source + "::adopt_existing") is True,
    )
    evidence.update(
        version_contract="TPA 1.24",
        configuration_readback=True,
        public_route_verified=False,
    )
    if before == after:
        return _state(
            "already_present",
            "The selected native TPA domain/routes and protection policies match. Check DNS/TLS/public reachability separately.",
            verification="passed",
            evidence=evidence,
        )
    if not apply or operation != "apply":
        evidence["preview_status"] = "ready" if operation == "preview" else "required"
        return _state(
            "waiting_input",
            "Review each routing field and explicitly accept this preview before saving routes. DNS and certificate issuance are separate checks.",
            evidence=evidence,
        )
    if (
        values.get(source + "::authorize_public_exposure") is not True
        or not evidence["approved"]
    ):
        return _state(
            "waiting_input",
            "Authorize public exposure and accept a fresh, unchanged field-by-field routing preview before applying.",
            evidence=evidence,
        )
    # Refuse stale consent immediately before the first mutation.
    fresh = _plan(client, config, payload, source, owned=owned)
    if fresh[0:2] != (before, after):
        raise RouteInput(
            "TPA routing changed after review. Generate and accept a fresh preview."
        )
    for name, desired_domain in domain_specs.items():
        if name in domains:
            continue
        created = _request(client, "POST", "/api/domains", json=desired_domain)
        if (
            not isinstance(created, dict)
            or created.get("domain") != name
            or created.get("isDefault") is not False
        ):
            raise RouteInput(
                "The new TPA domain could not be verified. Recheck the native inventory before retrying."
            )
        _id(created.get("id"))
        domains[name] = created
    for item in selected:
        desired = copy.deepcopy(item["desired"])
        desired["domainId"] = domains[item["domain"]]["id"]
        identifier = item["id"]
        if identifier:
            current = _dto(_request(client, "GET", "/api/services/" + _id(identifier)))
            if (
                current != item["before"]
                or _security(client, identifier) != item["security_before"]
            ):
                raise RouteInput(
                    "A selected TPA route changed before saving. Review a fresh preview."
                )
        else:
            # Never publish an unprotected intermediate route.
            staged = {**desired, "enabled": False}
            result = _request(client, "POST", "/api/services", json=staged)
            identifier = result.get("id") if isinstance(result, dict) else None
            _id(identifier)
            ledger[identity + ":" + item["hostname"]] = {
                "id": identifier,
                "hostname": item["hostname"],
                "phase": "created_disabled",
                "snapshot": fingerprint(staged),
            }
            write_ownership(ledger_path, ledger)
            if _dto(
                _request(client, "GET", "/api/services/" + _id(identifier))
            ) != staged or _security(client, identifier):
                raise RouteInput(
                    "The new disabled route has unexpected settings or protection. Review it in TPA before enabling."
                )
        if not item["id"] or item["recover_disabled"]:
            for policy in item["security"]:
                body = {
                    "securityType": policy["type"],
                    "isEnabled": policy["isEnabled"],
                    "priority": policy["priority"],
                    "config": policy["config"],
                }
                _request(
                    client,
                    "POST",
                    "/api/services/" + _id(identifier) + "/security-configs",
                    json=body,
                )
        if _security(client, identifier) != item["security"]:
            raise RouteInput(
                "The selected route's native authentication policy did not match; it was not enabled."
            )
        _check_protection(client, item["security"])
        _fresh_generated_guard(
            client, desired, identifier, item["hostname"], item["domain"]
        )
        current = _dto(_request(client, "GET", "/api/services/" + _id(identifier)))
        if current != desired:
            expected = item["before"] if item["id"] else {**desired, "enabled": False}
            if current != expected:
                raise RouteInput(
                    "The staged native route changed before enabling. Review it in TPA before accepting a fresh preview."
                )
            # Existing complete DTOs preserve user headers, middleware and timing.
            _request(client, "PUT", "/api/services/" + _id(identifier), json=desired)
        if (
            _dto(_request(client, "GET", "/api/services/" + _id(identifier))) != desired
            or _security(client, identifier) != item["security"]
        ):
            raise RouteInput(
                "Native route readback changed after save. Inspect the route in TPA; no other routes were changed."
            )
        _check_protection(client, item["security"])
        ledger[identity + ":" + item["hostname"]] = {
            "id": identifier,
            "hostname": item["hostname"],
            "phase": "verified",
            "snapshot": fingerprint({"route": desired, "security": item["security"]}),
        }
        write_ownership(ledger_path, ledger)
    # No generated-config GET: native endpoint expires unrelated timed routes.
    return _state(
        "applied",
        "The selected TPA routes and authentication were saved and read back. Check DNS, TLS and public HTTP separately.",
        verification="passed",
        evidence={
            "version_contract": "TPA 1.24",
            "configuration_readback": True,
            "public_route_verified": False,
            "routes": len(selected),
        },
    )


def _dispatch(action, config, payload, operation, apply, factory, directory):
    try:
        kind = action.get("action")
        if kind == "account":
            return _account(action, config, payload, operation, apply, factory)
        if kind == "proxy_routes":
            return _routing(
                action, config, payload, operation, apply, factory, directory
            )
        return _state(
            "waiting_input",
            "This TPA setup operation is not supported.",
            verification="unavailable",
        )
    except RouteInput as exc:
        return _state("waiting_input", str(exc), verification="unavailable")
    except (ApiError, OSError, ValueError, TypeError, KeyError):
        return _state(
            "waiting_input",
            "TPA did not return the recognized authenticated routing contract. Review its native UI and recheck; no blind retry is scheduled.",
            verification="unavailable",
        )


def inspect_proxy_routes(
    action,
    config,
    payload,
    operation="inspect",
    *,
    client_factory=ApiClient,
    ownership_directory="/config/.dumb-setup/connections",
):
    return _dispatch(
        action, config, payload, operation, False, client_factory, ownership_directory
    )


def apply_proxy_routes(
    action,
    config,
    payload,
    operation="apply",
    *,
    client_factory=ApiClient,
    ownership_directory="/config/.dumb-setup/connections",
):
    return _dispatch(
        action, config, payload, operation, True, client_factory, ownership_directory
    )
