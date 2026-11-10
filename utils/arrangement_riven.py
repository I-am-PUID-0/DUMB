"""Standalone Riven settings readiness and explicitly selected media connections.

Official rivenmedia/riven v0.20.1, v0.23.6 (2f201350) and main (ae94fea2):
routers/secure/settings.py, auth.py and program/settings/models.py. The stable
partial POST set/all recursively merges, validates and persists the supplied
fields. Updater enablement, library paths, scraper and content policy are never
changed here. Authenticated identity checks do not scan or modify media.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from utils.arrangement_adoption import preview
from utils.arrangement_app_media import AppMediaInputError, _identity, _target
from utils.arrangement_http import ApiClient, ApiError
from utils.arrangement_media import UnsupportedMediaVersion, _resource, _values
from utils.integration_ownership import fingerprint, read_ownership, write_ownership


class RivenInputError(ValueError):
    pass


class RivenContractError(ValueError):
    pass


def _state(
    execution,
    reason,
    *,
    verification="not_checked",
    retryable=False,
    evidence=None,
    **extra,
):
    return dict(
        execution=execution,
        verification=verification,
        reason=reason,
        retryable=retryable,
        evidence=evidence or {},
        **extra,
    )


def schema_fields(resource, config, payload=None):
    del payload
    if resource.get("service_key") != "riven_backend":
        return []
    source = resource["id"]
    options = [{"value": "none", "label": "Keep the current media connections"}]
    options += [
        {"value": key, "label": key.capitalize()}
        for key in ("plex", "jellyfin", "emby")
        if (config.get(key) or {}).get("enabled")
    ]
    options.append({"value": "external", "label": "Explicit external media server"})
    fields = [
        (
            "api_key",
            "Riven backend API key",
            "password",
            "Optional override; otherwise reuse the backend's generated native key. Retained only in protected setup inputs.",
        ),
        (
            "media_server",
            "Selected Riven media server",
            "connection",
            "Configure only the selected Plex, Jellyfin or Emby connection. Existing updater enablement, library paths and content choices remain unchanged.",
        ),
        (
            "external_media",
            "External Riven media server",
            "external_service",
            "Choose the exact Plex, Jellyfin or Emby URL and application key, including explicit private-network/TLS choices.",
        ),
        (
            "adopt_existing",
            "Accept the exact existing connection changes",
            "boolean",
            "Existing custom URLs or credentials require the current field preview before they can be replaced.",
        ),
        (
            "adoption_preview_id",
            "Accepted Riven connection preview",
            "string",
            "Opaque identifier for the exact fresh before/after connection preview.",
        ),
    ]
    return [
        dict(
            key=source + "::" + name,
            resource_id=source,
            service_key="riven_backend",
            label=label,
            type=kind,
            description=description,
            required=False,
            **({"secret": True} if kind == "password" else {}),
            **(
                {"options": options, "refresh_on_change": True}
                if name == "media_server"
                else {}
            ),
            **(
                {
                    "visible_when": {
                        "key": source + "::media_server",
                        "equals": "external",
                    }
                }
                if name == "external_media"
                else {}
            ),
            **({"default": False} if kind == "boolean" else {}),
        )
        for name, label, kind, description in fields
    ]


def _native_path(cfg):
    return Path(
        cfg.get("config_file")
        or str(
            Path(cfg.get("config_dir") or "/riven/backend") / "data" / "settings.json"
        )
    )


def _read_native(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise RivenInputError(
                "Riven settings must be an existing regular native JSON file."
            )
        content = handle.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024:
        raise RivenInputError(
            "Riven settings exceed the supported private readback size."
        )
    result = json.loads(content)
    if not isinstance(result, dict):
        raise RivenContractError("Unsupported native Riven settings document.")
    return result


def _request(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    document = response.json()
    if not isinstance(document, dict):
        raise RivenContractError("Unsupported Riven settings response.")
    return document


def _properties(schema, node):
    if not isinstance(node, dict):
        raise RivenContractError("Unsupported Riven settings schema.")
    if "$ref" in node:
        reference = node["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            raise RivenContractError("Unsupported Riven settings schema reference.")
        node = (schema.get("$defs") or {}).get(reference[len("#/$defs/") :])
    if not isinstance(node, dict) or not isinstance(node.get("properties"), dict):
        raise RivenContractError("Unsupported Riven settings object schema.")
    return node["properties"]


def _source(action, config, payload, factory):
    cfg, values = _resource(config, action["source"]), _values(payload)
    if not cfg.get("enabled"):
        raise RivenInputError(
            "Enable the selected Riven backend before checking its settings."
        )
    path = _native_path(cfg)
    native = _read_native(path)
    key = values.get(action["source"] + "::api_key") or native.get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise RivenInputError(
            "Wait for Riven to generate its backend API key, or supply the selected backend's key."
        )
    if len(key) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in key):
        raise RivenInputError("The Riven API key contains an unsupported character.")
    port = int(cfg.get("port") or 8080)
    if not 1 <= port <= 65535:
        raise RivenInputError("Use a valid configured Riven backend port.")
    origin = f"http://127.0.0.1:{port}"
    client = factory(origin, headers={"X-Api-Key": key}, managed=True, timeout=8)
    schema = _request(client, "GET", "/api/v1/settings/schema")
    properties = _properties(schema, schema)
    if (
        not isinstance(properties.get("api_key"), dict)
        or properties["api_key"].get("type") != "string"
    ):
        raise RivenContractError(
            "The native schema does not expose the supported authenticated Riven settings contract."
        )
    version = _request(client, "GET", "/api/v1/settings/get/version").get("version")
    if (
        not isinstance(version, str)
        or not version
        or len(version) > 80
        or any(ord(ch) < 32 for ch in version)
    ):
        raise RivenContractError("The native Riven version readback is unsupported.")
    return cfg, path, native, key, origin, client, schema, version


def _connection(document, service):
    row = (
        (document.get("updaters") or {}).get(service)
        if isinstance(document.get("updaters"), dict)
        else None
    )
    if not isinstance(row, dict):
        raise RivenContractError(
            "The selected Riven updater does not expose the supported settings contract."
        )
    auth = "token" if service == "plex" else "api_key"
    if (
        type(row.get("enabled")) is not bool
        or not isinstance(row.get("url"), str)
        or not isinstance(row.get(auth), str)
    ):
        raise RivenContractError(
            "The selected Riven updater settings have unsupported field types."
        )
    return row, auth


def _media(action, config, payload, operation, *, apply, factory, source):
    cfg, path, _native, key, origin, client, schema, version = source
    values = _values(payload)
    target = _target(action, config, payload, factory)
    if target is None:
        return _state(
            "already_present",
            "The selected choice preserves Riven's current media connections.",
            verification="passed",
            evidence={"selection": "unchanged", "backend_version": version},
        )
    service = target["service"]
    identifier, _name = _identity(target)
    root_properties = _properties(schema, schema)
    updater_properties = _properties(schema, root_properties.get("updaters"))
    selected_properties = _properties(schema, updater_properties.get(service))
    auth = "token" if service == "plex" else "api_key"
    if (
        (selected_properties.get("enabled") or {}).get("type") != "boolean"
        or (selected_properties.get(auth) or {}).get("type") != "string"
        or "url" not in selected_properties
    ):
        raise RivenContractError(
            "This Riven version does not expose the selected media updater contract."
        )
    response = _request(client, "GET", "/api/v1/settings/get/updaters." + service)
    row, auth = _connection(
        {"updaters": {service: response.get("updaters." + service)}}, service
    )
    before = {"url": row["url"], auth: row[auth]}
    after = {"url": target["url"], auth: target["key"]}
    evidence = {
        "backend_version": version,
        "media_server": service,
        "authenticated_identity": "passed",
        "updater_enabled": row["enabled"],
        "content_or_scan_started": False,
    }
    directory = path.parent
    identity = fingerprint({"origin": origin, "key": key, "service": service})
    ledger_path = directory / ".dumb-riven-connection-ownership.json"
    records = read_ownership(ledger_path)
    owned = (records.get(identity) or {}).get("fingerprint") == fingerprint(before)
    changed = before != after
    default_url = f"http://localhost:{32400 if service == 'plex' else 8096}"
    fresh = not before[auth] and before["url"].rstrip("/") in ("", default_url)
    proposal = preview(
        directory / ".dumb-riven-connection-preview.json",
        identity,
        before,
        after,
        secret_fields=(auth,),
        managed=bool(owned or fresh),
        operation=operation or ("apply" if apply else "inspect"),
        accepted_id=values.get(action["source"] + "::adoption_preview_id"),
        consent=values.get(action["source"] + "::adopt_existing") is True,
    )
    if changed:
        evidence["adoption"] = proposal
        if not apply:
            return _state(
                "planned",
                "The selected authenticated media connection is ready for the exact settings preview.",
                evidence=evidence,
            )
        if proposal["requires_consent"] and not proposal["approved"]:
            return _state(
                "waiting_input",
                "Accept the fresh Riven connection preview before replacing existing user settings.",
                evidence=evidence,
            )
        # Stable across the supported releases and main: recursive partial merge,
        # model validation, notification and native persistence. No enabled flag.
        _request(
            client,
            "POST",
            "/api/v1/settings/set/all",
            json={"updaters": {service: after}},
        )
    current = _request(client, "GET", "/api/v1/settings/get/updaters." + service)
    live, _ = _connection(
        {"updaters": {service: current.get("updaters." + service)}}, service
    )
    disk, _ = _connection(_read_native(path), service)
    expected = {**row, **after}
    if live != expected or disk != expected:
        return _state(
            "failed",
            "Riven media connection did not match authenticated and durable native readback.",
            verification="failed",
            retryable=True,
            evidence=evidence,
        )
    # Verify the same selected identity again after configuration, without any
    # library update/scan. A swapped endpoint cannot silently pass this check.
    if _identity(target)[0] != identifier:
        return _state(
            "failed",
            "The selected media server identity changed during configuration.",
            verification="failed",
            evidence=evidence,
        )
    if apply:
        records[identity] = {"fingerprint": fingerprint(after)}
        write_ownership(ledger_path, records)
    return _state(
        "applied" if changed else "already_present",
        "The selected Riven media connection passed authenticated identity and persistent settings readback; updater enablement was preserved.",
        verification="passed",
        evidence=evidence,
    )


def _dispatch(action, config, payload, operation, *, apply, factory):
    if action.get("service_key") != "riven_backend" or action.get("action") not in {
        "backend_connection",
        "media_connection",
    }:
        return None
    try:
        source = _source(action, config, payload, factory)
        if action["action"] == "media_connection":
            return _media(
                action,
                config,
                payload,
                operation,
                apply=apply,
                factory=factory,
                source=source,
            )
        cfg, path, native, key, origin, client, schema, version = source
        del cfg, path, native, origin, client, schema
        return _state(
            "already_present",
            "The standalone Riven backend generated key, authenticated settings schema and version readback are ready.",
            verification="passed",
            evidence={
                "backend_version": version,
                "standalone": True,
                "settings_authenticated": "passed",
            },
            **(
                {"_private_values": {action["source"] + "::api_key": key}}
                if apply
                else {}
            ),
        )
    except (RivenInputError, AppMediaInputError) as exc:
        return _state("waiting_input", str(exc))
    except (RivenContractError, UnsupportedMediaVersion):
        return _state(
            "unsupported",
            "This Riven version does not expose the recognized authenticated settings/media contract. Existing settings were preserved; update Riven or complete the connection in its UI.",
            verification="unavailable",
        )
    except ApiError as exc:
        if exc.status_code in (401, 403):
            return _state(
                "waiting_input",
                "The selected backend or media server rejected its application key. Refresh authorization and retry.",
                verification="failed",
            )
        if exc.status_code in (404, 405):
            return _state(
                "unsupported",
                "The installed application does not expose the supported settings endpoint; existing settings were preserved.",
                verification="unavailable",
            )
        return _state(
            "waiting_dependency",
            "Riven or the selected media API is not ready for authenticated settings readback.",
            retryable=True,
        )
    except FileNotFoundError:
        return _state(
            "waiting_dependency",
            "Waiting for the backend's generated native settings file.",
            retryable=True,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return _state(
            "waiting_dependency",
            "Native Riven settings or authenticated API readiness is incomplete. Recheck after startup.",
            retryable=True,
        )


def inspect_riven(action, config, payload, operation=None, *, client_factory=ApiClient):
    return _dispatch(
        action, config, payload, operation, apply=False, factory=client_factory
    )


def apply_riven(action, config, payload, operation=None, *, client_factory=ApiClient):
    return _dispatch(
        action, config, payload, operation, apply=True, factory=client_factory
    )
