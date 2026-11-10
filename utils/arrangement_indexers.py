"""Schema-driven indexer provisioning through supported Servarr APIs.

Definitions and field types come from the running application's schema. No
tracker credentials are guessed, interactive challenge responses are supplied
by the user, and omitted existing indexers are never removed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets

from utils.arrangement_http import ApiClient, ApiError, validate_endpoint
from utils.arrangement_external import resolve_target, target_family
from utils.arrangement_adoption import preview
from utils.arrangement_connections import (
    _scope,
    _values,
    _field,
    _client,
    _native,
    _request,
    _list,
    _ledger_path,
    _failure,
    InputNeeded,
    UnsupportedContract,
)
from utils.integration_ownership import read_ownership, write_ownership

FAMILIES = {"prowlarr", "radarr", "sonarr", "lidarr", "whisparr"}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _endpoint(resource, config, payload, factory, *, write=False):
    source, family, _ = _scope(resource)
    family = (
        target_family(payload, source) if source.startswith("external_arr.") else family
    )
    if family not in FAMILIES:
        return None
    if source.startswith("external_arr."):
        target = resolve_target(
            payload, source, client_factory=factory, require_write=write
        )
        return target["client"], target["api_prefix"]
    if family == "prowlarr":
        client, _ = _client(config, source, _values(payload), factory)
    else:
        _, url, key = _native(config, source)
        if not key:
            raise InputNeeded("Waiting for the Arr application's generated API key.")
        client = factory(url, headers={"X-Api-Key": key}, managed=True)
    _, source_url, native_key = _native(config, source)
    source_key = _values(payload).get(source + "::api_key") or native_key
    client._setup_ownership_scope = hashlib.sha256(
        # Private indexer endpoint/key scope prevents ownership crossing native servers.
        # This is not a login verifier; preserve existing sidecar identities.
        # codeql[py/weak-sensitive-data-hashing]
        json.dumps(
            {"family": family, "url": source_url, "key": source_key}, sort_keys=True
        ).encode()
    ).hexdigest()
    return client, "/api/v1" if family in {"prowlarr", "lidarr"} else "/api/v3"


def _rows(source, payload):
    entries = _values(payload).get(source + "::indexers") or []
    if not isinstance(entries, list) or len(entries) > 32:
        raise InputNeeded("Provide at most 32 indexer entries.")
    seen = set()
    for row in entries:
        if not isinstance(row, dict) or not SAFE_ID.fullmatch(str(row.get("id") or "")):
            raise InputNeeded(
                "Each indexer needs a stable ID containing letters, numbers, underscores or hyphens."
            )
        if row["id"] in seen:
            raise InputNeeded("Indexer IDs must be unique within an application.")
        seen.add(row["id"])
    return entries


def _definition(row):
    identity = {
        key: row.get(key)
        for key in ("implementation", "configContract", "definitionName")
    }
    for field in row.get("fields") or []:
        if field.get("name") == "definitionFile":
            identity["definitionFile"] = field.get("value")
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[
        :24
    ]


def _native_fields(template, source, row_id):
    fields = []
    destination = next(
        (
            f["name"]
            for f in template.get("fields") or []
            if str(f.get("name", "")).lower() in {"baseurl", "base_url", "url", "host"}
        ),
        None,
    )
    for field in template.get("fields") or []:
        name, kind = field.get("name"), str(field.get("type") or "textbox").lower()
        if (
            not isinstance(name, str)
            or len(name) > 160
            or kind in {"info", "hidden", "button"}
        ):
            continue
        secret = bool(
            field.get("privacy") in {"password", "apiKey", "api_key", 1, 2}
        ) or any(
            word in name.lower()
            for word in (
                "password",
                "apikey",
                "api_key",
                "token",
                "cookie",
                "passkey",
                "secret",
                "username",
                "login",
                "email",
            )
        )
        secret = secret or kind == "captcha"
        options = field.get("selectOptions") or []
        advertised = _field(
            source,
            "indexer::" + row_id + "::" + name,
            row_id + ": " + str(field.get("label") or name),
            (
                "password"
                if secret
                else (
                    "boolean"
                    if kind == "checkbox"
                    else (
                        "select"
                        if options
                        else "number" if kind == "number" else "string"
                    )
                )
            ),
            secret=secret,
        )
        if secret and destination:
            advertised["credential_destination_key"] = (
                source + "::indexer::" + row_id + "::" + destination
            )
        if options:
            advertised["options"] = [
                {
                    "value": option["value"],
                    "label": str(option.get("name") or option["value"]),
                }
                for option in options
                if "value" in option
            ]
        if "value" in field and not secret:
            advertised["default"] = field["value"]
        fields.append(advertised)
    return fields


def schema_fields(resource, config, payload):
    source, family, _ = _scope(resource)
    if source.startswith("external_arr."):
        family = target_family(payload, source)
    if family not in FAMILIES:
        return []
    return [
        _field(
            source,
            "indexers",
            "Private and Usenet indexers",
            "list",
            default=[],
            max_items=32,
            refresh_on_change=True,
            item_label="Indexer",
            add_label="Add indexer",
            item_fields=[
                {"key": "id", "type": "string", "label": "Stable ID", "required": True},
                {"key": "name", "type": "string", "label": "Name", "required": True},
                {
                    "key": "definition",
                    "type": "select",
                    "label": "Indexer definition",
                    "options": [],
                    "required": True,
                },
                {
                    "key": "app_profile",
                    "type": "select",
                    "label": "Prowlarr application profile",
                    "options": [],
                },
                {
                    "key": "enable",
                    "type": "boolean",
                    "label": "Enable this indexer",
                    "default": False,
                },
                {
                    "key": "priority",
                    "type": "number",
                    "label": "Priority",
                    "default": 25,
                },
                {
                    "key": "allow_private",
                    "type": "boolean",
                    "label": "Allow a private indexer endpoint",
                    "default": False,
                },
                {
                    "key": "authorize_changes",
                    "type": "boolean",
                    "label": "Authorize testing and saving this indexer",
                    "default": False,
                },
            ],
            description="Select a native definition, provide its advertised credentials, preview changes and authorize its connection test. Existing indexers and tags are preserved.",
        ),
        _field(
            source,
            "adopt_existing",
            "Adopt matching user-managed indexer fields",
            "boolean",
            default=False,
        ),
        _field(source, "adoption_preview_id", "Accepted adoption preview", "string"),
    ]


def _schemas(client, prefix):
    # Current Prowlarr ships hundreds of Cardigann definitions; its documented
    # schema endpoint is not paginated (native 2.6.5 measures about 6 MB).
    response = client.request(
        "GET",
        prefix + "/indexer/schema",
        response_limit=8 * 1024 * 1024,
        json_node_limit=1000000,
    )
    response.raise_for_status()
    return _list(response.json())


def discover_fields(resource, config, payload, *, client_factory=ApiClient):
    fields = schema_fields(resource, config, payload)
    if not fields:
        return []
    source, _, _ = _scope(resource)
    try:
        client, prefix = _endpoint(resource, config, payload, client_factory)
        definitions = _schemas(client, prefix)
        options = [
            {
                "value": _definition(row),
                "label": str(
                    row.get("name")
                    or row.get("definitionName")
                    or row.get("implementation")
                ),
            }
            for row in definitions
        ]
        fields[0]["item_fields"][2]["options"] = options
        if (
            prefix == "/api/v1"
            and not source.startswith("external_arr.")
            and source.startswith("prowlarr.")
        ):
            profiles = _list(_request(client, "GET", prefix + "/appprofile"))
            fields[0]["item_fields"][3]["options"] = [
                {"value": row["id"], "label": str(row.get("name") or row["id"])}
                for row in profiles
                if "id" in row
            ]
        by_id = {_definition(row): row for row in definitions}
        for row in _rows(source, payload):
            if row.get("definition") in by_id:
                fields.extend(
                    _native_fields(by_id[row["definition"]], source, row["id"])
                )
    except (InputNeeded, ApiError, ValueError, OSError, TypeError):
        pass
    return fields


def _fields(row):
    return {
        item["name"]: item.get("value")
        for item in row.get("fields") or []
        if isinstance(item, dict) and "name" in item
    }


def _comparison(row):
    return {
        **{
            key: row.get(key)
            for key in (
                "name",
                "enable",
                "enableRss",
                "enableAutomaticSearch",
                "enableInteractiveSearch",
                "priority",
                "appProfileId",
            )
        },
        **{"setting." + key: value for key, value in _fields(row).items()},
    }


def _test(client, prefix, desired):
    findings = _request(client, "POST", prefix + "/indexer/test", body=desired)
    if findings not in (None, [], {}, "", "{}"):
        raise InputNeeded(
            "The native indexer test reported findings. Review credentials, endpoint, categories and any required challenge in the application."
        )


def apply_indexers(
    action,
    config,
    payload,
    operation="apply",
    *,
    client_factory=ApiClient,
    ownership_directory="/config/.dumb-setup/connections",
):
    if action.get("action") != "indexers" or not schema_fields(action, config, payload):
        return None
    source, _, _ = _scope(action)
    try:
        entries = _rows(source, payload)
        if not entries:
            return {
                "execution": "declined",
                "verification": "unavailable",
                "retryable": False,
                "reason": "No indexer provisioning was selected; existing native indexers are preserved.",
            }
        writes = operation in {"apply", "retry", "start"}
        client, prefix = _endpoint(
            action, config, payload, client_factory, write=writes
        )
        definitions = {_definition(row): row for row in _schemas(client, prefix)}
        native = _list(_request(client, "GET", prefix + "/indexer"))
        ledger = _ledger_path(ownership_directory, source + ".indexers", write=False)
        owned = {
            key: record
            for key, record in read_ownership(ledger).items()
            if isinstance(record, dict)
            and record.get("endpoint_scope") == client._setup_ownership_scope
        }
        values = _values(payload)
        outcomes, changed = [], False
        for entry in entries:
            template = definitions.get(entry.get("definition"))
            if template is None:
                raise InputNeeded(
                    "Choose an indexer definition advertised by the running application.",
                    [source + "::indexers"],
                )
            matches = [
                row
                for row in native
                if row.get("id") == (owned.get(entry["id"]) or {}).get("id")
            ]
            if not matches:
                matches = [
                    row for row in native if row.get("name") == entry.get("name")
                ]
            if len(matches) > 1:
                raise InputNeeded(
                    "Several native indexers match this name. Resolve the ambiguity first."
                )
            existing = matches[0] if matches else None
            if existing and _definition(existing) != _definition(template):
                raise InputNeeded(
                    "The matching native indexer uses a different definition; choose a distinct name or review it manually."
                )
            desired = copy.deepcopy(existing or template)
            if not existing:
                desired.pop("id", None)
            desired["name"] = str(entry.get("name") or "").strip()
            if not desired["name"] or len(desired["name"]) > 120:
                raise InputNeeded("Provide a bounded indexer name.")
            if not isinstance(entry.get("enable", False), bool):
                raise InputNeeded(
                    "Indexer activation must be an explicit boolean choice."
                )
            if source.startswith("prowlarr."):
                desired["enable"] = entry.get("enable", False)
                if str(desired.get("protocol", "")).lower() in {
                    "usenet",
                    "1",
                } and desired.get("supportsRedirect", False):
                    desired["redirect"] = True
                priority = int(entry.get("priority", 25))
                if not 1 <= priority <= 50:
                    raise InputNeeded("Indexer priority must be between 1 and 50.")
                desired["priority"] = priority
                profile = entry.get("app_profile")
                if profile not in (None, ""):
                    desired["appProfileId"] = int(profile)
                if not desired.get("appProfileId"):
                    raise InputNeeded(
                        "Choose a Prowlarr application profile for the new indexer."
                    )
            elif not existing:
                desired.update(
                    enableRss=False,
                    enableAutomaticSearch=False,
                    enableInteractiveSearch=entry.get("enable", False),
                )
            for field in desired.get("fields") or []:
                key = (
                    source + "::indexer::" + entry["id"] + "::" + field.get("name", "")
                )
                if key in values:
                    value = values[key]
                    if value == "[configured]":
                        raise InputNeeded(
                            "Re-enter the actual indexer credential instead of a redaction marker."
                        )
                    native_options = field.get("selectOptions") or []
                    if native_options:
                        choices = {
                            str(option["value"]): option["value"]
                            for option in native_options
                            if "value" in option
                        }
                        if str(value) not in choices:
                            raise InputNeeded(
                                "Choose an advertised indexer field option."
                            )
                        value = choices[str(value)]
                    if value not in ("", None) or field.get("type") == "checkbox":
                        field["value"] = value
                if str(field.get("name", "")).lower() in {
                    "baseurl",
                    "base_url",
                    "url",
                } and field.get("value"):
                    validate_endpoint(
                        str(field["value"]),
                        allow_private=entry.get("allow_private", False),
                    )
            owned_entry = bool(
                existing
                and existing.get("id") == (owned.get(entry["id"]) or {}).get("id")
            )
            before, after = _comparison(existing or {}), _comparison(desired)
            credential_digest = hashlib.sha256(
                # Private credential equality handles masked native indexer readback.
                # This is not a login verifier; keep existing credential-change detection.
                # codeql[py/weak-sensitive-data-hashing]
                json.dumps(
                    {
                        key: value
                        for key, value in after.items()
                        if any(
                            word in key.lower()
                            for word in (
                                "password",
                                "apikey",
                                "api_key",
                                "token",
                                "cookie",
                                "passkey",
                                "secret",
                            )
                        )
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if (
                existing
                and (owned.get(entry["id"]) or {}).get("credential_digest")
                == credential_digest
            ):
                for key in before:
                    if before[key] == "********" and key in after:
                        before[key] = after[key]
            different = before != after
            proposal = preview(
                _ledger_path(
                    ownership_directory,
                    source + ".indexer-previews",
                    write=operation == "preview",
                ),
                source
                + "::indexer::"
                + entry["id"]
                + "::"
                + client._setup_ownership_scope,
                before,
                after,
                managed=owned_entry or not existing,
                operation=operation,
                consent=values.get(source + "::adopt_existing", False),
                accepted_id=values.get(source + "::adoption_preview_id"),
            )
            if operation in {"preview", "inspect"}:
                outcomes.append({"id": entry["id"], **proposal})
                continue
            batch_records = read_ownership(
                _ledger_path(ownership_directory, source + ".indexer-batch-previews")
            )
            accepted = (
                batch_records.get(source + "::" + client._setup_ownership_scope) or {}
            )
            proposal["approved"] = bool(
                values.get(source + "::adopt_existing")
                and values.get(source + "::adoption_preview_id") == accepted.get("id")
                and proposal.get("preview_id")
                and (accepted.get("members") or {}).get(entry["id"])
                == proposal["preview_id"]
            )
            if writes and different:
                if not entry.get("authorize_changes", False):
                    return {
                        "execution": "waiting_input",
                        "verification": "not_checked",
                        "retryable": False,
                        "reason": "Authorize the selected indexer's native connection test and configuration write.",
                        "missing_inputs": [source + "::indexers"],
                        "evidence": proposal,
                    }
                if existing and not owned_entry and not proposal["approved"]:
                    return {
                        "execution": "waiting_input",
                        "verification": "not_checked",
                        "retryable": False,
                        "reason": "Preview and accept the exact matching native indexer changes before adoption.",
                        "evidence": proposal,
                        "missing_inputs": [
                            source + "::adopt_existing",
                            source + "::adoption_preview_id",
                        ],
                    }
                _test(client, prefix, desired)
                endpoint = (
                    prefix
                    + "/indexer"
                    + ("/" + str(existing["id"]) if existing else "")
                )
                if existing:
                    desired["id"] = existing["id"]
                saved = _request(
                    client, "PUT" if existing else "POST", endpoint, body=desired
                )
                readback = _list(_request(client, "GET", prefix + "/indexer"))
                saved_id = existing["id"] if existing else (saved or {}).get("id")
                stored = next(
                    (row for row in readback if row.get("id") == saved_id), None
                )
                if stored is None:
                    raise ApiError(
                        "Indexer creation did not return a saved native identity."
                    )
                owned[entry["id"]] = {
                    "id": saved_id,
                    "definition": _definition(template),
                    "credential_digest": credential_digest,
                    "endpoint_scope": client._setup_ownership_scope,
                }
                write_ownership(
                    _ledger_path(ownership_directory, source + ".indexers", write=True),
                    owned,
                )
                # Secret fields may be masked by native readback; compare all other selected fields.
                actual = _comparison(stored)
                if any(
                    actual.get(key) != value
                    for key, value in after.items()
                    if not any(
                        word in key.lower()
                        for word in (
                            "password",
                            "apikey",
                            "api_key",
                            "token",
                            "cookie",
                            "passkey",
                            "secret",
                        )
                    )
                ):
                    raise ApiError(
                        "Indexer native readback did not match the proposed settings."
                    )
                existing, changed = stored, True
            elif different:
                return {
                    "execution": "waiting_input",
                    "verification": "not_checked",
                    "retryable": False,
                    "reason": "Apply the reviewed indexer settings before verification.",
                }
            _test(client, prefix, existing or desired)
            outcomes.append(
                {
                    "id": entry["id"],
                    "configuration_readback": "passed",
                    "connection_test": "passed",
                }
            )
        if operation in {"preview", "inspect"}:
            members = {row["id"]: row.get("preview_id") for row in outcomes}
            path = _ledger_path(
                ownership_directory,
                source + ".indexer-batch-previews",
                write=operation == "preview",
            )
            records = read_ownership(path)
            batch = records.get(source + "::" + client._setup_ownership_scope) or {}
            if operation == "preview" and batch.get("members") != members:
                batch = {"id": secrets.token_urlsafe(32), "members": members}
                records[source + "::" + client._setup_ownership_scope] = batch
                write_ownership(path, records)
            evidence = {
                "preview_id": (
                    batch.get("id") if batch.get("members") == members else None
                ),
                "requires_consent": any(
                    row.get("requires_consent") for row in outcomes
                ),
                "changes": [
                    {**change, "field": row["id"] + "." + change["field"]}
                    for row in outcomes
                    for change in row.get("changes") or []
                ],
            }
        else:
            evidence = outcomes[0] if len(outcomes) == 1 else {"indexers": outcomes}
        return {
            "execution": "applied" if changed else "already_present",
            "verification": (
                "not_checked" if operation in {"preview", "inspect"} else "passed"
            ),
            "reason": (
                "Indexer fields were inspected without changes."
                if operation in {"preview", "inspect"}
                else "Selected native indexers were saved, read back and connection-tested; other indexers and application policies were preserved."
            ),
            "retryable": False,
            "evidence": evidence,
        }
    except (
        InputNeeded,
        UnsupportedContract,
        ApiError,
        ValueError,
        OSError,
        TypeError,
        KeyError,
    ) as exc:
        return _failure(exc)


def inspect_indexers(action, config, payload, operation="inspect", **kwargs):
    return apply_indexers(
        action,
        config,
        payload,
        operation if operation in {"preview", "recheck", "verify"} else "inspect",
        **kwargs,
    )
