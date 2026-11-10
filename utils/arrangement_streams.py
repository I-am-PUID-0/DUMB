"""Explicit AIOStreams profile presets and public-origin readiness.

Verified against official AIOStreams 2.36.0, aaf4e4ecbf035bde5ba90e02c2ee8a3ddbdfc9e4:
server routes api/{user,status,auth}, core db/schemas and presets/{prowlarr,zilean}.
Provider credentials belong to an encrypted user profile, never shared templates
or public global presets. No search, download, stream or content test is run.
"""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit
import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException

from utils.arrangement_http import ApiClient, ApiError
from utils.arrangement_media import _resource, _values
from utils.integration_ownership import fingerprint, read_ownership, write_ownership

_LOCK = threading.Lock()
DEBRID = ("realdebrid", "alldebrid", "premiumize", "debridlink", "torbox")


class StreamsInputError(ValueError):
    pass


class StreamsContractError(ValueError):
    pass


def schema_fields(resource, config, payload=None):
    del payload
    if resource.get("service_key") != "aiostreams":
        return []
    rid = resource["id"]
    choices = [
        {"value": f"prowlarr.{name}", "label": f"Prowlarr — {name}"}
        for name, entry in (
            (config.get("prowlarr") or {}).get("instances") or {}
        ).items()
        if entry.get("enabled")
    ]
    if (config.get("zilean") or {}).get("enabled"):
        choices.append({"value": "zilean", "label": "Zilean"})
    engines = [{"value": "none", "label": "Do not add a Usenet engine"}] + [
        {"value": service, "label": name}
        for service, name in (
            ("infinidysk", "InfiniDysk (NzbDAV-compatible)"),
            ("altmount", "AltMount"),
        )
        if (config.get(service) or {}).get("enabled")
    ]
    fields = []
    for key, label, kind, description, options in (
        (
            "backend_presets",
            "Add these backend presets",
            "multiselect",
            "Only these selected endpoints are added to the chosen encrypted user profile. Existing presets and preferences are preserved.",
            choices,
        ),
        (
            "streaming_engine",
            "Usenet playback engine",
            "select",
            "Choose explicitly. InfiniDysk uses AIOStreams' NzbDAV-compatible service; Decypharr has no supported AIOStreams playback adapter.",
            engines,
        ),
        (
            "stream_profile_uuid",
            "AIOStreams profile UUID",
            "string",
            "Existing profile to update. A new UUID is retained privately after an explicitly authorized creation.",
            None,
        ),
        (
            "stream_profile_password",
            "AIOStreams profile password",
            "password",
            "Password for this encrypted profile, distinct from dashboard login; retained privately for readback and retries.",
            None,
        ),
        (
            "create_stream_profile",
            "Create a new AIOStreams profile",
            "boolean",
            "Create only when no existing profile UUID is supplied. Does not install the addon in your player.",
            None,
        ),
        (
            "stream_debrid_service",
            "Debrid service for selected presets",
            "select",
            "Optional explicit service for a new profile. Zilean requires a supported debrid service; an existing profile's enabled services can be reused.",
            [{"value": "none", "label": "Reuse existing profile services"}]
            + [{"value": key, "label": key} for key in DEBRID],
        ),
        (
            "stream_debrid_api_key",
            "Selected debrid API key",
            "password",
            "Added only to the selected private profile; an existing user-owned credential is preserved.",
            None,
        ),
        (
            "engine_webdav_username",
            "Playback engine WebDAV username",
            "string",
            "The selected engine's existing WebDAV account. Setup does not create or reset that account.",
            None,
        ),
        (
            "engine_webdav_password",
            "Playback engine WebDAV password",
            "password",
            "Private WebDAV credential for the explicitly selected playback engine.",
            None,
        ),
        (
            "engine_public_url",
            "Playback engine public HTTPS URL",
            "string",
            "Required public streaming origin for remote players. The local API endpoint is used only for control requests.",
            None,
        ),
        (
            "public_origin_allow_private",
            "Allow private-network public-origin check",
            "boolean",
            "Explicitly permit checking a private/LAN HTTPS endpoint; public TLS verification stays enabled by default.",
            None,
        ),
        (
            "public_origin_verify_tls",
            "Verify public-origin TLS certificate",
            "boolean",
            "Defaults to true. Disable only for an explicitly trusted private endpoint with a self-signed certificate.",
            None,
        ),
    ):
        item = {
            "key": f"{rid}::{key}",
            "resource_id": rid,
            "service_key": "aiostreams",
            "label": label,
            "type": kind,
            "description": description,
            "required": False,
        }
        if options is not None:
            item["options"] = options
        fields.append(item)
    return fields


def _state(
    execution, reason, *, verification="not_checked", retryable=False, evidence=None
):
    return {
        "execution": execution,
        "verification": verification,
        "reason": reason,
        "retryable": retryable,
        "evidence": evidence or {},
    }


def _request(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    document = response.json()
    if (
        not isinstance(document, dict)
        or document.get("success") is not True
        or not isinstance(document.get("data"), dict)
    ):
        raise StreamsContractError(
            "AIOStreams returned an unsupported API response. Check the installed version in its dashboard."
        )
    return document["data"]


def _client(cfg, factory):
    return factory(
        f"http://127.0.0.1:{int(cfg.get('port') or 3006)}", managed=True, timeout=10
    )


def _base_path(value):
    value = str(value or "").rstrip("/")
    if value and (
        not value.startswith("/")
        or value.startswith("//")
        or any(ch in value for ch in "?#\\\r\n")
    ):
        raise StreamsInputError("Use a valid managed backend URL base path.")
    return value


def _managed_endpoint(config, identifier):
    cfg = _resource(config, identifier)
    if not cfg.get("enabled"):
        raise StreamsInputError("The selected backend is disabled or missing.")
    service = identifier.split(".", 1)[0]
    port = (
        int(cfg.get("backend_port") or 8080)
        if service == "infinidysk"
        else int(
            cfg.get("port")
            or {"altmount": 8088, "zilean": 8181, "prowlarr": 9696}[service]
        )
    )
    base = _base_path(cfg.get("url_base") or cfg.get("base_path"))
    token = cfg.get("api_key") or ""
    if service == "prowlarr":
        path = Path(
            cfg.get("config_file")
            or str(Path(cfg.get("config_dir") or "/prowlarr") / "config.xml")
        )
        if path.is_symlink():
            raise StreamsInputError(
                "Prowlarr's credentials must be a regular configuration file."
            )
        if path.is_file():
            if path.stat().st_size > 1024 * 1024:
                raise StreamsInputError(
                    "Prowlarr's configuration is unexpectedly large."
                )
            xml = ET.fromstring(path.read_text())
            token = xml.findtext("ApiKey") or token
            base = _base_path(xml.findtext("UrlBase") or base)
    elif service == "infinidysk":
        token = (cfg.get("env") or {}).get("FRONTEND_BACKEND_API_KEY") or token
    elif service == "altmount":
        token = (cfg.get("env") or {}).get("ALTMOUNT_API_KEY") or token
    return cfg, f"http://127.0.0.1:{port}{base}", token


def _public_url(value):
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or any(ord(ch) <= 32 or ch == "\\" for ch in value)
    ):
        raise StreamsInputError(
            "Provide the final public HTTPS origin without embedded credentials."
        )
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise StreamsInputError(
            "Provide the final public HTTPS origin without embedded credentials, a query or fragment."
        )
    # Embedded UI paths are browser routing context, never public addon origins.
    if parsed.path.startswith(("/ui/", "/service/")):
        raise StreamsInputError(
            "Use the dedicated public AIOStreams route, not its embedded DUMB UI address."
        )
    return value.rstrip("/")


def _status(client):
    data = _request(client, "GET", "/api/v1/status")
    settings = data.get("settings")
    if isinstance(settings, dict):
        data = {**data, "presets": settings.get("presets")}
    if (
        not isinstance(data.get("version"), str)
        or not re.match(r"^2\.", data["version"])
        or not isinstance(data.get("settings"), dict)
        or not isinstance(data.get("presets"), list)
    ):
        raise StreamsContractError(
            "This AIOStreams version does not expose the supported v2 status/preset contract."
        )
    return data


def _public_origin(action, config, payload, factory):
    cfg, values, rid = (
        config.get("aiostreams") or {},
        _values(payload),
        action["source"],
    )
    origin = _public_url(
        values.get(f"{rid}::base_url")
        or cfg.get("base_url")
        or (cfg.get("env") or {}).get("BASE_URL")
    )
    local = _status(_client(cfg, factory))
    if str(local["settings"].get("baseUrl") or "").rstrip("/") != origin:
        return _state(
            "waiting_dependency",
            "The running AIOStreams BASE_URL differs from the chosen public origin. Apply its bootstrap configuration and restart AIOStreams, then retry.",
            retryable=True,
        )
    public = _status(
        factory(
            origin,
            managed=False,
            allow_private=values.get(f"{rid}::public_origin_allow_private") is True,
            verify_tls=values.get(f"{rid}::public_origin_verify_tls") is not False,
            timeout=10,
        )
    )
    if (
        public.get("version") != local.get("version")
        or public.get("commit") != local.get("commit")
        or public["settings"].get("baseUrl", "").rstrip("/") != origin
    ):
        return _state(
            "failed",
            "The public route does not report the selected AIOStreams build and configured origin. Correct the proxy route before installing the addon.",
            verification="failed",
        )
    return _state(
        "already_present",
        "The configured HTTPS origin serves the selected AIOStreams build and public API. Player installation and playback authorization still require your explicit choice.",
        verification="passed",
        evidence={"public_route_reachable": True, "content_tested": False},
    )


def _login(client, cfg, values, rid):
    username = values.get(f"{rid}::admin_username") or cfg.get("auth_username")
    password = values.get(f"{rid}::admin_password") or cfg.get("auth_password")
    if (
        not isinstance(username, str)
        or not isinstance(password, str)
        or not username
        or not password
    ):
        raise StreamsInputError(
            "Provide the configured AIOStreams dashboard credentials to authorize a new private profile."
        )
    data = _request(
        client,
        "POST",
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    if data.get("isAdmin") is not True:
        raise StreamsInputError(
            "AIOStreams administrator authorization is required to create a setup profile."
        )


def _auth(uuid, password):
    return {
        "Authorization": "Basic "
        + base64.b64encode(f"{uuid}:{password}".encode()).decode()
    }


def _profile(client, uuid, password):
    data = _request(
        client,
        "GET",
        "/api/v1/user",
        params={"raw": "true"},
        headers=_auth(uuid, password),
    )
    document = data.get("userData")
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("presets"), list)
        or not isinstance(document.get("services", []), list)
    ):
        raise StreamsContractError(
            "The selected AIOStreams profile has an unsupported configuration schema."
        )
    if document.get("parentConfig"):
        raise StreamsInputError(
            "Choose a standalone AIOStreams profile; inherited profiles must be edited in AIOStreams to preserve their merge policies."
        )
    return document


def _desired(config, values, rid, status, old, factory):
    selected = values.get(f"{rid}::backend_presets")
    if (
        not isinstance(selected, list)
        or len(selected) > 30
        or any(not isinstance(item, str) for item in selected)
        or len(set(selected)) != len(selected)
    ):
        raise StreamsInputError("Choose the exact backend presets to add.")
    advertised = {
        item["value"]
        for field in schema_fields({"id": rid, "service_key": "aiostreams"}, config)
        if field["key"] == f"{rid}::backend_presets"
        for item in field["options"]
    }
    if set(selected) - advertised:
        raise StreamsInputError(
            "A selected backend is disabled, missing or unsupported."
        )
    metadata = {
        item.get("ID"): item for item in status["presets"] if isinstance(item, dict)
    }
    services = []
    debrid = values.get(f"{rid}::stream_debrid_service")
    if debrid not in (None, "", "none"):
        key = values.get(f"{rid}::stream_debrid_api_key")
        if debrid not in DEBRID or not isinstance(key, str) or not key:
            raise StreamsInputError(
                "Provide the API key for the explicitly selected debrid service."
            )
        services.append({"id": debrid, "enabled": True, "credentials": {"apiKey": key}})
    engine = values.get(f"{rid}::streaming_engine") or "none"
    if engine != "none":
        if engine not in {"infinidysk", "altmount"}:
            raise StreamsInputError(
                "Choose a supported managed Usenet playback engine."
            )
        _, url, key = _managed_endpoint(config, engine)
        user, password = values.get(f"{rid}::engine_webdav_username"), values.get(
            f"{rid}::engine_webdav_password"
        )
        if engine == "altmount" and not user and not password:
            from utils.arrangement_altmount_account import verified_webdav_credentials

            native_credentials = verified_webdav_credentials(
                config, client_factory=factory
            )
            if native_credentials:
                user, password = (
                    native_credentials["username"],
                    native_credentials["password"],
                )
        public = _public_url(values.get(f"{rid}::engine_public_url"))
        if (
            not key
            or not isinstance(user, str)
            or not user
            or not isinstance(password, str)
            or not password
        ):
            raise StreamsInputError(
                "Wait for the selected engine API key and provide its WebDAV account and public HTTPS streaming URL."
            )
        # SAB version is a credential/readiness check only: no NZBs or articles.
        probe = factory(url, managed=True, timeout=10).request(
            "GET",
            "/sabnzbd/api" if engine == "altmount" else "/api",
            params={"mode": "version", "output": "json", "apikey": key},
        )
        probe.raise_for_status()
        if not isinstance(probe.json(), dict) or not isinstance(
            probe.json().get("version"), str
        ):
            raise StreamsContractError(
                "The selected playback engine does not expose the supported SAB-compatible API."
            )
        services.append(
            {
                "id": "nzbdav" if engine == "infinidysk" else "altmount",
                "enabled": True,
                "credentials": {
                    "url": url,
                    "publicUrl": public,
                    "apiKey": key,
                    "username": user,
                    "password": password,
                },
            }
        )
    available = {
        item.get("id")
        for item in old.get("services", []) + services
        if isinstance(item, dict) and item.get("enabled") is True
    }
    presets = []
    for identifier in selected:
        family = identifier.split(".", 1)[0]
        meta = metadata.get(family)
        if not isinstance(meta, dict) or not isinstance(
            meta.get("SUPPORTED_SERVICES"), list
        ):
            raise StreamsContractError(
                "The installed AIOStreams build does not advertise the selected preset's service contract."
            )
        usable = sorted(available & set(meta["SUPPORTED_SERVICES"]))
        if not usable:
            raise StreamsInputError(
                "Enable an explicitly selected compatible debrid/playback service for each preset. Zilean requires debrid; it cannot use a Usenet engine."
            )
        _, url, key = _managed_endpoint(config, identifier)
        options = {"name": f"DUMB {identifier}", "services": usable}
        if family == "prowlarr":
            if not key:
                raise StreamsInputError("Wait for the selected Prowlarr API key.")
            result = factory(
                url, managed=True, headers={"X-Api-Key": key}, timeout=10
            ).request("GET", "/api/v1/system/status")
            result.raise_for_status()
            if not isinstance(result.json(), dict) or not isinstance(
                result.json().get("version"), str
            ):
                raise StreamsContractError(
                    "Prowlarr did not return its supported authenticated status."
                )
            options.update({"prowlarrUrl": url, "prowlarrApiKey": key})
        else:
            options["url"] = url
        presets.append(
            {
                "instanceId": "dumb-" + fingerprint(identifier)[:20],
                "type": family,
                "enabled": True,
                "options": options,
            }
        )
    return presets, services


def _merge(old, presets, services, records):
    desired = copy.deepcopy(old)
    desired.setdefault("presets", [])
    desired.setdefault("services", [])
    changed, owned = False, {}
    for field, entries, identity in (
        ("presets", presets, "instanceId"),
        ("services", services, "id"),
    ):
        for item in entries:
            key = field + ":" + item[identity]
            matches = [
                row
                for row in desired[field]
                if isinstance(row, dict) and row.get(identity) == item[identity]
            ]
            if len(matches) > 1:
                raise StreamsInputError(
                    "The selected profile contains ambiguous duplicate preset/service identifiers. Review it in AIOStreams."
                )
            if matches:
                existing = matches[0]
                # Compare only fields DUMB owns. Native migrations may add defaults.
                view = {name: existing.get(name) for name in item}
                if view == item:
                    continue
                if records.get(key) != fingerprint(view):
                    raise StreamsInputError(
                        "An existing profile preset or service has user-owned settings. Preserve it or choose a separate profile; setup did not overwrite it."
                    )
                existing.update(copy.deepcopy(item))
            else:
                desired[field].append(copy.deepcopy(item))
            changed = True
            owned[key] = fingerprint(item)
    return desired, changed, owned


def _verify(actual, presets, services):
    for field, entries, identity in (
        ("presets", presets, "instanceId"),
        ("services", services, "id"),
    ):
        for expected in entries:
            rows = [
                row
                for row in actual.get(field, [])
                if isinstance(row, dict) and row.get(identity) == expected[identity]
            ]
            if len(rows) != 1 or any(
                rows[0].get(key) != value for key, value in expected.items()
            ):
                raise StreamsContractError(
                    "AIOStreams did not retain the selected private preset/service fields. Review the profile before retrying."
                )


def _presets(action, config, payload, apply, factory):
    rid, values, cfg = (
        action["source"],
        _values(payload),
        config.get("aiostreams") or {},
    )
    password = values.get(f"{rid}::stream_profile_password")
    uuid = values.get(f"{rid}::stream_profile_uuid")
    if (
        not isinstance(password, str)
        or not 6 <= len(password) <= 256
        or any(ord(ch) < 32 for ch in password)
    ):
        raise StreamsInputError(
            "Provide the selected AIOStreams profile password (6–256 characters)."
        )
    if uuid and (
        not isinstance(uuid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", uuid)
    ):
        raise StreamsInputError("Provide a valid AIOStreams profile UUID.")
    if not uuid and values.get(f"{rid}::create_stream_profile") is not True:
        raise StreamsInputError(
            "Choose an existing profile UUID or explicitly authorize creating a new private profile."
        )
    client = _client(cfg, factory)
    status = _status(client)
    old = (
        _profile(client, uuid, password)
        if uuid
        else {
            "presets": [],
            "services": [],
            "sortCriteria": {"global": []},
            "formatter": {"id": "gdrive"},
        }
    )
    presets, services = _desired(config, values, rid, status, old, factory)
    if not presets and not services:
        return _state(
            "already_present",
            "No backend presets or playback engines are selected.",
            verification="unavailable",
        )
    ledger = (
        Path(cfg.get("config_dir") or "/aiostreams")
        / ".dumb-stream-profile-ownership.json"
    )
    with _LOCK:
        records = read_ownership(ledger)
        if not uuid and records.get("_pending_creation"):
            raise StreamsInputError(
                "A prior profile creation has an uncertain outcome. Find its UUID in AIOStreams and enter it before retrying; setup will not create a duplicate."
            )
        if uuid and apply and records.pop("_pending_creation", None):
            write_ownership(ledger, records)
        profile_records = records.get(uuid, {}) if uuid else {}
        if not isinstance(profile_records, dict):
            raise StreamsInputError(
                "The private AIOStreams ownership record needs review."
            )
        desired, changed, owned = _merge(old, presets, services, profile_records)
        if not changed:
            _verify(old, presets, services)
            return _state(
                "already_present",
                "The selected private profile already contains these backend presets and services.",
                verification="passed",
            )
        if not apply:
            return _state(
                "planned",
                "Add only the selected private profile presets/services; preserve existing addons, credentials, filters and policies.",
            )
        handoff = {}
        if not uuid:
            _login(client, cfg, values, rid)
            records["_pending_creation"] = True
            write_ownership(ledger, records)
            try:
                data = _request(
                    client,
                    "POST",
                    "/api/v1/user",
                    json={"config": desired, "password": password},
                )
            except ApiError as exc:
                if exc.status_code in {400, 401, 403, 422, 429}:
                    records.pop("_pending_creation", None)
                    write_ownership(ledger, records)
                    raise
                raise StreamsInputError(
                    "The profile creation response was lost. Check AIOStreams for the new profile UUID and enter it before retrying; setup will not create another profile automatically."
                ) from None
            uuid = data.get("uuid")
            if not isinstance(uuid, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,128}", uuid
            ):
                raise StreamsContractError(
                    "AIOStreams did not return a supported new profile identifier."
                )
            handoff[f"{rid}::stream_profile_uuid"] = uuid
        else:
            # Read again under the write lock so an intervening user edit survives.
            old = _profile(client, uuid, password)
            desired, changed, owned = _merge(old, presets, services, profile_records)
            if changed:
                _request(
                    client,
                    "PUT",
                    "/api/v1/user",
                    headers=_auth(uuid, password),
                    json={"config": desired},
                )
        try:
            actual = _profile(client, uuid, password)
            _verify(actual, presets, services)
            records.pop("_pending_creation", None)
            records[uuid] = {**profile_records, **owned}
            write_ownership(ledger, records)
        except (ApiError, StreamsContractError, StreamsInputError, OSError):
            result = _state(
                "waiting_dependency",
                "AIOStreams accepted the profile update; authenticated readback or private ownership persistence is pending. Retry the same profile rather than creating another.",
                retryable=True,
            )
            if handoff:
                result["_private_values"] = handoff
            return result
        result = _state(
            "applied",
            "Saved and authenticated the selected private backend presets/services. Review and install this profile in your player when its public route is ready.",
            verification="passed",
            evidence={
                "presets": len(presets),
                "services": len(services),
                "content_tested": False,
            },
        )
        if handoff:
            result["_private_values"] = handoff
        return result


def _dispatch(action, config, payload, *, apply, client_factory):
    if action.get("service_key") != "aiostreams" or action.get("action") not in {
        "backend_presets",
        "public_origin",
    }:
        return None
    try:
        if action["action"] == "public_origin":
            return _public_origin(action, config, payload, client_factory)
        return _presets(action, config, payload, apply, client_factory)
    except StreamsInputError as exc:
        return _state("waiting_input", str(exc))
    except StreamsContractError as exc:
        return _state("unsupported", str(exc), verification="unavailable")
    except ApiError as exc:
        if exc.status_code == 429:
            return _state(
                "waiting_dependency",
                "AIOStreams is rate-limiting setup checks. Wait for its API limit window before retrying the same profile.",
                retryable=True,
            )
        if exc.status_code in {400, 401, 403, 422}:
            return _state(
                "waiting_input",
                "The application rejected authorization or the selected profile. Check its credentials, service compatibility and upstream validation in the application UI.",
            )
        return _state(
            "waiting_dependency",
            "The selected application API or public route is not ready. Retry after its health and proxy checks pass.",
            retryable=True,
        )
    except (OSError, ValueError, TypeError, ET.ParseError, DefusedXmlException):
        return _state(
            "waiting_input",
            "Check the selected backend configuration, profile inputs and private ownership-file access.",
        )


def inspect_streams(
    action, config, payload, operation=None, *, client_factory=ApiClient
):
    del operation
    return _dispatch(
        action, config, payload, apply=False, client_factory=client_factory
    )


def apply_streams(action, config, payload, operation=None, *, client_factory=ApiClient):
    del operation
    return _dispatch(action, config, payload, apply=True, client_factory=client_factory)
