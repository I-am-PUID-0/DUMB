"""Guarded MediaStorm 1.5 settings/authentication and selected source handoff.

Official contract: godver3/mediastorm a4b60d7c10d6ef8e6f02625d9e0cc808d2a9f049,
backend/{api/routes,handlers/{auth,settings,admin_ui},config/settings}.go.
GET settings redacts credentials; PUT preserves placeholders by array index.
Never reorder/delete native entries, initialize existing accounts, or run a
download test. Engine tests always use the native basic mode.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import threading

from utils.arrangement_adoption import preview as adoption_preview
from utils.arrangement_http import ApiClient, ApiError
from utils.arrangement_media import _resource, _values
from utils.arrangement_providers import (
    _normalize_provider,
    authenticate_nntp,
    validate_connection_budgets,
)
from utils.arrangement_streams import _managed_endpoint
from utils.integration_ownership import fingerprint, read_ownership, write_ownership

_LOCK = threading.RLock()
REDACTED = "••••••••"
DEBRID = ("realdebrid", "torbox", "alldebrid", "premiumize", "torrin")
KINDS = frozenset({"account", "sources", "metadata"})
SESSION = re.compile(r"[A-Za-z0-9_-]{43}=")


class MediaStormInputError(ValueError):
    pass


class MediaStormContractError(ValueError):
    pass


def _field(rid, key, label, kind="string", **kwargs):
    return {
        "key": f"{rid}::{key}",
        "resource_id": rid,
        "service_key": "mediastorm",
        "type": kind,
        "label": label,
        **kwargs,
    }


def schema_fields(resource, config, payload=None):
    del payload
    if resource.get("service_key") != "mediastorm":
        return []
    rid = resource["id"]
    prowlarr = [
        {"value": f"prowlarr.{name}", "label": f"Prowlarr — {name}"}
        for name, cfg in ((config.get("prowlarr") or {}).get("instances") or {}).items()
        if cfg.get("enabled")
    ]
    torrents = list(prowlarr)
    if (config.get("zilean") or {}).get("enabled"):
        torrents.append({"value": "zilean", "label": "Zilean"})
    engines = [{"value": "none", "label": "No external Usenet engine"}] + [
        {"value": family, "label": family.capitalize()}
        for family in ("infinidysk", "decypharr", "altmount")
        if (config.get(family) or {}).get("enabled")
    ]
    account_sources = [{"value": "own", "label": "Enter accounts for MediaStorm"}] + [
        {"value": family, "label": f"Reuse accounts from {family.capitalize()}"}
        for family in ("infinidysk", "decypharr", "altmount")
        if (config.get(family) or {}).get("enabled")
    ]
    return [
        _field(
            rid, "admin_username", "MediaStorm administrator username", default="admin"
        ),
        _field(
            rid,
            "admin_password",
            "Current administrator password",
            "password",
            secret=True,
        ),
        _field(
            rid,
            "new_admin_password",
            "Replacement first-login password",
            "password",
            secret=True,
            description="Used only when the native application confirms its default password is still active.",
        ),
        _field(
            rid,
            "authorize_initial_account",
            "Authorize replacing the initial default administrator password",
            "boolean",
            default=False,
        ),
        _field(
            rid,
            "settings_session",
            "Protected administrator session",
            "password",
            secret=True,
            internal=True,
        ),
        _field(
            rid,
            "indexer_sources",
            "Prowlarr sources for Usenet",
            "multiselect",
            options=prowlarr,
            description="Adds the selected sources' enabled Usenet indexers with search support; preview lists exact entries.",
        ),
        _field(
            rid,
            "torrent_sources",
            "Torrent search sources",
            "multiselect",
            options=torrents,
        ),
        _field(
            rid,
            "usenet_engine",
            "Managed Usenet playback engine",
            "connection",
            options=engines,
            default="none",
        ),
        _field(
            rid,
            "engine_webdav_username",
            "Playback engine WebDAV username",
            visible_when={"key": f"{rid}::usenet_engine", "not_equals": "none"},
        ),
        _field(
            rid,
            "engine_webdav_password",
            "Playback engine WebDAV password",
            "password",
            secret=True,
            visible_when={"key": f"{rid}::usenet_engine", "not_equals": "none"},
        ),
        _field(
            rid,
            "engine_category",
            "Playback engine category",
            default="mediastorm",
            visible_when={"key": f"{rid}::usenet_engine", "not_equals": "none"},
        ),
        _field(
            rid,
            "engine_webdav_path",
            "Playback engine WebDAV path",
            description="Leave blank for the native engine path (/webdav for AltMount); an explicit relative path overrides that default.",
            visible_when={"key": f"{rid}::usenet_engine", "not_equals": "none"},
        ),
        _field(
            rid,
            "debrid_provider",
            "Debrid playback service",
            "select",
            default="none",
            options=[{"value": "none", "label": "Do not add debrid"}]
            + [{"value": key, "label": key} for key in DEBRID],
        ),
        _field(
            rid,
            "debrid_api_key",
            "Debrid playback API key",
            "password",
            secret=True,
            visible_when={"key": f"{rid}::debrid_provider", "not_equals": "none"},
        ),
        _field(
            rid,
            "nntp_source",
            "MediaStorm Usenet account source",
            "connection",
            default="own",
            options=account_sources,
        ),
        _field(
            rid,
            "nntp",
            "MediaStorm native Usenet accounts",
            "nntp_providers",
            default=[],
            description="Optional native playback accounts; shared account budgets include these connections.",
            visible_when={"key": f"{rid}::nntp_source", "equals": "own"},
        ),
        _field(
            rid,
            "nntp_connections",
            "MediaStorm connection allocation",
            "integer",
            default=5,
            minimum=1,
            maximum=200,
            visible_when={"key": f"{rid}::nntp_source", "not_equals": "own"},
        ),
        _field(
            rid,
            "allow_private_nntp",
            "Allow authorized private NNTP endpoints",
            "boolean",
            default=False,
        ),
        _field(
            rid,
            "authorize_source_tests",
            "Authorize selected provider and indexer connection tests",
            "boolean",
            default=False,
            description="Indexer tests may consume search requests. No articles, media downloads or playback are requested.",
        ),
        _field(rid, "tmdb_api_key", "TMDB API key", "password", secret=True),
        _field(rid, "tvdb_api_key", "Optional TVDB API key", "password", secret=True),
        _field(
            rid,
            "adopt_existing",
            "Authorize the exact inspected existing settings changes",
            "boolean",
            default=False,
        ),
        _field(rid, "adoption_preview_id", "Accepted settings preview"),
    ]


def _state(execution, reason, *, verification="not_checked", retryable=False, **kwargs):
    return {
        "execution": execution,
        "verification": verification,
        "reason": reason,
        "retryable": retryable,
        **kwargs,
    }


def _request(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, (dict, list)):
        raise MediaStormContractError(
            "MediaStorm returned an unsupported API response."
        )
    return value


def _client(config, rid, values, factory):
    cfg = _resource(config, rid)
    if not cfg.get("enabled"):
        raise MediaStormInputError("The selected MediaStorm service is disabled.")
    port = int(cfg.get("port") or 7777)
    if not 0 < port < 65536:
        raise MediaStormInputError("Use a valid MediaStorm service port.")
    client = factory(f"http://127.0.0.1:{port}", managed=True, timeout=15)
    version = _request(client, "GET", "/api/version")
    if not re.fullmatch(r"1\.5\.\d+", str(version.get("version") or "")):
        raise MediaStormContractError(
            "This MediaStorm version is outside the supported 1.5 settings contract."
        )
    session = values.get(f"{rid}::settings_session")
    if session:
        if not isinstance(session, str) or not SESSION.fullmatch(session):
            raise MediaStormInputError(
                "Authorize a valid protected MediaStorm administrator session."
            )
        client.headers["Authorization"] = "Bearer " + session
        client.cookies["strmr_admin_session"] = session
    return client, cfg


def _authenticated(client):
    account = _request(client, "GET", "/api/auth/me")
    if account.get("isMaster") is not True:
        raise MediaStormInputError(
            "MediaStorm settings require its primary administrator account."
        )


def _account(client, rid, values, operation):
    if values.get(f"{rid}::settings_session"):
        try:
            _authenticated(client)
            return _state(
                "already_present",
                "The retained primary administrator session authenticated.",
                verification="passed",
            )
        except ApiError as exc:
            if exc.status_code not in (401, 403):
                raise
    if operation not in {"apply", "retry"}:
        return _state(
            "waiting_input",
            "Apply the account task to authorize an administrator session. A fresh default password must be replaced deliberately.",
            missing_inputs=[f"{rid}::admin_password"],
        )
    username, password = values.get(f"{rid}::admin_username"), values.get(
        f"{rid}::admin_password"
    )
    if (
        not isinstance(username, str)
        or not username
        or not isinstance(password, str)
        or not password
    ):
        raise MediaStormInputError(
            "Provide the current primary administrator username and password."
        )
    defaults = _request(client, "GET", "/api/auth/default-password")
    if not isinstance(defaults.get("hasDefaultPassword"), bool):
        raise MediaStormContractError(
            "MediaStorm did not expose its supported first-login password guard."
        )
    body = {"username": username, "password": password, "rememberMe": False}
    replacement = values.get(f"{rid}::new_admin_password")
    if defaults["hasDefaultPassword"]:
        if values.get(f"{rid}::authorize_initial_account") is not True:
            raise MediaStormInputError(
                "Authorize the initial password replacement explicitly."
            )
        if (
            not isinstance(replacement, str)
            or not 12 <= len(replacement) <= 256
            or replacement.strip() != replacement
            or replacement == "admin"
        ):
            raise MediaStormInputError(
                "Choose a replacement first-login password with 12–256 characters and no surrounding whitespace."
            )
        body["newPassword"] = replacement
    elif replacement:
        raise MediaStormInputError(
            "The initial password was already replaced. Existing passwords are preserved; sign in with the current password."
        )
    login = _request(client, "POST", "/api/auth/login", json=body)
    token = login.get("token")
    if (
        login.get("isMaster") is not True
        or not isinstance(token, str)
        or not SESSION.fullmatch(token)
    ):
        raise MediaStormContractError(
            "MediaStorm did not issue a supported primary administrator session."
        )
    client.headers["Authorization"] = "Bearer " + token
    client.cookies["strmr_admin_session"] = token
    _authenticated(client)
    return _state(
        "applied",
        "The primary administrator authenticated and the temporary password inputs were removed.",
        verification="passed",
        _private_values={f"{rid}::settings_session": token},
        _clear_inputs=[f"{rid}::admin_password", f"{rid}::new_admin_password"],
    )


def _settings(client):
    value = _request(client, "GET", "/api/settings")
    required = {
        "server": dict,
        "metadata": dict,
        "streaming": dict,
        "usenet": list,
        "indexers": list,
        "torrentScrapers": list,
    }
    if any(not isinstance(value.get(key), kind) for key, kind in required.items()):
        raise MediaStormContractError(
            "MediaStorm settings have an unrecognized shape; existing configuration was preserved."
        )
    if not isinstance(value["streaming"].get("debridProviders", []), list):
        raise MediaStormContractError(
            "MediaStorm debrid settings have an unsupported shape."
        )
    return value


def _snapshot(value):
    if isinstance(value, dict):
        return {
            key: (
                ("[configured]" if child else "")
                if re.search(r"password|apiKey|token|secret", key, re.I)
                else _snapshot(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_snapshot(child) for child in value]
    return value


def _path(value):
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or any(c in value for c in "?#\\\r\n")
    ):
        raise MediaStormInputError(
            "Use a relative native WebDAV path beginning with one slash."
        )
    return value


def _engine(config, rid, values, factory=ApiClient):
    identifier = values.get(f"{rid}::usenet_engine") or "none"
    if identifier == "none":
        return None
    if identifier not in {"infinidysk", "altmount", "decypharr"}:
        raise MediaStormInputError("Choose an advertised managed playback engine.")
    if identifier == "decypharr":
        from utils.arrangement_external_provider import (
            _decypharr_protocol_fields,
            ProviderRegistrationError,
        )

        cfg = _resource(config, identifier)
        if not cfg.get("enabled"):
            raise MediaStormInputError("The selected playback engine is disabled.")
        try:
            base, key = _decypharr_protocol_fields(cfg)
        except ProviderRegistrationError as error:
            raise MediaStormInputError(str(error)) from None
        if base:
            base = _path(base).rstrip("/")
        url = f"http://127.0.0.1:{int(cfg.get('port') or 8282)}{base}"
    else:
        cfg, url, key = _managed_endpoint(config, identifier)
    username, password = values.get(f"{rid}::engine_webdav_username"), values.get(
        f"{rid}::engine_webdav_password"
    )
    if identifier == "altmount" and not username and not password:
        from utils.arrangement_altmount_account import verified_webdav_credentials

        credentials = verified_webdav_credentials(config, client_factory=factory)
        if credentials:
            username, password = credentials["username"], credentials["password"]
    if (
        not key
        or not isinstance(username, str)
        or not username
        or not isinstance(password, str)
        or not password
    ):
        raise MediaStormInputError(
            "Wait for the managed engine API key and provide its authorized WebDAV account."
        )
    webdav = _path(
        values.get(f"{rid}::engine_webdav_path")
        or ("/webdav" if identifier == "altmount" else "/")
    )
    category = values.get(f"{rid}::engine_category", "mediastorm")
    if not isinstance(category, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,64}", category
    ):
        raise MediaStormInputError(
            "Use a bounded engine category without path separators."
        )
    return {
        "name": f"DUMB {identifier}",
        "type": "nzbdav" if identifier == "infinidysk" else identifier,
        "enabled": True,
        "baseUrl": url,
        "apiKey": key,
        "webdavBaseUrl": url + webdav,
        "webdavUsername": username,
        "webdavPassword": password,
        "category": category,
    }


def _planned_sources(config, rid, values, factory):
    planned, tests = [], []
    for choice, protocol, section in (
        ("indexer_sources", "usenet", "indexers"),
        ("torrent_sources", "torrent", "torrentScrapers"),
    ):
        selected = values.get(f"{rid}::{choice}", []) or []
        if (
            not isinstance(selected, list)
            or len(selected) > 30
            or any(not isinstance(item, str) for item in selected)
            or len(set(selected)) != len(selected)
        ):
            raise MediaStormInputError(
                "Select a bounded list of exact source identities."
            )
        for identifier in selected:
            if identifier == "zilean" and protocol == "torrent":
                _, url, _ = _managed_endpoint(config, identifier)
                row = {
                    "name": "DUMB zilean",
                    "type": "zilean",
                    "url": url,
                    "enabled": True,
                }
                planned.append((section, "zilean", row))
                tests.append(("scraper", row))
                continue
            if not identifier.startswith("prowlarr."):
                raise MediaStormInputError(
                    "Choose an advertised Prowlarr or Zilean source."
                )
            _, url, key = _managed_endpoint(config, identifier)
            if not key:
                raise MediaStormInputError("Wait for the selected Prowlarr API key.")
            upstream = factory(url, managed=True, headers={"X-Api-Key": key})
            rows = _request(upstream, "GET", "/api/v1/indexer")
            if not isinstance(rows, list) or len(rows) > 1000:
                raise MediaStormContractError(
                    "Prowlarr's indexer discovery returned an unsupported shape."
                )
            enabled = [
                item
                for item in rows
                if isinstance(item, dict)
                and item.get("enable") is True
                and item.get("protocol") == protocol
                and item.get("supportsSearch") is True
            ]
            if not enabled:
                raise MediaStormInputError(
                    "The selected Prowlarr source has no enabled searchable indexers for the chosen protocol."
                )
            for item in enabled:
                if (
                    not isinstance(item.get("id"), int)
                    or isinstance(item["id"], bool)
                    or item["id"] < 0
                ):
                    raise MediaStormContractError(
                        "Prowlarr returned an unsupported indexer identity."
                    )
                identity = f"{identifier}.{item['id']}"
                row = {
                    "name": f"DUMB {identity}",
                    "type": "newznab" if protocol == "usenet" else "prowlarr",
                    "url": f"{url}/{item['id']}/api",
                    "apiKey": key,
                    "enabled": True,
                }
                if section == "indexers":
                    row["categories"] = ""
                planned.append((section, identity, row))
                tests.append(("indexer" if protocol == "usenet" else "scraper", row))
    engine = _engine(config, rid, values, factory)
    if engine:
        planned.append(("usenetEngines", values[f"{rid}::usenet_engine"], engine))
        tests.append(("usenet-engine", {**engine, "testMode": "basic"}))
    provider = values.get(f"{rid}::debrid_provider") or "none"
    if provider != "none":
        key = values.get(f"{rid}::debrid_api_key")
        if provider not in DEBRID or not isinstance(key, str) or not key:
            raise MediaStormInputError(
                "Provide an API key for the explicitly selected debrid service."
            )
        row = {
            "name": f"DUMB {provider}",
            "provider": provider,
            "apiKey": key,
            "enabled": True,
        }
        planned.append(("debridProviders", provider, row))
        tests.append(("debrid-provider", row))
    supplied = values.get(f"{rid}::nntp", []) or []
    if not isinstance(supplied, list) or len(supplied) > 50:
        raise MediaStormInputError("Provide a bounded list of native NNTP accounts.")
    for index, original in enumerate(supplied):
        item = _normalize_provider(original)
        identity = fingerprint((item["host"], item["port"], item["username"]))[:16]
        row = {
            "name": f"DUMB NNTP {original.get('name') or index + 1}",
            "host": item["host"],
            "port": item["port"],
            "username": item["username"],
            "password": item["password"],
            "ssl": item["tls"],
            "connections": item["connections"],
            "enabled": True,
        }
        planned.append(("usenet", identity, row))
        tests.append(("nntp", item))
    return planned, tests


def _rows(settings, section):
    if section == "debridProviders":
        value = settings["streaming"].setdefault(section, [])
    else:
        value = settings.setdefault(section, [])
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise MediaStormContractError(
            "MediaStorm source rows have an unsupported shape."
        )
    return value


def _match(row, desired):
    if row.get("name") == desired["name"]:
        return True
    for key in ("url", "baseUrl", "provider"):
        if desired.get(key) and row.get(key) == desired[key]:
            return True
    return bool(
        desired.get("host")
        and all(
            row.get(key) == desired.get(key) for key in ("host", "port", "username")
        )
    )


def _merge(current, planned, records):
    result = copy.deepcopy(current)
    before, after, next_records, expected = {}, {}, copy.deepcopy(records), []
    unowned = False
    for section, identity, proposed in planned:
        rows = _rows(result, section)
        candidates = [(i, row) for i, row in enumerate(rows) if _match(row, proposed)]
        if len(candidates) > 1:
            raise MediaStormInputError(
                "Several native source entries match a selected connection; resolve that ambiguity in MediaStorm first."
            )
        index, old = candidates[0] if candidates else (len(rows), {})
        identity_key = section + "." + identity
        record = records.get(identity_key) or {}
        actual_fields = {key: old.get(key) for key in proposed}
        managed = not old or record.get("snapshot") == fingerprint(
            _snapshot(actual_fields)
        )
        secret_signature = fingerprint(
            {
                key: value
                for key, value in proposed.items()
                if re.search(r"password|apiKey|token", key, re.I)
            }
        )
        desired = {**old, **proposed, "name": old.get("name") or proposed["name"]}
        expected.append((section, identity, desired))
        differs = _snapshot({key: old.get(key) for key in proposed}) != _snapshot(
            {key: desired.get(key) for key in proposed}
        )
        differs |= bool(
            old
            and record.get("secrets") != secret_signature
            and any(re.search(r"password|apiKey|token", key, re.I) for key in proposed)
        )
        if not differs:
            continue
        unowned |= bool(old and not managed)
        for key in set(old) | set(desired):
            before[identity_key + "." + key] = old.get(key)
            after[identity_key + "." + key] = desired.get(key)
        if old:
            rows[index] = desired
        else:
            rows.append(desired)
        next_records[identity_key] = {
            "snapshot": fingerprint(
                _snapshot({key: desired.get(key) for key in proposed})
            ),
            "secrets": secret_signature,
        }
    return result, before, after, next_records, expected, unowned


def _verify_readback(saved, planned):
    for section, _identity, desired in planned:
        matches = [
            row for row in _rows(saved, section) if row.get("name") == desired["name"]
        ]
        if len(matches) != 1:
            raise MediaStormContractError(
                "A saved MediaStorm source identity did not match its readback."
            )
        actual = matches[0]
        for key, value in desired.items():
            if re.search(r"password|apiKey|token", key, re.I):
                if value and not actual.get(key):
                    raise MediaStormContractError(
                        "A saved MediaStorm credential was not present after applying settings."
                    )
            elif actual.get(key) != value:
                raise MediaStormContractError(
                    "Saved MediaStorm source fields did not match their readback."
                )


def _configure(
    client,
    cfg,
    config,
    rid,
    values,
    payload,
    kind,
    operation,
    factory,
    ownership_directory,
    nntp_authenticator,
):
    current = _settings(client)
    ledger = (
        Path(ownership_directory) / "mediastorm.json"
        if ownership_directory
        else Path(cfg.get("config_dir") or "/mediastorm") / ".dumb-arrangement.json"
    )
    records = read_ownership(ledger)
    if kind == "metadata":
        desired = copy.deepcopy(current)
        before, after, unowned = {}, {}, False
        for field, native in (
            ("tmdb_api_key", "tmdbApiKey"),
            ("tvdb_api_key", "tvdbApiKey"),
        ):
            value = values.get(f"{rid}::{field}")
            if not value:
                continue
            if not isinstance(value, str) or len(value) > 8192:
                raise MediaStormInputError("Use a bounded metadata API key.")
            old = current["metadata"].get(native) or ""
            owned = (records.get("metadata." + native) or {}).get(
                "secret"
            ) == fingerprint(value)
            if old and owned:
                continue
            before["metadata." + native], after["metadata." + native] = old, value
            unowned |= bool(old and not owned)
            desired["metadata"][native] = value
        tests = (
            [
                (
                    "metadata",
                    {
                        native: values[f"{rid}::{field}"]
                        for field, native in (
                            ("tmdb_api_key", "tmdbApiKey"),
                            ("tvdb_api_key", "tvdbApiKey"),
                        )
                        if values.get(f"{rid}::{field}")
                    },
                )
            ]
            if any(
                values.get(f"{rid}::{field}")
                for field in ("tmdb_api_key", "tvdb_api_key")
            )
            else []
        )
        if not tests:
            raise MediaStormInputError(
                "Provide a metadata API key to configure or verify."
            )
        next_records = copy.deepcopy(records)
        for key, value in after.items():
            next_records[key] = {"secret": fingerprint(value)}
        expected = []
    else:
        validate_connection_budgets(payload)
        planned, tests = _planned_sources(config, rid, values, factory)
        desired, before, after, next_records, expected, unowned = _merge(
            current, planned, records
        )
        if not planned:
            raise MediaStormInputError(
                "Choose at least one native provider or managed search/playback source to configure."
            )
    proposal = adoption_preview(
        str(ledger) + ".previews",
        rid + "::" + kind,
        before,
        after,
        managed=not unowned,
        operation=operation,
        accepted_id=values.get(f"{rid}::adoption_preview_id"),
        consent=values.get(f"{rid}::adopt_existing", False),
    )
    if operation in {"inspect", "preview"}:
        return _state(
            "waiting_input" if after else "already_present",
            "Selected native settings inspected; no MediaStorm settings changed.",
            evidence=proposal,
        )
    if proposal["requires_consent"] and not proposal["approved"]:
        return _state(
            "waiting_input",
            "Review and accept the exact existing-settings proposal before applying it.",
            evidence=proposal,
            missing_inputs=[f"{rid}::adopt_existing", f"{rid}::adoption_preview_id"],
        )
    if operation in {"recheck", "verify"} and after:
        return _state(
            "waiting_input",
            "Apply the reviewed MediaStorm settings before rechecking them.",
            evidence=proposal,
        )
    if values.get(f"{rid}::authorize_source_tests") is not True:
        raise MediaStormInputError(
            "Authorize the selected source/metadata connection tests explicitly."
        )
    for endpoint, body in tests:
        if endpoint == "nntp":
            nntp_authenticator(
                body, allow_private=values.get(f"{rid}::allow_private_nntp", False)
            )
            continue
        result = _request(client, "POST", "/admin/api/test/" + endpoint, json=body)
        if result.get("success") is not True:
            raise MediaStormInputError(
                "MediaStorm rejected a selected source/metadata connection test. Existing settings were preserved."
            )
    if after:
        if fingerprint(_settings(client)) != fingerprint(current):
            raise MediaStormInputError(
                "MediaStorm settings changed after inspection; refresh the preview before applying."
            )
        _request(client, "PUT", "/api/settings", json=desired)
    saved = _settings(client)
    if kind == "metadata":
        for field in after:
            if not saved["metadata"].get(field.split(".", 1)[1]):
                raise MediaStormContractError(
                    "A saved metadata credential was not present after applying settings."
                )
    else:
        _verify_readback(saved, expected)
    if after:
        write_ownership(ledger, next_records)
    return _state(
        "applied" if after else "already_present",
        "Selected settings were read back and native connection tests passed. No download, import, scan or playback test was run.",
        verification="passed",
        evidence={
            "level": "configuration_readback",
            "native_connection_tests": len(tests),
            "workflow_tested": False,
        },
    )


def _dispatch(
    action,
    config,
    payload,
    operation,
    *,
    client_factory,
    ownership_directory,
    nntp_authenticator,
):
    if action.get("service_key") != "mediastorm" or action.get("action") not in KINDS:
        return None
    rid = action.get("source") or "mediastorm"
    values = _values(payload)
    try:
        with _LOCK:
            client, cfg = _client(config, rid, values, client_factory)
            if action["action"] == "account":
                return _account(client, rid, values, operation)
            _authenticated(client)
            return _configure(
                client,
                cfg,
                config,
                rid,
                values,
                payload,
                action["action"],
                operation,
                client_factory,
                ownership_directory,
                nntp_authenticator,
            )
    except MediaStormContractError as exc:
        return _state(
            "waiting_input", str(exc), verification="unavailable", automation="unknown"
        )
    except MediaStormInputError as exc:
        return _state("waiting_input", str(exc))
    except ApiError as exc:
        if exc.status_code in (401, 403):
            return _state(
                "waiting_input",
                "MediaStorm rejected authorization. Apply the account task with its current primary administrator credentials.",
            )
        if exc.status_code in (404, 405):
            return _state(
                "waiting_input",
                "The installed MediaStorm API contract is unsupported. Use its native administrator interface.",
                verification="unavailable",
                automation="unknown",
            )
        return _state(
            "waiting_dependency",
            "MediaStorm or its selected source is not ready for the bounded connection check.",
            retryable=True,
        )
    except (ValueError, TypeError, KeyError, OSError):
        return _state(
            "waiting_input",
            "Review MediaStorm's selected source settings, generated credentials and administrator authorization.",
        )


def inspect_mediastorm(
    action,
    config,
    payload,
    operation="inspect",
    *,
    client_factory=ApiClient,
    ownership_directory=None,
    nntp_authenticator=authenticate_nntp,
):
    return _dispatch(
        action,
        config,
        payload,
        (
            "preview"
            if operation == "preview"
            else "recheck" if operation in {"verify", "recheck"} else "inspect"
        ),
        client_factory=client_factory,
        ownership_directory=ownership_directory,
        nntp_authenticator=nntp_authenticator,
    )


def apply_mediastorm(
    action,
    config,
    payload,
    operation="apply",
    *,
    client_factory=ApiClient,
    ownership_directory=None,
    nntp_authenticator=authenticate_nntp,
):
    return _dispatch(
        action,
        config,
        payload,
        operation or "apply",
        client_factory=client_factory,
        ownership_directory=ownership_directory,
        nntp_authenticator=nntp_authenticator,
    )
