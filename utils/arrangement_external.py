"""Explicit external Arr inventory and authenticated, read-only readiness.

Credentials live only in protected arrangement inputs. Public resources contain
identity and family, never endpoints or keys. No installer handles these rows.
"""

from __future__ import annotations

import re
import hashlib
import json

from utils.arrangement_http import ApiClient, ApiError

FAMILIES = ("radarr", "sonarr", "lidarr", "whisparr")


class ExternalInputError(ValueError):
    """A safe external inventory or authorization error requiring user input."""


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def global_fields(config, payload):
    return [
        {
            "key": "setup::external_arrs",
            "resource_id": "setup",
            "label": "Existing external Arr servers",
            "type": "list",
            "default": [],
            "group_label": "Existing external services",
            "item_label": "External Arr server",
            "add_label": "Add external Arr server",
            "refresh_on_change": True,
            "max_items": 32,
            "identity_fields": ["id", "url", "service_key"],
            "item_fields": [
                {"key": "id", "type": "string", "label": "Stable ID", "required": True},
                {"key": "label", "type": "string", "label": "Display name"},
                {
                    "key": "service_key",
                    "type": "select",
                    "label": "Application",
                    "required": True,
                    "options": [
                        {"value": f, "label": f.capitalize()} for f in FAMILIES
                    ],
                },
                {
                    "key": "url",
                    "type": "string",
                    "label": "API base URL",
                    "required": True,
                },
                {
                    "key": "api_key",
                    "type": "password",
                    "label": "API key",
                    "secret": True,
                    "required": True,
                },
                {
                    "key": "allow_private",
                    "type": "boolean",
                    "label": "Allow private network endpoint",
                    "default": False,
                },
                {
                    "key": "verify_tls",
                    "type": "boolean",
                    "label": "Verify TLS certificate",
                    "default": True,
                },
                {
                    "key": "authorize_changes",
                    "type": "boolean",
                    "label": "Authorize configuration writes to this server",
                    "default": False,
                },
            ],
            "description": "Uses your existing servers without installing or restarting them. Authorization for configuration writes is separate from read-only discovery.",
        }
    ]


def rows(payload):
    values = (payload.get("arrangement") or {}).get("values") or {}
    result = values.get("setup::external_arrs") or []
    if not isinstance(result, list) or len(result) > 32:
        raise ExternalInputError("Provide a bounded list of external Arr servers.")
    identifiers = set()
    for row in result:
        if not isinstance(row, dict):
            raise ExternalInputError("External Arr entries must be objects.")
        identifier = row.get("id")
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise ExternalInputError(
                "Use a stable external Arr ID containing letters, numbers, underscores or hyphens."
            )
        if identifier in identifiers:
            raise ExternalInputError("External Arr IDs must be unique.")
        identifiers.add(identifier)
        if row.get("service_key") not in FAMILIES:
            raise ExternalInputError("Choose a supported external Arr application.")
        for field in ("allow_private", "verify_tls", "authorize_changes"):
            if field in row and not isinstance(row[field], bool):
                raise ExternalInputError(
                    "External endpoint permissions must be boolean."
                )
    return result


def external_resources(payload):
    return [
        {
            "id": "external_arr." + row["id"],
            "service_key": row["service_key"],
            "instance_key": row["id"],
            "external": True,
            "selection_kind": "external",
            "name": str(row.get("label") or row["id"])[:120],
            "roles": ["library_manager"],
            "installable": False,
        }
        for row in rows(payload)
    ]


def target_family(payload, identifier):
    if not isinstance(identifier, str):
        return None
    if not identifier.startswith("external_arr."):
        return identifier.partition(".")[0]
    return next(
        (
            row["service_key"]
            for row in rows(payload)
            if "external_arr." + row["id"] == identifier
        ),
        None,
    )


def target_options(payload, allowed=FAMILIES):
    return [
        {"value": r["id"], "label": r["name"] + " (external " + r["service_key"] + ")"}
        for r in external_resources(payload)
        if r["service_key"] in allowed
    ]


def resolve_target(
    payload, identifier, *, client_factory=ApiClient, require_write=False
):
    row = next(
        (r for r in rows(payload) if "external_arr." + r["id"] == identifier), None
    )
    if row is None:
        raise ExternalInputError(
            "The selected external Arr server is no longer configured."
        )
    if require_write and not row.get("authorize_changes", False):
        raise ExternalInputError(
            "Authorize configuration writes for this external Arr server before continuing."
        )
    key = row.get("api_key")
    if (
        not isinstance(key, str)
        or not key
        or len(key) > 8192
        or any(c in key for c in "\r\n")
    ):
        raise ExternalInputError("Provide a valid external Arr API key.")
    url = row.get("url")
    if not isinstance(url, str):
        raise ExternalInputError("Provide an external Arr base URL.")
    client = client_factory(
        url,
        headers={"X-Api-Key": key},
        managed=False,
        allow_private=row.get("allow_private", False),
        verify_tls=row.get("verify_tls", True),
    )
    family = row["service_key"]
    prefix = "/api/v1" if family == "lidarr" else "/api/v3"
    response = client.request("GET", prefix + "/system/status")
    response.raise_for_status()
    status = response.json()
    if not isinstance(status, dict) or not isinstance(status.get("version"), str):
        raise ExternalInputError(
            "The external Arr API has an unsupported system identity."
        )
    name = str(status.get("appName") or status.get("app_name") or "").lower()
    if name != family:
        raise ExternalInputError(
            "The external endpoint identifies a different Arr application."
        )
    match = re.match(r"^(\d+)\.", status["version"])
    if not match:
        raise ExternalInputError("The external Arr version could not be identified.")
    scope = hashlib.sha256(
        # Private external endpoint/key scope binds ownership to its native identity.
        # This is not a login verifier; preserve existing sidecar identities.
        # codeql[py/weak-sensitive-data-hashing]
        json.dumps(
            {"family": family, "url": url.rstrip("/"), "api_key": key}, sort_keys=True
        ).encode()
    ).hexdigest()
    client._setup_ownership_scope = scope
    return {
        "id": identifier,
        "family": family,
        "url": url.rstrip("/"),
        "key": key,
        "client": client,
        "api_prefix": prefix,
        "major": int(match[1]),
        "external": True,
        "write_authorized": row.get("authorize_changes", False),
        "ownership_scope": scope,
    }


def inspect_external(
    action, config, payload, operation="inspect", *, client_factory=ApiClient, **kwargs
):
    source = action.get("source") or action.get("id", "").split("::", 1)[0]
    if action.get("action") != "external_ready" or not source.startswith(
        "external_arr."
    ):
        return None
    try:
        target = resolve_target(payload, source, client_factory=client_factory)
        return {
            "execution": "already_present",
            "verification": "passed",
            "retryable": False,
            "reason": "The existing Arr server authenticated and reported the expected application identity.",
            "evidence": {
                "level": "authenticated_readiness",
                "family": target["family"],
                "major": target["major"],
            },
        }
    except ApiError as exc:
        auth = exc.status_code in {401, 403}
        return {
            "execution": "waiting_input" if auth else "waiting_dependency",
            "verification": "failed",
            "retryable": not auth,
            "reason": (
                "The external Arr authorization was rejected."
                if auth
                else "The external Arr API is not ready."
            ),
        }
    except (ValueError, TypeError, OSError):
        return {
            "execution": "waiting_input",
            "verification": "unavailable",
            "retryable": False,
            "reason": "Review the external Arr endpoint, identity, permissions and credentials.",
            "missing_inputs": ["setup::external_arrs"],
        }


def apply_external(action, config, payload, operation="apply", **kwargs):
    """External readiness is always read-only, including the apply operation."""
    return inspect_external(action, config, payload, operation, **kwargs)
