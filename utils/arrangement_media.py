"""Fresh-account and non-destructive media-library setup adapters.

Official contracts inspected 2026-10-14: Jellyfin StartupController,
ApiKeyController and LibraryStructureController; Emby LibraryStructureService
REST reference; Plex PMS API /library/sections/all; Audiobookshelf v2.37.1
Server.js, Auth.js, ApiKeyController and LibraryController; Tautulli
webserve.get_server_identity and configUpdate (the latter is deliberately
avoided because it resets omitted checkboxes).

Internal _private_values/_clear_inputs are consumed by the coordinator before
public persistence. No administrator password is written by these adapters.
"""

from __future__ import annotations

import configparser
import io
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException

from utils.arrangement_http import ApiClient, ApiError
from utils.private_files import atomic_write_private_text
from utils.integration_ownership import fingerprint, read_ownership, write_ownership

MEDIA = frozenset({"plex", "jellyfin", "emby", "audiobookshelf"})
AUTHORIZATION = 'MediaBrowser Client="DUMB Setup", Device="DUMB", DeviceId="dumb-setup", Version="1"'
JELLYFIN_TYPES = {
    "movie": "movies",
    "show": "tvshows",
    "music": "music",
    "book": "books",
    "mixed": None,
}
PLEX_TYPES = {
    "movie": (1, "movie", "Plex Movie", "tv.plex.agents.movie"),
    "show": (2, "show", "Plex TV Series", "tv.plex.agents.series"),
    "music": (8, "artist", "Plex Music", "tv.plex.agents.music"),
}


class MediaInputError(ValueError):
    pass


class UnsupportedMediaVersion(ValueError):
    pass


def _dto_value(document, *names):
    """Accept explicit vendor casing aliases, rejecting ambiguous responses."""
    if not isinstance(document, dict):
        raise UnsupportedMediaVersion("Unsupported application response shape.")
    present = [document[name] for name in names if name in document]
    if not present:
        return None
    first = present[0]
    if any(type(value) is not type(first) or value != first for value in present[1:]):
        raise UnsupportedMediaVersion("Conflicting application response aliases.")
    return first


def _state(
    execution,
    reason,
    *,
    verification="not_checked",
    retryable=False,
    missing_inputs=(),
    evidence=None,
    **internal,
):
    return {
        "execution": execution,
        "verification": verification,
        "reason": reason,
        "retryable": retryable,
        "missing_inputs": list(missing_inputs),
        "evidence": evidence or {},
        **internal,
    }


def _values(payload):
    return (payload.get("arrangement") or {}).get("values") or {}


def _resource(config, rid):
    key, _, instance = rid.partition(".")
    block = config.get(key) or {}
    return (block.get("instances") or {}).get(instance, {}) if instance else block


def schema_fields(resource, config, payload=None):
    del config, payload
    service, rid = resource["service_key"], resource["id"]
    if service == "tautulli":
        return [
            {
                "key": f"{rid}::api_key",
                "resource_id": rid,
                "service_key": service,
                "label": "Tautulli API key",
                "type": "password",
                "required": False,
                "description": "Optional generated application key; otherwise DUMB reuses General.api_key after Tautulli API access has been enabled.",
            },
            {
                "key": f"{rid}::restart_after_config",
                "resource_id": rid,
                "service_key": service,
                "label": "Restart Tautulli to apply its Plex connection",
                "type": "boolean",
                "required": False,
                "description": "Explicitly permit a controlled restart of Tautulli only. DUMB saves/restores its configuration if applying the connection fails.",
            },
        ]
    if service not in {"jellyfin", "audiobookshelf"}:
        return []

    def field(name, label, description, kind="password"):
        return {
            "key": f"{rid}::{name}",
            "resource_id": rid,
            "service_key": service,
            "label": label,
            "type": kind,
            "description": description,
            "required": False,
        }

    return [
        field(
            "admin_username",
            "Initial administrator name",
            "Used only for an explicitly fresh server. Established accounts are never renamed or reinitialized.",
            "string",
        ),
        field(
            "admin_password",
            "Initial administrator password",
            "Used for first setup and exchanged for a dedicated application key. Removed from retained inputs after successful exchange.",
        ),
    ]


def _read_private_json(path):
    path = Path(path)
    if path.is_symlink():
        raise MediaInputError("Configuration credentials must be in a regular file.")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise MediaInputError("The generated configuration is not ready.")
    return value


def _request(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    if not response.content or response.status_code == 204:
        return None
    try:
        value = response.json()
    except ApiError:
        # Plex and startup endpoints may return XML/plain success bodies.
        if response.text.lstrip().startswith("<"):
            return ET.fromstring(response.text)
        if method != "GET":
            return None
        raise UnsupportedMediaVersion(
            "Unexpected application response format."
        ) from None
    return value


def _managed_plex(config, values=None, rid="plex", *, prefer_managed=False):
    values = values or {}
    dumb, cfg = config.get("dumb") or {}, _resource(config, rid)
    explicit = str(dumb.get("plex_address") or "").strip()
    token = (
        values.get(f"{rid}::api_key")
        or (dumb.get("plex_token") if not (prefer_managed and explicit) else "")
        or ""
    )
    if explicit and not prefer_managed:
        # Never leak the managed server's token to a different explicit server.
        return explicit, str(token), False
    if not token:
        path = cfg.get("config_file") or "/plex/Plex Media Server/Preferences.xml"
        if os.path.isfile(path) and not os.path.islink(path):
            token = ET.parse(path).getroot().attrib.get("PlexOnlineToken") or ""
    return f"http://127.0.0.1:{int(cfg.get('port') or 32400)}", str(token), True


def _client(service, source, config, payload, factory, *, token=None):
    cfg, values = _resource(config, source), _values(payload)
    if service == "plex":
        url, discovered, managed = _managed_plex(
            config, values, source, prefer_managed=True
        )
        return factory(
            url,
            headers={"X-Plex-Token": token if token is not None else discovered},
            managed=managed,
            allow_private=not managed,
            timeout=10,
        )
    ports = {"jellyfin": 8096, "emby": 8096, "audiobookshelf": 13378}
    base = str(cfg.get("base_url") or cfg.get("url_base") or "").rstrip("/")
    if service == "audiobookshelf":
        base = "/audiobookshelf"
    if base and (
        not base.startswith("/")
        or base.startswith("//")
        or any(ch in base for ch in "?#\\\r\n")
    ):
        raise MediaInputError("The media server URL base must be a path.")
    api_key = (
        token
        if token is not None
        else values.get(f"{source}::api_key") or cfg.get("api_key") or ""
    )
    if not api_key:
        for item in ((config.get("dumb") or {}).get("media_protection") or {}).get(
            "services", []
        ):
            if item.get("process_name") == cfg.get("process_name"):
                api_key = item.get("api_key") or ""
                break
    if any(
        ord(character) < 32 or ord(character) == 127 or character in {chr(34), chr(92)}
        for character in str(api_key)
    ):
        raise MediaInputError("Use an application API key without header delimiters.")
    headers = (
        {"Authorization": f"Bearer {api_key}"}
        if service == "audiobookshelf"
        else {
            "Authorization": AUTHORIZATION
            + (f', Token="{api_key}"' if api_key else ""),
            "X-Emby-Token": api_key,
        }
    )
    return factory(
        f"http://127.0.0.1:{int(cfg.get('port') or ports[service])}{base}",
        headers=headers,
        managed=True,
        timeout=10,
    )


def _account(action, config, payload, *, apply, factory):
    service, source = action["service_key"], action["source"]
    values = _values(payload)
    client = _client(service, source, config, payload, factory)
    if service == "plex":
        identity = _request(client, "GET", "/identity")
        claimed = (
            identity.attrib.get("claimed")
            if isinstance(identity, Element)
            else (identity or {}).get("MediaContainer", identity or {}).get("claimed")
        )
        if str(claimed).lower() not in {"1", "true"}:
            return _state(
                "waiting_input",
                "Claim this Plex server with your Plex account, then recheck. DUMB never creates a Plex account or resets a claim.",
            )
        if not _managed_plex(config, values, source, prefer_managed=True)[1]:
            return _state(
                "waiting_input",
                "The Plex server is claimed. Provide its authorized token to verify library access.",
                missing_inputs=[f"{source}::api_key"],
            )
        _read_libraries(client, service)
        return _state(
            "already_present",
            "The claimed Plex server accepted its authenticated library request.",
            verification="passed",
        )
    status = _request(
        client,
        "GET",
        "/status" if service == "audiobookshelf" else "/System/Info/Public",
    )
    if not isinstance(status, dict):
        raise UnsupportedMediaVersion(
            "The server does not expose supported initialization status."
        )
    fresh = (
        status.get("isInit") is False
        if service == "audiobookshelf"
        else _dto_value(status, "StartupWizardCompleted", "startupWizardCompleted")
        is False
    )
    initialized = (
        status.get("isInit") is True
        if service == "audiobookshelf"
        else _dto_value(status, "StartupWizardCompleted", "startupWizardCompleted")
        is True
    )
    if not fresh and not initialized:
        raise UnsupportedMediaVersion(
            "The server did not provide an explicit initialization state."
        )
    if initialized:
        if service == "audiobookshelf":
            _read_libraries(client, service)
        else:
            details = _request(client, "GET", "/System/Info")
            if not isinstance(details, dict) or not _dto_value(details, "Id", "id"):
                raise UnsupportedMediaVersion(
                    "The authenticated server identity is unavailable."
                )
        return _state(
            "already_present",
            "The existing server is initialized and accepted authenticated access. Existing accounts were preserved.",
            verification="passed",
            _clear_inputs=(
                [f"{source}::admin_password"]
                if values.get(f"{source}::admin_password")
                else []
            ),
        )
    if service == "emby":
        return _state(
            "waiting_input",
            "Complete Emby's initial administrator wizard in its UI and provide an application API key; then recheck. Established accounts are never reset.",
        )
    username = values.get(f"{source}::admin_username")
    password = values.get(f"{source}::admin_password")
    if (
        not isinstance(username, str)
        or not username.strip()
        or not isinstance(password, str)
        or len(password) < 8
        or any(ch in username + password for ch in "\r\n\0")
    ):
        return _state(
            "waiting_input",
            "Provide the initial administrator name and a password of at least eight characters for this fresh server.",
            missing_inputs=[f"{source}::admin_username", f"{source}::admin_password"],
        )
    if not apply:
        return _state(
            "planned",
            "The server explicitly reports a fresh installation. Initial account setup requires applying this plan.",
        )
    # Recheck immediately before mutation; another operator may have finished setup.
    latest = _request(
        client,
        "GET",
        "/status" if service == "audiobookshelf" else "/System/Info/Public",
    )
    if not isinstance(latest, dict) or (
        latest.get("isInit") is not False
        if service == "audiobookshelf"
        else _dto_value(latest, "StartupWizardCompleted", "startupWizardCompleted")
        is not False
    ):
        return _state(
            "waiting_dependency",
            "Initialization state changed. Recheck before creating an initial account.",
            retryable=True,
        )
    if service == "jellyfin":
        # The first-user endpoint initializes the fresh wizard's placeholder user.
        _request(client, "GET", "/Startup/User")
        _request(
            client,
            "POST",
            "/Startup/User",
            json={"Name": username.strip(), "Password": password},
        )
        _request(client, "POST", "/Startup/Complete")
        login = _request(
            client,
            "POST",
            "/Users/AuthenticateByName",
            json={"Username": username.strip(), "Pw": password},
        )
        session = _dto_value(login or {}, "AccessToken", "accessToken")
        if not session:
            raise MediaInputError(
                "The initial administrator could not be authenticated; finish setup in the server UI."
            )
        authenticated = _client(
            service, source, config, payload, factory, token=session
        )
        created = _request(
            authenticated,
            "POST",
            "/Auth/Keys",
            params={"app": "DUMB Setup and Protection"},
        )
        key = (
            _dto_value(created, "AccessToken", "accessToken")
            if isinstance(created, dict)
            else None
        )
        if not key:
            listing = _request(authenticated, "GET", "/Auth/Keys")
            candidates = [
                item
                for item in (_dto_value(listing or {}, "Items", "items") or [])
                if _dto_value(item, "AppName", "appName") == "DUMB Setup and Protection"
            ]
            if len(candidates) == 1:
                key = _dto_value(candidates[0], "AccessToken", "accessToken")
    else:
        _request(
            client,
            "POST",
            "/init",
            json={"newRoot": {"username": username.strip(), "password": password}},
        )
        login = _request(
            client,
            "POST",
            "/login",
            json={"username": username.strip(), "password": password},
        )
        user = (login or {}).get("user") or {}
        session = user.get("accessToken") or user.get("token")
        if not session or not user.get("id"):
            raise MediaInputError(
                "The initial root account could not be authenticated; finish setup in the server UI."
            )
        authenticated = _client(
            service, source, config, payload, factory, token=session
        )
        created = _request(
            authenticated,
            "POST",
            "/api/api-keys",
            json={"name": "DUMB Setup", "userId": user["id"], "isActive": True},
        )
        key = ((created or {}).get("apiKey") or {}).get("apiKey")
    if not key:
        return _state(
            "waiting_input",
            "The account was initialized, but this version requires creating a dedicated application API key in its UI.",
            missing_inputs=[f"{source}::api_key"],
        )
    dedicated = _client(service, source, config, payload, factory, token=key)
    _request(
        dedicated,
        "GET",
        "/api/libraries" if service == "audiobookshelf" else "/System/Info",
    )
    return _state(
        "applied",
        "The fresh administrator account and dedicated application key were verified. The temporary administrator password can now be removed.",
        verification="passed",
        _private_values={f"{source}::api_key": key},
        _clear_inputs=[f"{source}::admin_password"],
    )


def _library_specs(payload, source, service):
    supplied = _values(payload).get(f"{source}::libraries")
    if not isinstance(supplied, list) or not supplied:
        return []
    if len(supplied) > 50:
        raise MediaInputError("Choose at most 50 libraries.")
    output, names = [], set()
    for item in supplied:
        if (
            isinstance(item, dict)
            and item.get("scan_on_create") is True
            and service not in {"jellyfin", "emby"}
        ):
            raise MediaInputError(
                "This server does not support initial scanning through its library creation contract."
            )
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not item["name"].strip()
            or len(item["name"]) > 100
        ):
            raise MediaInputError("Each library needs a short name.")
        name = item["name"].strip()
        if name.lower() in names:
            raise MediaInputError("Library names must be unique in one plan.")
        names.add(name.lower())
        kind = item.get("media_type")
        if (
            (service == "plex" and kind not in PLEX_TYPES)
            or (service == "audiobookshelf" and kind not in {"book", "podcast"})
            or (service in {"jellyfin", "emby"} and kind not in JELLYFIN_TYPES)
        ):
            raise MediaInputError(
                "This media type is not supported by this server's automatic library setup. Choose a supported type or configure it in the application UI."
            )
        paths = item.get("paths")
        if (
            not isinstance(paths, list)
            or not paths
            or len(paths) > 20
            or any(
                not isinstance(path, str)
                or not os.path.isabs(path)
                or path == "/"
                or ".." in path.split("/")
                or path.split("/")[1] in {"etc", "config", "proc", "sys", "dev"}
                for path in paths
            )
        ):
            raise MediaInputError(
                "Libraries require absolute media paths inside DUMB, outside system/configuration directories."
            )
        language = item.get("language")
        if language and service == "audiobookshelf":
            raise MediaInputError(
                "Audiobookshelf library metadata language is not an equivalent field. Choose it in Audiobookshelf after creating the library."
            )
        if language and (
            not isinstance(language, str)
            or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z]{2})?", language)
        ):
            raise MediaInputError("Use a metadata language code such as en.")
        output.append(
            {
                "name": name,
                "media_type": kind,
                "paths": list(dict.fromkeys(os.path.normpath(path) for path in paths)),
                "language": language,
                "create": item.get("create_if_missing") is True,
                "scan_on_create": item.get("scan_on_create") is True,
            }
        )
    return output


def _plex_library_request(client, method, params=None):
    try:
        return _request(
            client,
            method,
            "/library/sections/all",
            **({"params": params} if params else {}),
        )
    except ApiError as exc:
        if exc.status_code not in (404, 405):
            raise
        legacy = dict(params or {})
        if method == "POST":
            legacy["type"] = {1: "movie", 2: "show", 8: "artist"}.get(
                legacy.get("type"), legacy.get("type")
            )
            legacy["location"] = legacy.pop("locations", [])
        return _request(
            client,
            method,
            "/library/sections",
            **({"params": legacy} if legacy else {}),
        )


def _read_libraries(client, service):
    path = (
        "/library/sections/all"
        if service == "plex"
        else (
            "/api/libraries"
            if service == "audiobookshelf"
            else (
                "/Library/VirtualFolders/Query"
                if service == "emby"
                else "/Library/VirtualFolders"
            )
        )
    )
    value = (
        _plex_library_request(client, "GET")
        if service == "plex"
        else _request(client, "GET", path)
    )
    if service == "plex":
        if isinstance(value, Element):
            return [
                {
                    "name": row.attrib.get("title"),
                    "type": row.attrib.get("type"),
                    "paths": [
                        node.attrib.get("path") for node in row.findall("Location")
                    ],
                }
                for row in value.findall("Directory")
            ]
        if not isinstance(value, dict) or not isinstance(
            value.get("MediaContainer"), dict
        ):
            raise UnsupportedMediaVersion("Unsupported Plex library response.")
        rows = value["MediaContainer"].get("Directory", [])
        if not isinstance(rows, list):
            raise UnsupportedMediaVersion("Unsupported Plex library response.")
        return [
            {
                "name": row.get("title"),
                "type": row.get("type"),
                "paths": [node.get("path") for node in row.get("Location") or []],
            }
            for row in rows
        ]
    if service == "audiobookshelf":
        if not isinstance(value, dict) or not isinstance(value.get("libraries"), list):
            raise UnsupportedMediaVersion(
                "Unsupported Audiobookshelf library response."
            )
        return [
            {
                "name": row.get("name"),
                "type": row.get("mediaType"),
                "paths": [
                    node.get("fullPath") or node.get("path")
                    for node in row.get("folders") or []
                ],
            }
            for row in value["libraries"]
        ]
    rows = value.get("Items") if isinstance(value, dict) else value
    if not isinstance(rows, list):
        raise UnsupportedMediaVersion("Unsupported virtual library response.")
    return [
        {
            "name": (
                _dto_value(row, "Name", "name")
                if service == "jellyfin"
                else row.get("Name")
            ),
            "type": (
                _dto_value(row, "CollectionType", "collectionType")
                if service == "jellyfin"
                else row.get("CollectionType")
            )
            or None,
            "paths": (
                _dto_value(row, "Locations", "locations")
                if service == "jellyfin"
                else row.get("Locations")
            )
            or [],
        }
        for row in rows
    ]


def _expected_type(spec, service):
    if service == "plex":
        return PLEX_TYPES[spec["media_type"]][1]
    return (
        spec["media_type"]
        if service == "audiobookshelf"
        else JELLYFIN_TYPES[spec["media_type"]]
    )


def _match(spec, existing, service):
    named = [
        row
        for row in existing
        if str(row.get("name") or "").lower() == spec["name"].lower()
    ]
    if len(named) > 1:
        raise MediaInputError(
            "Multiple existing libraries match the selected name. Resolve that ambiguity in the server UI."
        )
    if not named:
        for row in existing:
            if set(row.get("paths") or []) == set(spec["paths"]):
                raise MediaInputError(
                    "A differently named library already uses these paths. Select its existing name to reuse it."
                )
        return None
    current = named[0]
    if current["type"] != _expected_type(spec, service) or not set(
        spec["paths"]
    ).issubset(set(current.get("paths") or [])):
        raise MediaInputError(
            "An existing library has a different type or source paths. It was preserved; choose another name or update it explicitly in the server UI."
        )
    return current


def _libraries(action, config, payload, *, apply, factory):
    service, source = action["service_key"], action["source"]
    specs = _library_specs(payload, source, service)
    if not specs:
        return _state(
            "waiting_input",
            "Choose libraries to reuse or explicitly create, including their paths as seen inside DUMB.",
            missing_inputs=[f"{source}::libraries"],
        )
    client = _client(service, source, config, payload, factory)
    existing = _read_libraries(client, service)
    missing = [spec for spec in specs if _match(spec, existing, service) is None]
    if missing and any(not spec["create"] for spec in missing):
        return _state(
            "waiting_input",
            "A selected library is missing. Explicitly enable Create if missing or choose an existing library.",
        )
    if missing and not apply:
        return _state(
            "planned",
            "The selected missing libraries are ready to create. Existing libraries, paths and metadata settings will be preserved.",
        )
    for spec in missing:
        language = spec.get("language")
        if service == "plex":
            number, _kind, scanner, agent = PLEX_TYPES[spec["media_type"]]
            _plex_library_request(
                client,
                "POST",
                params={
                    "name": spec["name"],
                    "type": number,
                    "scanner": scanner,
                    "agent": agent,
                    "language": language or "en",
                    "locations": spec["paths"],
                },
            )
        elif service == "audiobookshelf":
            if language:
                raise MediaInputError(
                    "Audiobookshelf library metadata language is not an equivalent field. Choose it in Audiobookshelf after creating the library."
                )
            _request(
                client,
                "POST",
                "/api/libraries",
                json={
                    "name": spec["name"],
                    "mediaType": spec["media_type"],
                    "folders": [{"fullPath": path} for path in spec["paths"]],
                },
            )
        else:
            options = {"PathInfos": [{"Path": path} for path in spec["paths"]]}
            if language:
                options["PreferredMetadataLanguage"] = language
            kind = JELLYFIN_TYPES[spec["media_type"]]
            if service == "jellyfin":
                params = {
                    "name": spec["name"],
                    "refreshLibrary": "true" if spec["scan_on_create"] else "false",
                }
                if kind:
                    params["collectionType"] = kind
                _request(
                    client,
                    "POST",
                    "/Library/VirtualFolders",
                    params=params,
                    json={"LibraryOptions": options},
                )
            else:
                _request(
                    client,
                    "POST",
                    "/Library/VirtualFolders",
                    json={
                        "Name": spec["name"],
                        "CollectionType": kind,
                        "Paths": spec["paths"],
                        "RefreshLibrary": spec["scan_on_create"],
                        "LibraryOptions": options,
                    },
                )
        existing = _read_libraries(client, service)
        try:
            matched = _match(spec, existing, service)
        except MediaInputError:
            matched = None
        if matched is None:
            return _state(
                "failed",
                "The server did not retain the requested library configuration.",
                verification="failed",
                retryable=True,
            )
    return _state(
        "applied" if missing else "already_present",
        "Selected libraries were read back. Existing paths, metadata agents and options were preserved; playback was not tested.",
        verification="passed",
        evidence={
            "libraries": len(specs),
            "configuration": "passed",
            "playback_tested": False,
            "initial_scans_requested": sum(spec["scan_on_create"] for spec in missing),
        },
    )


def _protection(action, config, payload, *, factory):
    service, source = action["service_key"], action["source"]
    if service not in {"plex", "jellyfin", "emby"}:
        return _state(
            "unsupported",
            "This server is not supported by the current media-protection coordinator.",
            verification="unavailable",
        )
    client = _client(service, source, config, payload, factory)
    sessions = _request(
        client, "GET", "/status/sessions" if service == "plex" else "/Sessions"
    )
    if service == "plex":
        if not (
            isinstance(sessions, Element)
            or isinstance(sessions, dict)
            and isinstance(sessions.get("MediaContainer"), dict)
        ):
            raise UnsupportedMediaVersion("Unsupported Plex session response.")
    elif not isinstance(sessions, list):
        raise UnsupportedMediaVersion("Unsupported media server session response.")
    _read_libraries(client, service)
    return _state(
        "already_present",
        "The server accepted authenticated session and library checks. Enabling protection and choosing mount dependencies remain explicit setup decisions.",
        verification="passed",
        evidence={
            "session_access": "passed",
            "library_access": "passed",
            "outage_simulated": False,
        },
    )


def _ini_values(path):
    if path.is_symlink():
        raise MediaInputError(
            "The generated service configuration must be a regular file."
        )
    text = path.read_text()
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(text)
    return text, parser


def _patch_ini_section(text, section, updates):
    lines = text.splitlines(keepends=True)
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if line.strip().lower() == f"[{section.lower()}]"
        ),
        None,
    )
    if start is None:
        lines.extend(
            ["\n" if lines and not lines[-1].endswith("\n") else "", f"[{section}]\n"]
        )
        start = len(lines) - 1
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"^\s*\[", lines[i])),
        len(lines),
    )
    indexes = {
        line.split("=", 1)[0].strip().lower(): i
        for i, line in enumerate(lines[start + 1 : end], start + 1)
        if "=" in line and not line.lstrip().startswith(("#", ";"))
    }
    for key, value in updates.items():
        rendered = f"{key} = {value}\n"
        if key in indexes:
            lines[indexes[key]] = rendered
        else:
            lines.insert(end, rendered)
            end += 1
    return "".join(lines)


def _tautulli(action, config, payload, *, apply, factory):
    source, values = action["source"], _values(payload)
    selected = values.get(f"{source}::media_server")
    if selected == "external":
        external = values.get(f"{source}::external_media") or {}
        if (
            not isinstance(external, dict)
            or external.get("service_key", external.get("type")) != "plex"
            or not external.get("url")
            or not external.get("api_key")
        ):
            return _state(
                "waiting_input",
                "Provide the authorized external Plex URL and token.",
                missing_inputs=[f"{source}::external_media"],
            )
        url, token, managed = external["url"], external["api_key"], False
        private = external.get("allow_private") is True
        verify_tls = external.get("verify_tls") is not False
    elif selected == "plex":
        url, token, managed = _managed_plex(config, values, prefer_managed=True)
        private, verify_tls = not managed, True
    else:
        return _state(
            "waiting_input",
            "Select the Plex server for Tautulli.",
            missing_inputs=[f"{source}::media_server"],
        )
    if not token:
        return _state(
            "waiting_dependency",
            "Waiting for the selected Plex server's authorized token.",
            retryable=True,
        )
    plex = factory(
        url,
        headers={"X-Plex-Token": token},
        managed=managed,
        allow_private=private,
        verify_tls=verify_tls,
        timeout=10,
    )
    identity = _request(plex, "GET", "/identity")
    container = (
        identity.attrib
        if isinstance(identity, Element)
        else (identity or {}).get("MediaContainer", identity or {})
    )
    identifier = container.get("machineIdentifier")
    if not identifier:
        raise UnsupportedMediaVersion("Plex did not return a stable server identity.")
    _request(plex, "GET", "/library/sections/all")
    cfg = _resource(config, source)
    path = Path(cfg.get("config_file") or "/tautulli/data/config.ini")
    text, parser = _ini_values(path)
    pms = dict(parser["PMS"]) if parser.has_section("PMS") else {}
    current_token = pms.get("pms_token", "").strip("\"' ")
    current_identifier = pms.get("pms_identifier", "").strip("\"' ")
    if (current_token and current_token != token) or (
        current_identifier and current_identifier != identifier
    ):
        return _state(
            "waiting_input",
            "Tautulli already has a different Plex connection. It was preserved; choose the connection in Tautulli before retrying.",
        )
    parts = urlsplit(url)
    desired = {
        "pms_token": token,
        "pms_url": url,
        "pms_url_manual": "1",
        "pms_ip": parts.hostname,
        "pms_port": str(parts.port or (443 if parts.scheme == "https" else 80)),
        "pms_identifier": identifier,
    }
    updates = {
        key: value
        for key, value in desired.items()
        if pms.get(key, "").strip("\"' ") != value
    }
    if updates and not apply:
        return _state(
            "planned",
            "Tautulli's empty Plex connection can be filled without changing monitoring, retention or notification settings.",
        )
    if updates:
        if not values.get(f"{source}::restart_after_config") or not payload.get(
            "_service_stopped"
        ):
            return _state(
                "waiting_input",
                "Applying Tautulli's Plex connection requires a controlled Tautulli-only restart. Enable that option or configure it in Tautulli, then recheck.",
                missing_inputs=[f"{source}::restart_after_config"],
            )
        atomic_write_private_text(path, _patch_ini_section(text, "PMS", updates))
    api_key = (
        _values(payload).get(f"{source}::api_key")
        or parser.get("General", "api_key", fallback="")
    ).strip("\"' ")
    if not api_key:
        return _state(
            "waiting_input",
            "Plex settings are saved. Enable Tautulli's API and provide its key to verify the running connection.",
        )
    api = factory(
        f"http://127.0.0.1:{int(cfg.get('port') or 8181)}", managed=True, timeout=10
    )
    result = _request(
        api, "POST", "/api/v2", data={"apikey": api_key, "cmd": "get_server_identity"}
    )
    response = (result or {}).get("response") or {}
    data = response.get("data")
    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    if response.get("result") != "success" or not any(
        row.get("machine_identifier") == identifier for row in rows
    ):
        return _state(
            "waiting_dependency",
            "Tautulli has not loaded the saved Plex connection yet. Reload or restart Tautulli, then recheck.",
            retryable=True,
            evidence={"configuration_saved": True, "restart_required": "tautulli"},
        )
    return _state(
        "applied" if updates else "already_present",
        "Tautulli authenticated to the selected Plex server and returned its matching identity.",
        verification="passed",
    )


def _riven_frontend(action, config, payload, *, apply, factory):
    del payload
    backend = config.get("riven_backend") or {}
    path = Path(backend.get("config_file") or "/riven/backend/data/settings.json")
    data = _read_private_json(path)
    key = data.get("api_key")
    if not isinstance(key, str) or not key.strip():
        return _state(
            "waiting_dependency",
            "Waiting for Riven to generate its backend API key.",
            retryable=True,
        )
    url = f"http://127.0.0.1:{int(backend.get('port') or 8080)}"
    client = factory(url, headers={"X-Api-Key": key}, managed=True, timeout=10)
    _request(client, "GET", "/api/v1/settings/schema")
    cfg = _resource(config, action["source"])
    directory = cfg.get("config_dir")
    if not directory or not Path(directory).is_dir():
        return _state(
            "waiting_dependency",
            "Waiting for the Riven frontend installation directory.",
            retryable=True,
        )
    target = Path(directory) / "config" / "server.json"
    if target.parent.is_symlink():
        raise MediaInputError(
            "Riven frontend configuration must use a regular directory; the existing symlink was preserved."
        )
    directory_owner = Path(directory).stat()
    service_owner = (
        (
            int(config["puid"])
            if config.get("puid") is not None
            else directory_owner.st_uid
        ),
        (
            int(config["pgid"])
            if config.get("pgid") is not None
            else directory_owner.st_gid
        ),
    )
    if target.exists() and target.stat().st_uid != service_owner[0]:
        return _state(
            "waiting_input",
            "The existing frontend credentials belong to another user. Review ownership for the configured frontend user; the existing file was preserved.",
        )
    desired = {"backendUrl": url, "apiKey": key}
    existing = _read_private_json(target) if target.exists() else {}
    ledger_path = str(target) + ".dumb-owned.json"
    owned = read_ownership(ledger_path)
    if (
        existing
        and existing != desired
        and owned.get("fingerprint") != fingerprint(existing)
    ):
        return _state(
            "waiting_input",
            "Riven frontend already has a different backend connection. It was preserved; confirm the intended backend before retrying.",
        )
    existed = target.exists()
    changed = existing != desired or bool(existed and target.stat().st_mode & 0o077)
    if changed and not apply:
        return _state(
            "planned",
            "The generated Riven backend key is ready to copy into the frontend's private configuration.",
        )
    if changed:
        try:
            target.parent.mkdir(mode=0o755)
        except FileExistsError:
            pass
        else:
            created_owner = target.parent.stat()
            if (created_owner.st_uid, created_owner.st_gid) != service_owner:
                os.chown(target.parent, *service_owner)
        # Ownership is set on the private temporary inode before credentials
        # are written. Existing legitimate ownership is retained on refresh.
        atomic_write_private_text(
            target,
            json.dumps(desired, indent=2) + "\n",
            owner=service_owner if not existed else None,
        )
        write_ownership(ledger_path, {"fingerprint": fingerprint(desired)})
    if _read_private_json(target) != desired:
        return _state(
            "failed",
            "Riven frontend configuration did not match its readback.",
            verification="failed",
            retryable=True,
        )
    return _state(
        "applied" if changed else "already_present",
        "The authenticated Riven backend and frontend configuration copy were verified.",
        verification="passed",
        evidence={"configuration": "passed", "browser_session_tested": False},
    )


def _dispatch(action, config, payload, operation, *, apply, client_factory):
    del operation
    service, kind = action.get("service_key"), action.get("action")
    if not (
        (service in MEDIA and kind in {"account", "libraries", "protection"})
        or (service in {"tautulli", "riven_frontend"} and kind == "backend_connection")
    ):
        return None
    try:
        if kind == "account":
            return _account(
                action, config, payload, apply=apply, factory=client_factory
            )
        if kind == "libraries":
            return _libraries(
                action, config, payload, apply=apply, factory=client_factory
            )
        if kind == "protection":
            return _protection(action, config, payload, factory=client_factory)
        if service == "tautulli":
            return _tautulli(
                action, config, payload, apply=apply, factory=client_factory
            )
        return _riven_frontend(
            action, config, payload, apply=apply, factory=client_factory
        )
    except MediaInputError as exc:
        return _state("waiting_input", str(exc))
    except UnsupportedMediaVersion:
        return _state(
            "unsupported",
            "This application version does not expose a supported setup contract. Complete this task in its UI and recheck.",
            verification="unavailable",
        )
    except ApiError as exc:
        if exc.status_code in (401, 403):
            return _state(
                "waiting_input",
                "The server rejected application authorization. Provide an administrator application key or finish authorization in its UI.",
                verification="failed",
            )
        if exc.status_code in (404, 405):
            return _state(
                "unsupported",
                "The installed server version does not expose the required setup API. Complete this task in its UI.",
                verification="unavailable",
            )
        return _state(
            "waiting_dependency",
            "The media server API is not ready. Retry after it finishes starting.",
            retryable=True,
        )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        ET.ParseError,
        DefusedXmlException,
    ):
        return _state(
            "waiting_dependency",
            "Generated configuration or API readiness is incomplete. Recheck after startup.",
            retryable=True,
        )


def apply_media(action, config, payload, operation=None, *, client_factory=ApiClient):
    return _dispatch(
        action, config, payload, operation, apply=True, client_factory=client_factory
    )


def inspect_media(action, config, payload, operation=None, *, client_factory=ApiClient):
    return _dispatch(
        action, config, payload, operation, apply=False, client_factory=client_factory
    )
