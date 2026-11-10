"""Versioned application connection adapters for retained setup actions.

Contracts inspected against Seerr API v1 settings, Pulsarr v1 instance schemas,
Maintainerr settings API and Bazarr system/settings. This module never initializes
an account, starts a workflow, imports profiles, searches or creates retention
rules. Unknown response shapes stop the action instead of inferring success.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from urllib.parse import urlsplit

import defusedxml.ElementTree as ET
import yaml

from utils.arrangement_http import ApiClient, ApiError
from utils.arrangement_adoption import preview as adoption_preview
from utils.arrangement_external import (
    resolve_target,
    ExternalInputError,
    target_family,
    target_options,
)
from utils.integration_ownership import read_ownership, write_ownership

DEFAULT_OWNERSHIP_DIRECTORY = "/config/.dumb-setup/connections"
SUPPORTED = frozenset({"seerr", "pulsarr", "bazarr", "maintainerr"})
ARR_FAMILIES = ("radarr", "sonarr")
_LOCK = threading.RLock()


class DependencyNeeded(RuntimeError):
    pass


class InputNeeded(ValueError):
    def __init__(self, reason, fields=()):
        super().__init__(reason)
        self.fields = list(fields)


class UnsupportedContract(InputNeeded):
    """An unrecognized versioned API contract, distinct from missing user input."""


def _scope(item):
    source = item.get("source") or item.get("id") or item.get("service_key") or ""
    source = str(source).split("::", 1)[0]
    family, _, instance = source.partition(".")
    return source, item.get("service_key") or family, instance or None


def _config(config, identifier):
    family, _, instance = identifier.partition(".")
    block = config.get(family) or {}
    if not isinstance(block, dict):
        return {}
    return ((block.get("instances") or {}).get(instance) or {}) if instance else block


def _values(payload):
    arrangement = payload.get("arrangement") or {}
    return arrangement.get("values") or {}


def _value(values, source, field, default=None):
    return values.get(f"{source}::{field}", default)


def _field(source, name, label, kind="string", **kwargs):
    return {
        "key": f"{source}::{name}",
        "resource_id": source,
        "label": label,
        "type": kind,
        **kwargs,
    }


def _read_file(path):
    if not path or not os.path.isfile(path):
        return None
    # Generated native application configuration may contain credentials; never
    # return its contents through discovery/evidence or exception messages.
    if os.path.getsize(path) > 2 * 1024 * 1024:
        raise InputNeeded("Application configuration exceeds the supported size.")
    return Path(path).read_text(encoding="utf-8")


def _native(config, source):
    settings = _config(config, source)
    family = source.split(".", 1)[0]
    port = settings.get("port") or settings.get("host_port")
    try:
        port = int(port)
        if not 0 < port < 65536:
            raise ValueError
    except (ValueError, TypeError):
        raise InputNeeded(
            "Set a valid application port before configuring this connection."
        )
    base, key = "", ""
    raw = _read_file(settings.get("config_file"))
    if raw:
        try:
            if family in {"radarr", "sonarr", "lidarr", "whisparr", "prowlarr"}:
                root = ET.fromstring(raw)
                key = (root.findtext(".//ApiKey") or "").strip()
                base = (root.findtext(".//UrlBase") or "").strip()
            elif family == "seerr":
                key = str((json.loads(raw).get("main") or {}).get("apiKey") or "")
            elif family == "bazarr":
                native = yaml.safe_load(raw) or {}
                key = str((native.get("auth") or {}).get("apikey") or "")
                base = str((native.get("general") or {}).get("base_url") or "")
        except (ValueError, TypeError, ET.ParseError, yaml.YAMLError):
            raise InputNeeded(
                "The application configuration is not initialized or has an unsupported format."
            ) from None
    if family == "pulsarr":
        base = str((settings.get("env") or {}).get("basePath") or "")
    base = base.strip().strip("/")
    if any(character in base for character in "?#\\\r\n") or base.startswith(
        ("http:", "https:")
    ):
        raise InputNeeded("The application's native base path must be a relative path.")
    return settings, f"http://127.0.0.1:{port}" + ("/" + base if base else ""), key


def _client(config, source, values, client_factory):
    settings, host, native_key = _native(config, source)
    family = source.split(".", 1)[0]
    key = _value(values, source, "api_key") or native_key
    if key and (
        not isinstance(key, str) or len(key) > 8192 or any(ch in key for ch in "\r\n")
    ):
        raise InputNeeded("Use a valid application API key without line breaks.")
    if family in {"seerr", "pulsarr", "bazarr", "profilarr"} and not key:
        raise InputNeeded(
            "Provide an application API key after completing its administrator setup.",
            [f"{source}::api_key"],
        )
    client = client_factory(
        host, headers={"X-Api-Key": str(key)} if key else {}, managed=True
    )
    client._setup_ownership_scope = hashlib.sha256(
        json.dumps({"family": family, "url": host, "key": key}, sort_keys=True).encode()
    ).hexdigest()
    return client, settings


def _request(client, method, path, *, body=None, form=None):
    response = client.request(method, path, json=body, data=form)
    response.raise_for_status()
    if response.status_code in {204, 205} or not response.text.strip():
        return None
    value = response.json()
    if isinstance(value, dict) and (
        value.get("status") == "NOK" or value.get("success") is False
    ):
        raise ApiError("The application rejected its connection check.")
    return value


def _list(value):
    if (
        not isinstance(value, list)
        or len(value) > 1000
        or any(not isinstance(row, dict) for row in value)
    ):
        raise UnsupportedContract(
            "The installed application API has an unsupported response shape."
        )
    return value


def _case(value):
    return {str(key).lower(): item for key, item in value.items()}


def _target(config, identifier, client_factory, payload=None):
    external = identifier.startswith("external_arr.")
    family = target_family(payload or {}, identifier)
    if family not in ARR_FAMILIES:
        raise InputNeeded("Choose a supported Radarr or Sonarr instance.")
    if external:
        try:
            target = resolve_target(
                payload or {}, identifier, client_factory=client_factory
            )
        except ValueError as exc:
            raise InputNeeded(str(exc), ["setup::external_arrs"]) from None
        host, key, client = target["url"], target["key"], target["client"]
        settings = {"enabled": True}
    else:
        settings, host, key = _native(config, identifier)
        client = client_factory(host, headers={"X-Api-Key": key}, managed=True)
    if not settings.get("enabled"):
        raise InputNeeded("The assigned Arr instance is disabled or missing.")
    if not key:
        raise InputNeeded(
            "Waiting for the selected Arr application's generated API key."
        )
    status = _request(client, "GET", "/api/v3/system/status")
    if not isinstance(status, dict) or not isinstance(status.get("version"), str):
        raise UnsupportedContract(
            "The selected Arr API does not provide a supported system identity."
        )
    name = str(status.get("appName") or status.get("app_name") or "").lower()
    if name and name != family:
        raise InputNeeded(
            "The selected endpoint identifies a different Arr application."
        )
    version = re.match(r"^(\d+)\.", status["version"])
    if not version:
        raise UnsupportedContract("The selected Arr version could not be identified.")
    # Sonarr 3 exposes separate language profiles; Sonarr 4 removed that API.
    profiles = _list(_request(client, "GET", "/api/v3/qualityprofile"))
    roots = _list(_request(client, "GET", "/api/v3/rootfolder"))
    languages = (
        _list(_request(client, "GET", "/api/v3/languageprofile"))
        if family == "sonarr" and int(version[1]) <= 3
        else []
    )
    return {
        "id": identifier,
        "family": family,
        "url": host,
        "key": key,
        "profiles": profiles,
        "roots": roots,
        "languages": languages,
        "major": int(version[1]),
        "client": client,
        "ownership_scope": hashlib.sha256(
            json.dumps(
                {"family": family, "url": host, "key": key}, sort_keys=True
            ).encode()
        ).hexdigest(),
    }


def _options(rows, key="id", label="name"):
    return [
        {"value": row[key], "label": str(row.get(label) or row[key])}
        for row in rows
        if key in row and isinstance(row[key], (str, int))
    ]


def schema_fields(resource, config, payload):
    """Advertise inputs without contacting apps; safe during initial planning."""
    source, family, _ = _scope(resource)
    # Prowlarr assignments belong to the companion helper, which supports
    # Lidarr and Whisparr as well. Do not overwrite its authoritative field with
    # this consumer adapter's Radarr/Sonarr-only choices.
    if family not in SUPPORTED | {"profilarr"}:
        return []
    arrs = [
        {"value": f"{arr}.{key}", "label": f"{arr.capitalize()} — {key}"}
        for arr in ARR_FAMILIES
        for key, inst in ((_config(config, arr).get("instances")) or {}).items()
        if isinstance(inst, dict) and inst.get("enabled")
    ]
    arrs.extend(target_options(payload, ARR_FAMILIES))
    fields = []
    if family != "profilarr":
        fields.append(
            _field(
                source,
                "arr_instances",
                "Arr instances to connect",
                "multiselect",
                options=arrs,
                description="Choose exact connections; an empty selection connects none.",
            )
        )
    if family in {"seerr", "pulsarr", "bazarr", "profilarr"}:
        fields.append(
            _field(
                source,
                "api_key",
                "Application API key",
                "password",
                secret=True,
                description="Use an authorized application key. Leave empty to reuse a generated managed key when available.",
            )
        )
    if family in SUPPORTED:
        fields.append(
            _field(
                source,
                "adopt_existing",
                "Allow DUMB to manage matching existing connection fields",
                "boolean",
                default=False,
                description="Explicitly adopt only selected matching connections; unrelated policy, rules and settings are preserved.",
            )
        )
    if family in SUPPORTED:
        fields.append(
            _field(
                source,
                "adoption_preview_id",
                "Accepted adoption preview",
                "string",
                description="Bound to the exact inspected native state and proposed changes.",
            )
        )
    if family == "bazarr":
        fields.append(
            _field(
                source,
                "enable_integrations",
                "Enable selected Bazarr integrations and their existing schedules",
                "boolean",
                default=False,
            )
        )
        fields.append(
            _field(
                source,
                "language_profile",
                "Existing subtitle language profile",
                "select",
                options=[],
            )
        )
        fields.append(
            _field(
                source,
                "languages",
                "Additional enabled subtitle languages",
                "multiselect",
                options=[],
            )
        )
    selected = _value(_values(payload), source, "arr_instances", []) or []
    if family in {"seerr", "pulsarr"}:
        for target in selected:
            if not isinstance(target, str) or target not in {
                row["value"] for row in arrs
            }:
                continue
            prefix = f"{target}::"
            fields.extend(
                [
                    _field(
                        source,
                        prefix + "quality_profile",
                        f"{target}: quality profile",
                        "select",
                        options=[],
                        required=True,
                    ),
                    _field(
                        source,
                        prefix + "root_folder",
                        f"{target}: root folder",
                        "select",
                        options=[],
                        required=True,
                    ),
                    _field(
                        source,
                        prefix + "is_default",
                        f"{target}: make default",
                        "boolean",
                        default=False,
                    ),
                ]
            )
            if family == "seerr":
                fields.append(
                    _field(
                        source,
                        prefix + "is_4k",
                        f"{target}: use for 4K requests",
                        "boolean",
                        default=False,
                    )
                )
            if target_family(payload, target) == "radarr":
                fields.append(
                    _field(
                        source,
                        prefix + "minimum_availability",
                        f"{target}: minimum availability",
                        "select",
                        options=[
                            {"value": value, "label": value}
                            for value in ("announced", "inCinemas", "released")
                        ],
                        default="released",
                    )
                )
            elif family == "pulsarr":
                fields.append(
                    _field(
                        source,
                        prefix + "series_type",
                        f"{target}: series type",
                        "select",
                        options=[
                            {"value": value, "label": value}
                            for value in ("standard", "anime", "daily")
                        ],
                        default="standard",
                    )
                )
            fields.append(
                _field(
                    source,
                    prefix + "language_profile",
                    f"{target}: Sonarr 3 language profile",
                    "select",
                    options=[],
                )
            )
    return fields


def discover_fields(action_or_resource, config, payload, *, client_factory=ApiClient):
    """Read-only authenticated discovery; partial readiness keeps input fields usable."""
    fields = schema_fields(action_or_resource, config, payload)
    source, family, _ = _scope(action_or_resource)
    values = _values(payload)
    for target_id in _value(values, source, "arr_instances", []) or []:
        if family not in {"seerr", "pulsarr"}:
            break
        try:
            target = _target(config, target_id, client_factory, payload)
        except (InputNeeded, DependencyNeeded, ApiError, OSError, ValueError):
            continue
        for field in fields:
            if field["key"] == f"{source}::{target_id}::quality_profile":
                field["options"] = _options(target["profiles"])
            elif field["key"] == f"{source}::{target_id}::root_folder":
                field["options"] = _options(target["roots"], "path", "path")
            elif field["key"] == f"{source}::{target_id}::language_profile":
                field["options"] = _options(target["languages"])
    if family == "bazarr":
        try:
            client, _ = _client(config, source, values, client_factory)
            profiles = _list(_request(client, "GET", "/api/system/languages/profiles"))
            languages = _list(_request(client, "GET", "/api/system/languages"))
            for field in fields:
                if field["key"] == f"{source}::language_profile":
                    field["options"] = _options(profiles, "profileId")
                elif field["key"] == f"{source}::languages":
                    field["options"] = _options(languages, "code2")
        except (InputNeeded, DependencyNeeded, ApiError, OSError, ValueError):
            pass
    return fields


def _outcome(
    execution,
    reason,
    *,
    verification="not_checked",
    retryable=False,
    missing=(),
    evidence=(),
    automation=None,
):
    return {
        **({"automation": automation} if automation else {}),
        "execution": execution,
        "verification": verification,
        "reason": reason,
        "detail": reason,
        "retryable": retryable,
        "missing_inputs": list(missing),
        "evidence": evidence if isinstance(evidence, dict) else list(evidence),
    }


def _ledger_path(directory, source, *, write=False):
    root = Path(directory)
    if write:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.exists():
        info = root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise InputNeeded(
                "Connection ownership storage must be private and controller-owned."
            )
    elif root.is_symlink():
        raise InputNeeded("Connection ownership storage cannot be a symbolic link.")
    return root / (hashlib.sha256(source.encode()).hexdigest() + ".json")


def _fingerprint(row, family):
    names = (
        ("name", "baseUrl")
        if family == "pulsarr"
        else (
            ("serverName", "url")
            if family == "maintainerr"
            else ("name", "hostname", "port", "useSsl", "baseUrl")
        )
    )
    public = {key: row.get(key) for key in names}
    return hashlib.sha256(json.dumps(public, sort_keys=True).encode()).hexdigest()


def _path(family, arr):
    return (
        f"/v1/{arr}/instances"
        if family == "pulsarr"
        else (
            f"/api/settings/{arr}"
            if family == "maintainerr"
            else f"/api/v1/settings/{arr}"
        )
    )


def _matches(row, target, family):
    if family in {"pulsarr", "maintainerr"}:
        url = row.get("baseUrl" if family == "pulsarr" else "url") or ""
        return (
            str(url).rstrip("/") == target["url"].rstrip("/")
            or row.get("name", row.get("serverName")) == f"DUMB {target['id']}"
        )
    parsed = urlsplit(target["url"])
    return (
        str(row.get("hostname") or "").lower() == parsed.hostname
        and str(row.get("port"))
        == str(parsed.port or (443 if parsed.scheme == "https" else 80))
        and str(row.get("baseUrl") or "").strip("/") == parsed.path.strip("/")
    ) or row.get("name") == f"DUMB {target['id']}"


def _policy(source, target, values, rows, family, existing):
    prefix = f"{target['id']}::"
    selected_profile = _value(values, source, prefix + "quality_profile")
    selected_root = _value(values, source, prefix + "root_folder")
    profiles = {str(item["id"]): item for item in target["profiles"] if "id" in item}
    roots = {
        str(item["path"])
        for item in target["roots"]
        if isinstance(item.get("path"), str)
    }
    if str(selected_profile) not in profiles:
        raise InputNeeded(
            "Choose an available quality profile for each selected Arr instance.",
            [f"{source}::{prefix}quality_profile"],
        )
    if selected_root not in roots:
        raise InputNeeded(
            "Choose an available root folder for each selected Arr instance.",
            [f"{source}::{prefix}root_folder"],
        )
    chosen = profiles[str(selected_profile)]
    default = _value(
        values,
        source,
        prefix + "is_default",
        existing.get("isDefault", False) if existing else False,
    )
    four_k = _value(
        values,
        source,
        prefix + "is_4k",
        existing.get("is4k", False) if existing else False,
    )
    if not isinstance(default, bool) or not isinstance(four_k, bool):
        raise InputNeeded("Default and 4K routing choices must be boolean.")
    # Setting a default can silently clear another application's default. Require
    # the user to resolve that existing route explicitly rather than stealing it.
    if default and any(
        row.get("isDefault")
        and row.get("id") != (existing or {}).get("id")
        and (family != "seerr" or bool(row.get("is4k")) == four_k)
        for row in rows
    ):
        raise InputNeeded(
            "Another connection is already the default. Review the default route in the application first."
        )
    if family == "pulsarr":
        policy = {
            "qualityProfile": chosen["id"],
            "rootFolder": selected_root,
            "isDefault": default,
        }
        if target["family"] == "radarr":
            policy["minimumAvailability"] = _value(
                values, source, prefix + "minimum_availability", "released"
            )
        else:
            policy["seriesType"] = _value(
                values, source, prefix + "series_type", "standard"
            )
        return policy
    policy = {
        "activeProfileId": chosen["id"],
        "activeProfileName": str(chosen.get("name") or chosen["id"]),
        "activeDirectory": selected_root,
        "isDefault": default,
        "is4k": four_k,
    }
    if target["family"] == "radarr":
        policy["minimumAvailability"] = _value(
            values, source, prefix + "minimum_availability", "released"
        )
    elif target["major"] <= 3:
        language = _value(values, source, prefix + "language_profile")
        available = {
            str(row["id"]): row["id"] for row in target["languages"] if "id" in row
        }
        if str(language) not in available:
            raise InputNeeded(
                "Choose a Sonarr 3 language profile.",
                [f"{source}::{prefix}language_profile"],
            )
        policy["activeLanguageProfileId"] = available[str(language)]
    return policy


def _test(client, family, target, desired):
    arr = target["family"]
    if family == "maintainerr":
        response = _request(client, "POST", f"/api/settings/test/{arr}", body=desired)
        if not isinstance(response, dict) or response.get("status") != "OK":
            raise ApiError("Maintainerr could not verify the connection.")
    elif family == "pulsarr":
        response = _request(
            client,
            "POST",
            f"/v1/{arr}/test-connection",
            body={"baseUrl": target["url"], "apiKey": target["key"]},
        )
        if not isinstance(response, dict) or response.get("success") is not True:
            raise ApiError("Pulsarr could not verify the connection.")
    else:
        response = _request(
            client, "POST", f"/api/v1/settings/{arr}/test", body=desired
        )
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("profiles"), list)
            or not isinstance(response.get("rootFolders"), list)
        ):
            raise ApiError("Seerr could not verify the connection.")


def _desired(client, family, source, target, values, rows, existing):
    name = f"DUMB {target['id']}"
    if family == "maintainerr":
        if target["url"] != target["url"].lower():
            raise InputNeeded(
                "The Maintainerr settings API lowercases connection URLs. Use a case-stable route before connecting this native base path."
            )
        return {
            **(copy.deepcopy(existing) if existing else {}),
            "serverName": (existing or {}).get("serverName") or name,
            "url": target["url"],
            "apiKey": target["key"],
        }
    policy = _policy(source, target, values, rows, family, existing)
    if family == "pulsarr":
        result = {
            **(copy.deepcopy(existing) if existing else {}),
            "name": (existing or {}).get("name") or name,
            "baseUrl": target["url"],
            "apiKey": target["key"],
            **policy,
        }
        if not existing:
            # Creating a connection never enables searches or watchlist workflows.
            result.update(searchOnAdd=False, bypassIgnored=False)
            if target["family"] == "sonarr":
                result.update(seasonMonitoring="none", monitorNewItems="none")
            else:
                result["monitor"] = "none"
        return result
    parsed = urlsplit(target["url"])
    result = {
        **(copy.deepcopy(existing) if existing else {}),
        "name": (existing or {}).get("name") or name,
        "hostname": parsed.hostname,
        "port": parsed.port or (443 if parsed.scheme == "https" else 80),
        "useSsl": parsed.scheme == "https",
        "baseUrl": parsed.path,
        "apiKey": target["key"],
        **policy,
    }
    if not existing:
        result.update(syncEnabled=False, preventSearch=True)
        if target["family"] == "sonarr":
            result["enableSeasonFolders"] = False
    return result


def _connection_matches(actual, desired, family):
    for key, value in desired.items():
        if key == "id":
            continue
        existing = actual.get(key)
        if (
            family == "pulsarr"
            and key == "qualityProfile"
            and isinstance(value, int)
            and not isinstance(value, bool)
        ):
            if isinstance(existing, str) and existing == str(value):
                continue
        if existing != value:
            return False
    return True


def _consumer_connections(
    action, config, payload, operation, factory, ownership_directory, target_ids=None
):
    source, family, _ = _scope(action)
    values = _values(payload)
    selected = _value(values, source, "arr_instances")
    if not isinstance(selected, list) or not selected:
        raise InputNeeded(
            "Select the exact Arr instances to connect.", [f"{source}::arr_instances"]
        )
    if len(selected) > 50 or any(
        not isinstance(identifier, str) for identifier in selected
    ):
        raise InputNeeded("Choose a bounded list of Arr instance identities.")
    client, _ = _client(config, source, values, factory)
    path = _ledger_path(ownership_directory, source)
    ownership = read_ownership(path)
    outcomes = []
    for target_id in dict.fromkeys(selected):
        if target_ids is not None and target_id not in target_ids:
            continue
        try:
            target = _target(config, target_id, factory, payload)
            route = _path(family, target["family"])
            rows = _list(_request(client, "GET", route))
            saved_record = ownership.get(target_id) or {}
            record = (
                saved_record
                if saved_record.get("source_scope") == client._setup_ownership_scope
                and saved_record.get("target_scope") == target["ownership_scope"]
                else {}
            )
            existing = next(
                (
                    row
                    for row in rows
                    if record.get("id") is not None and row.get("id") == record["id"]
                ),
                None,
            )
            candidates = [row for row in rows if _matches(row, target, family)]
            drifted = bool(
                existing and record.get("fingerprint") != _fingerprint(existing, family)
            )
            unowned = existing is None and bool(candidates)
            if unowned:
                if len(candidates) != 1:
                    raise InputNeeded(
                        "Several existing connections match this target. Resolve the ambiguity in the application."
                    )
                existing = candidates[0]
            if existing and (
                not isinstance(existing.get("id"), int)
                or isinstance(existing.get("id"), bool)
                or existing["id"] < 0
            ):
                raise UnsupportedContract(
                    "The application returned an unsupported connection identity."
                )
            desired = _desired(client, family, source, target, values, rows, existing)
            # Reading back existing state is sufficient for inspection, but not for
            # authenticated connection verification from the consuming application.
            current = bool(existing) and _connection_matches(existing, desired, family)
            needs_adoption = bool(existing and (unowned or drifted) and not current)
            proposal = adoption_preview(
                _ledger_path(
                    ownership_directory,
                    source + ".previews",
                    write=operation == "preview",
                ),
                source
                + "::"
                + target_id
                + "::"
                + client._setup_ownership_scope
                + "::"
                + target["ownership_scope"],
                existing or {},
                desired,
                secret_fields={"apiKey"},
                managed=not needs_adoption,
                operation=operation,
                accepted_id=_value(values, source, "adoption_preview_id"),
                consent=_value(values, source, "adopt_existing", False),
            )
            if operation in {"inspect", "preview"}:
                outcomes.append(
                    {
                        "target": target_id,
                        "configuration": "already_present" if current else "pending",
                        **proposal,
                    }
                )
                continue
            if needs_adoption and not proposal["approved"]:
                return _outcome(
                    "waiting_input",
                    "Preview the matching native connection and accept that exact proposal before adoption.",
                    missing=[
                        source + "::adopt_existing",
                        source + "::adoption_preview_id",
                    ],
                    evidence={"target": target_id, **proposal},
                )
            _test(client, family, target, desired)
            if operation in {"verify", "recheck"}:
                if not current:
                    return _outcome(
                        "waiting_input",
                        "The selected connection settings are incomplete; apply the reviewed connection first.",
                    )
                outcomes.append(
                    {
                        "target": target_id,
                        "configuration": "already_present",
                        "connection_test": "passed",
                    }
                )
                continue
            if operation not in {"apply", "retry"}:
                raise InputNeeded("Unsupported setup operation.")
            if not current:
                endpoint = route + (f"/{existing['id']}" if existing else "")
                saved = _request(
                    client, "PUT" if existing else "POST", endpoint, body=desired
                )
                # Some APIs return only an id, others a full row, or no body on update.
                saved_id = (
                    existing.get("id")
                    if existing
                    else (saved or {}).get("id") if isinstance(saved, dict) else None
                )
                readback = _list(_request(client, "GET", route))
                stored = next(
                    (
                        row
                        for row in readback
                        if saved_id is not None and row.get("id") == saved_id
                    ),
                    None,
                )
                if stored is None:
                    matches = [row for row in readback if _matches(row, target, family)]
                    stored = matches[0] if len(matches) == 1 else None
                if stored is None or not isinstance(stored.get("id"), int):
                    raise ApiError(
                        "The application did not return a saved connection identity."
                    )
                # Persist identity before further work; failed verification must not
                # turn the next retry into a duplicate create.
                ownership[target_id] = {
                    "id": stored["id"],
                    "fingerprint": _fingerprint(stored, family),
                    "source_scope": client._setup_ownership_scope,
                    "target_scope": target["ownership_scope"],
                }
                path = _ledger_path(ownership_directory, source, write=True)
                write_ownership(path, ownership)
                if not _connection_matches(stored, desired, family):
                    raise ApiError(
                        "Saved connection settings did not match their readback."
                    )
                _test(client, family, target, stored)
            elif (
                existing
                and target_id not in ownership
                and _value(values, source, "adopt_existing", False)
            ):
                ownership[target_id] = {
                    "id": existing["id"],
                    "fingerprint": _fingerprint(existing, family),
                    "source_scope": client._setup_ownership_scope,
                    "target_scope": target["ownership_scope"],
                }
                path = _ledger_path(ownership_directory, source, write=True)
                write_ownership(path, ownership)
            outcomes.append(
                {
                    "target": target_id,
                    "configuration": "already_present" if current else "applied",
                    "connection_test": "passed",
                }
            )
        except (
            InputNeeded,
            DependencyNeeded,
            ApiError,
            ValueError,
            OSError,
            TypeError,
            KeyError,
        ) as exc:
            failure = _failure(exc)
            outcomes.append({"target": target_id, **failure})
    unfinished = [
        item
        for item in outcomes
        if item.get("execution") in {"waiting_input", "waiting_dependency", "failed"}
    ]
    if unfinished:
        failure = unfinished[0]
        return _outcome(
            failure["execution"],
            failure["reason"],
            verification=failure["verification"],
            retryable=any(item.get("retryable") for item in unfinished),
            missing=[
                key for item in unfinished for key in item.get("missing_inputs", [])
            ],
            evidence=outcomes,
            automation=(
                "unknown"
                if any(item.get("automation") == "unknown" for item in unfinished)
                else None
            ),
        )

    if operation in {"inspect", "preview"}:
        ready = all(item["configuration"] == "already_present" for item in outcomes)
        return _outcome(
            "already_present" if ready else "waiting_input",
            "Existing connection settings were inspected; no application settings changed.",
            evidence=outcomes[0] if len(outcomes) == 1 else {"connections": outcomes},
        )
    return _outcome(
        (
            "applied"
            if any(item["configuration"] == "applied" for item in outcomes)
            else "already_present"
        ),
        "Selected connections were read back and authenticated by the consuming application. Workflow activation remains explicit.",
        verification="passed",
        evidence=outcomes,
    )


def _bazarr(
    action, config, payload, operation, factory, ownership_directory, target_ids=None
):
    source, _, _ = _scope(action)
    values = _values(payload)
    selected = _value(values, source, "arr_instances") or []
    if not isinstance(selected, list) or len(selected) > 2:
        raise InputNeeded(
            "Bazarr supports one Sonarr and one Radarr connection; choose at most one of each."
        )
    if any(not isinstance(identifier, str) for identifier in selected):
        raise InputNeeded("Choose canonical Arr instance names.")
    if len({target_family(payload, identifier) for identifier in selected}) != len(
        selected
    ):
        raise InputNeeded(
            "A Bazarr instance supports only one connection per Arr family."
        )
    targets = [
        _target(config, identifier, factory, payload)
        for identifier in selected
        if target_ids is None or identifier in target_ids
    ]
    client, _ = _client(config, source, values, factory)
    settings = _request(client, "GET", "/api/system/settings")
    if not isinstance(settings, dict):
        raise UnsupportedContract(
            "This Bazarr version does not expose supported settings."
        )
    settings = {
        key.lower(): _case(value) if isinstance(value, dict) else value
        for key, value in settings.items()
    }
    general = settings.get("general") or {}
    if not isinstance(general, dict):
        raise UnsupportedContract(
            "This Bazarr version has an unsupported general-settings shape."
        )
    ownership_path = _ledger_path(ownership_directory, source)
    ownership = read_ownership(ownership_path)
    form, expected = {}, {}
    ownable = set()
    needs_adoption = False
    general_expected = {}
    for target in targets:
        family = target["family"]
        current = settings.get(family)
        if not isinstance(current, dict) or not {
            "ip",
            "port",
            "apikey",
            "base_url",
            "ssl",
        }.issubset(current):
            raise UnsupportedContract(
                "The installed Bazarr settings schema is unsupported."
            )
        configured = bool(current.get("apikey"))
        saved_record = ownership.get(family) or {}
        record = (
            saved_record
            if saved_record.get("source_scope") == client._setup_ownership_scope
            and saved_record.get("target_scope") == target["ownership_scope"]
            else {}
        )
        public = {key: current.get(key) for key in ("ip", "port", "base_url", "ssl")}
        signature = hashlib.sha256(
            json.dumps(public, sort_keys=True).encode()
        ).hexdigest()
        parsed = urlsplit(target["url"])
        desired = {
            "ip": parsed.hostname,
            "port": parsed.port or (443 if parsed.scheme == "https" else 80),
            "base_url": parsed.path or "/",
            "ssl": parsed.scheme == "https",
            "apikey": target["key"],
        }
        owned = record.get("fingerprint") == signature
        changing = any(current.get(key) != value for key, value in desired.items())
        needs_adoption = needs_adoption or bool(configured and not owned and changing)
        if not configured or owned:
            ownable.add(family)
        for key, value in desired.items():
            if current.get(key) != value:
                form[f"settings-{family}-{key}"] = (
                    str(value).lower() if isinstance(value, bool) else str(value)
                )
        expected[family] = desired
        if _value(values, source, "enable_integrations", False) and not general.get(
            f"use_{family}"
        ):
            form[f"settings-general-use_{family}"] = "true"
            general_expected[f"use_{family}"] = True
    # Language addition is a union: the API replaces its enabled-language list.
    languages = _value(values, source, "languages", []) or []
    if languages:
        available = _list(_request(client, "GET", "/api/system/languages"))
        codes = {row.get("code2") for row in available}
        if not set(languages).issubset(codes):
            raise InputNeeded(
                "Choose subtitle languages advertised by the installed Bazarr version."
            )
        union = sorted(
            set(languages) | {row["code2"] for row in available if row.get("enabled")}
        )
        if set(union) != {row["code2"] for row in available if row.get("enabled")}:
            form["languages-enabled"] = union
    profile = _value(values, source, "language_profile")
    if profile not in (None, ""):
        profiles = _list(_request(client, "GET", "/api/system/languages/profiles"))
        if str(profile) not in {str(row.get("profileId")) for row in profiles}:
            raise InputNeeded(
                "Choose an existing Bazarr language profile; create a new profile in Bazarr first."
            )
        for target in targets:
            kind = "movie" if target["family"] == "radarr" else "serie"
            if str(general.get(f"{kind}_default_profile")) != str(profile):
                form[f"settings-general-{kind}_default_profile"] = str(profile)
            if not general.get(f"{kind}_default_enabled"):
                form[f"settings-general-{kind}_default_enabled"] = "true"
            general_expected[f"{kind}_default_profile"] = str(profile)
            general_expected[f"{kind}_default_enabled"] = True
    before_fields = {
        family + "." + key: (settings.get(family) or {}).get(key)
        for family, wanted in expected.items()
        for key in wanted
    }
    after_fields = {
        family + "." + key: value
        for family, wanted in expected.items()
        for key, value in wanted.items()
    }
    for key, value in general_expected.items():
        before_fields["general." + key] = general.get(key)
        after_fields["general." + key] = value
    if languages:
        before_fields["enabled_languages"] = sorted(
            row["code2"] for row in available if row.get("enabled")
        )
        after_fields["enabled_languages"] = union
    proposal = adoption_preview(
        _ledger_path(
            ownership_directory,
            source + ".bazarr-previews",
            write=operation == "preview",
        ),
        source
        + "::bazarr::"
        + client._setup_ownership_scope
        + "::"
        + hashlib.sha256(
            json.dumps(sorted(target["ownership_scope"] for target in targets)).encode()
        ).hexdigest(),
        before_fields,
        after_fields,
        managed=not needs_adoption,
        operation=operation,
        accepted_id=_value(values, source, "adoption_preview_id"),
        consent=_value(values, source, "adopt_existing", False),
    )
    if operation in {"inspect", "preview"}:
        return _outcome(
            "waiting_input" if form else "already_present",
            "Bazarr connection settings inspected; providers and search policy remain unchanged.",
            evidence=proposal,
        )
    if needs_adoption and not proposal["approved"]:
        return _outcome(
            "waiting_input",
            "Preview and accept the exact existing Bazarr connection changes before adoption.",
            missing=[source + "::adopt_existing", source + "::adoption_preview_id"],
            evidence=proposal,
        )
    if proposal["approved"]:
        ownable.update(target["family"] for target in targets)

    if operation in {"verify", "recheck"} and form:
        return _outcome(
            "waiting_input",
            "Apply the reviewed Bazarr connection fields before checking them.",
        )
    if operation in {"apply", "retry"} and form:
        _request(client, "POST", "/api/system/settings", form=form)
    readback = _request(client, "GET", "/api/system/settings")
    if not isinstance(readback, dict):
        raise ApiError("Bazarr settings could not be read back.")
    readback = {
        key.lower(): _case(value) if isinstance(value, dict) else value
        for key, value in readback.items()
    }
    for family, wanted in expected.items():
        if any(
            (readback.get(family) or {}).get(key) != value
            for key, value in wanted.items()
        ):
            raise ApiError(
                "Bazarr connection readback did not match the selected instance."
            )
        public = {key: wanted[key] for key in ("ip", "port", "base_url", "ssl")}
        if family in ownable:
            ownership[family] = {
                "fingerprint": hashlib.sha256(
                    json.dumps(public, sort_keys=True).encode()
                ).hexdigest(),
                "source_scope": client._setup_ownership_scope,
                "target_scope": next(
                    target["ownership_scope"]
                    for target in targets
                    if target["family"] == family
                ),
            }
    for key, expected_value in general_expected.items():
        actual = (readback.get("general") or {}).get(key)
        if str(actual) != str(expected_value):
            raise ApiError(
                "Bazarr language profile or enablement readback did not match."
            )
    if languages:
        current_languages = _list(_request(client, "GET", "/api/system/languages"))
        if not set(languages).issubset(
            {row.get("code2") for row in current_languages if row.get("enabled")}
        ):
            raise ApiError("Bazarr enabled-language readback did not match.")
    if operation in {"apply", "retry"} and ownership:
        write_ownership(
            _ledger_path(ownership_directory, source, write=True), ownership
        )
    enabled = all(
        (readback.get("general") or {}).get(f"use_{target['family']}")
        for target in targets
    )
    if not enabled:
        return _outcome(
            "waiting_input",
            "Connection fields are saved. Explicitly enable the selected Bazarr integrations after reviewing subtitle providers and schedules.",
            missing=[f"{source}::enable_integrations"],
            evidence=[{"method": "configuration_readback", "result": "passed"}],
        )
    return _outcome(
        "applied" if form else "already_present",
        "Bazarr connection fields were read back. Target authentication was checked from DUMB; configure subtitle providers separately.",
        evidence=[
            {"method": "configuration_readback", "result": "passed"},
            {"method": "target_authenticated_from_controller", "result": "passed"},
        ],
    )


def _profilarr(action, config, payload, operation, factory):
    source, _, _ = _scope(action)
    values = _values(payload)
    client, _ = _client(config, source, values, factory)
    status = _request(client, "GET", "/api/v1/status")
    if not isinstance(status, dict):
        raise UnsupportedContract(
            "The installed Profilarr version does not expose supported synchronization status."
        )
    arrs = _list(_request(client, "GET", "/api/v1/arr"))
    # The current versioned public API exposes status and Arr reads but no
    # profile-selection mutation. Never replace that missing contract with
    # internal form actions or direct database policy writes.
    return _outcome(
        "waiting_input",
        "Select a database and profiles/custom formats in Profilarr, preview the changes, then run the chosen sync. This installed public API does not support profile-selection writes.",
        automation="unsupported",
        verification="unavailable",
        evidence=[
            {"method": "profilarr_public_status", "result": "read"},
            {"method": "profilarr_arr_inventory", "count": len(arrs)},
        ],
    )


def _failure(exc):
    if isinstance(exc, UnsupportedContract):
        return _outcome(
            "waiting_input", str(exc), automation="unknown", verification="unavailable"
        )
    if isinstance(exc, DependencyNeeded):
        return _outcome(
            "waiting_dependency",
            "Waiting for the selected application's generated configuration or API key.",
            retryable=True,
        )
    if isinstance(exc, (InputNeeded, ExternalInputError)):
        return _outcome(
            "waiting_input",
            str(exc),
            missing=getattr(exc, "fields", ["setup::external_arrs"]),
        )
    if isinstance(exc, ApiError) and exc.status_code in {401, 403}:
        return _outcome(
            "waiting_input",
            "The application rejected setup authorization. Supply an API key with administrator settings access.",
        )
    if isinstance(exc, ApiError) and exc.status_code in {404, 405}:
        return _outcome(
            "waiting_input",
            "The installed application version does not expose the expected setup API. Review its service page.",
            automation="unknown",
            verification="unavailable",
        )
    if isinstance(exc, ApiError) and exc.status_code in {400, 422}:
        return _outcome(
            "waiting_input",
            "The application rejected the proposed connection schema. Review the selected fields in its service page.",
        )
    return _outcome(
        "failed",
        "The connection could not be configured or verified. Existing settings were retained where no write succeeded; retry after checking application readiness.",
        retryable=True,
    )


def apply_connection(
    action,
    config,
    payload,
    operation="apply",
    *,
    client_factory=ApiClient,
    ownership_directory=DEFAULT_OWNERSHIP_DIRECTORY,
    target_ids=None,
):
    source, family, _ = _scope(action)
    kind = action.get("action")
    if family not in SUPPORTED | {"profilarr"} or kind not in {
        "arr_connections",
        "policy",
        "profiles",
        "languages",
    }:
        return None
    if family != "bazarr" and family != "profilarr" and kind != "arr_connections":
        return None
    try:
        if operation not in {
            "apply",
            "retry",
            "inspect",
            "preview",
            "verify",
            "recheck",
        }:
            raise InputNeeded("Unsupported setup operation.")
        with _LOCK:
            if family == "profilarr":
                return _profilarr(action, config, payload, operation, client_factory)
            if family == "bazarr":
                return _bazarr(
                    action,
                    config,
                    payload,
                    operation,
                    client_factory,
                    ownership_directory,
                    target_ids,
                )
            return _consumer_connections(
                action,
                config,
                payload,
                operation,
                client_factory,
                ownership_directory,
                target_ids,
            )
    except (
        InputNeeded,
        DependencyNeeded,
        ApiError,
        ValueError,
        OSError,
        TypeError,
        KeyError,
    ) as exc:
        return _failure(exc)


def inspect_connection(
    action,
    config,
    payload,
    operation="inspect",
    *,
    client_factory=ApiClient,
    ownership_directory=DEFAULT_OWNERSHIP_DIRECTORY,
    target_ids=None,
):
    return apply_connection(
        action,
        config,
        payload,
        (
            "recheck"
            if operation in {"recheck", "verify"}
            else "preview" if operation == "preview" else "inspect"
        ),
        client_factory=client_factory,
        ownership_directory=ownership_directory,
        target_ids=target_ids,
    )
