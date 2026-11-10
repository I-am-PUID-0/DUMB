"""Version-aware Profilarr profile selection through native authenticated APIs.

v1 uses the Flask API; v2 uses its documented public inventory and the versioned
SvelteKit form contract for selection. No policy is edited in native databases.
"""

from __future__ import annotations

import copy
import json

from utils.arrangement_http import ApiClient, ApiError, validate_endpoint
from utils.arrangement_connections import (
    _scope,
    _values,
    _field,
    _native,
    _request,
    _list,
    _ledger_path,
    _failure,
    InputNeeded,
    UnsupportedContract,
    _target,
)
from utils.arrangement_external import resolve_target
from utils.arrangement_adoption import preview
from utils.integration_ownership import read_ownership, write_ownership, fingerprint


def schema_fields(resource, config, payload):
    source, family, _ = _scope(resource)
    if family != "profilarr":
        return []
    fields = [
        _field(source, "api_key", "Profilarr API key", "password", secret=True),
        _field(source, "username", "Profilarr administrator username"),
        _field(
            source,
            "password",
            "Profilarr administrator password",
            "password",
            secret=True,
        ),
        _field(
            source,
            "session",
            "Protected Profilarr session",
            "password",
            secret=True,
            internal=True,
        ),
        _field(source, "database_url", "Profile database Git repository URL"),
        _field(source, "database_name", "Profile database display name"),
        _field(source, "database_branch", "Profile database branch"),
        _field(
            source,
            "database_token",
            "Private database GitHub token",
            "password",
            secret=True,
            credential_destination_key=source + "::database_url",
        ),
        _field(source, "git_user_name", "Git identity name for a private database"),
        _field(source, "git_user_email", "Git identity email for a private database"),
        _field(
            source,
            "authorize_database",
            "Authorize linking this profile database",
            "boolean",
            default=False,
        ),
        _field(
            source,
            "sync_now",
            "Synchronize selected profiles now",
            "boolean",
            default=False,
        ),
        _field(
            source,
            "adopt_existing",
            "Accept the exact profile configuration preview",
            "boolean",
            default=False,
        ),
        _field(source, "adoption_preview_id", "Accepted adoption preview"),
    ]
    for target in _values(payload).get(source + "::arr_instances") or []:
        fields.append(
            _field(
                source,
                target + "::profiles",
                target + ": profiles to synchronize",
                "multiselect",
                options=[],
                refresh_on_change=True,
            )
        )
    return fields


def _unflatten(values):
    if not isinstance(values, list) or len(values) > 100000:
        raise UnsupportedContract(
            "The installed Profilarr page-data contract is unsupported."
        )
    active, cache = set(), {}

    def hydrate(index, depth=0):
        if index == -1:
            return None
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < len(values)
            or depth > 32
        ):
            raise UnsupportedContract(
                "Profilarr returned invalid page-data references."
            )
        if index in cache:
            return cache[index]
        if index in active:
            raise UnsupportedContract("Profilarr returned cyclic page data.")
        active.add(index)
        value = values[index]
        if isinstance(value, dict):
            result = {key: hydrate(item, depth + 1) for key, item in value.items()}
        elif isinstance(value, list):
            if value and isinstance(value[0], str):
                if value[0] in {"Date", "BigInt"}:
                    result = value[1] if len(value) > 1 else None
                else:
                    raise UnsupportedContract(
                        "Profilarr returned an unsupported page-data type."
                    )
            else:
                result = [hydrate(item, depth + 1) for item in value]
        else:
            result = value
        active.remove(index)
        cache[index] = result
        return result

    return hydrate(0)


def _web_request(client, method, path, **kwargs):
    # Profilarr's web routes are session-only. A scoped API key must never be
    # forwarded to a native form, even when the same client holds a session.
    old = getattr(client, "headers", {})
    client.headers = {
        key: value for key, value in old.items() if key.lower() != "x-api-key"
    }
    try:
        return client.request(method, path, **kwargs)
    finally:
        client.headers = old


def _page(client, path):
    response = _web_request(client, "GET", path + "/__data.json")
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("nodes"), list):
        raise UnsupportedContract(
            "The installed Profilarr page-data API is unsupported."
        )
    result = {}
    for node in body["nodes"]:
        if isinstance(node, dict) and node.get("type") == "data":
            data = _unflatten(node.get("data"))
            if not isinstance(data, dict):
                raise UnsupportedContract("Profilarr returned unsupported page data.")
            result.update(data)
    return result


def _form(client, path, name, data):
    response = _web_request(
        client,
        "POST",
        path + ("?/" + name if name else ""),
        data=data,
        headers={
            "x-sveltekit-action": "true",
            "Origin": getattr(client, "headers", {}).get("Origin") or client.base_url,
            "Accept": "application/json",
        },
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("type") not in {"success", "redirect"}:
        raise InputNeeded(
            "Profilarr rejected the native form action. Review its authorization and selected values."
        )
    if body.get("type") == "success":
        value = json.loads(body.get("data") or "[]")
        if isinstance(value, list):
            decoded = _unflatten(value)
            if isinstance(decoded, dict) and decoded.get("error"):
                raise InputNeeded("Profilarr rejected the native form action.")
    return body


def _connect(source, config, payload, factory):
    cfg, url, native_key = _native(config, source)
    values = _values(payload)
    key = values.get(source + "::api_key") or native_key
    client = factory(url, headers={"X-Api-Key": key} if key else {}, managed=True)
    if (cfg.get("env") or {}).get("ORIGIN"):
        client.headers = {
            **getattr(client, "headers", {}),
            "Origin": cfg["env"]["ORIGIN"],
        }
    session = values.get(source + "::session")
    if session:
        client.cookies = {"session": session}
    response = client.request("GET", "/api/v1/arr")
    if response.status_code in {401, 403}:
        username, password = values.get(source + "::username"), values.get(
            source + "::password"
        )
        if username and password:
            _form(
                client, "/auth/login", "", {"username": username, "password": password}
            )
            response = client.request("GET", "/api/v1/arr")
        else:
            raise InputNeeded(
                "Provide Profilarr authorization. Its profile-selection form requires a native administrator session.",
                [source + "::username", source + "::password"],
            )
    if response.status_code not in {404, 405}:
        response.raise_for_status()
        try:
            native = response.json()
        except ApiError:
            native = None
        if isinstance(native, list):
            if (
                not getattr(client, "cookies", {}).get("session")
                and values.get(source + "::username")
                and values.get(source + "::password")
            ):
                _form(
                    client,
                    "/auth/login",
                    "",
                    {
                        "username": values[source + "::username"],
                        "password": values[source + "::password"],
                    },
                )
            client._setup_ownership_scope = fingerprint(
                {
                    "family": "profilarr",
                    "url": url,
                    "auth": key or getattr(client, "cookies", {}).get("session"),
                }
            )
            return client, 2, _list(native)
    # v1's routes are stable JSON APIs, not SvelteKit pages.
    native = _request(client, "GET", "/api/arr/config")
    if not isinstance(native, dict) or native.get("success") is not True:
        raise UnsupportedContract(
            "The installed Profilarr version has no supported Arr inventory API."
        )
    client._setup_ownership_scope = fingerprint(
        {
            "family": "profilarr",
            "url": url,
            "auth": key or getattr(client, "cookies", {}).get("session"),
        }
    )
    return client, 1, _list(native.get("data"))


def _inventory(client, version, row):
    if version == 1:
        profiles = _list(_request(client, "GET", "/api/data/profile"))
        return {
            "profiles": [
                {
                    "value": str(
                        (p.get("content") or {}).get("name")
                        or p.get("name")
                        or p.get("file_name", "").removesuffix(".yml")
                    ),
                    "label": str(
                        (p.get("content") or {}).get("name")
                        or p.get("name")
                        or p.get("file_name", "")
                    ),
                }
                for p in profiles
            ],
            "native": row,
        }
    data = _page(client, "/arr/" + str(row["id"]) + "/sync")
    if not isinstance(data.get("syncData"), dict) or not isinstance(
        data.get("databases"), list
    ):
        raise UnsupportedContract(
            "The installed Profilarr profile-selection contract is unsupported."
        )
    choices = []
    for database in data["databases"]:
        for profile in database.get("qualityProfiles") or []:
            if isinstance(profile.get("name"), str):
                choices.append(
                    {
                        "value": str(database["id"]) + ":" + profile["name"],
                        "label": str(database.get("name") or database["id"])
                        + " — "
                        + profile["name"],
                    }
                )
    return {"profiles": choices, "native": data}


def _matching(native, target):
    return [
        row
        for row in native
        if row.get("type") == target["family"]
        and str(row.get("url") or row.get("arrServer") or "").rstrip("/")
        == target["url"].rstrip("/")
    ]


def discover_fields(resource, config, payload, *, client_factory=ApiClient):
    fields = schema_fields(resource, config, payload)
    if not fields:
        return []
    source, _, _ = _scope(resource)
    try:
        client, version, native = _connect(source, config, payload, client_factory)
        for target_id in _values(payload).get(source + "::arr_instances") or []:
            target = _target(config, target_id, client_factory, payload)
            matches = _matching(native, target)
            if len(matches) != 1:
                continue
            inventory = _inventory(client, version, matches[0])
            for field in fields:
                if field["key"] == source + "::" + target_id + "::profiles":
                    field["options"] = inventory["profiles"]
    except (InputNeeded, ApiError, ValueError, OSError, TypeError, KeyError):
        pass
    return fields


def _database(client, version, source, values, operation):
    url = values.get(source + "::database_url")
    if not url:
        return
    # Native applications clone this URL, so disallow non-HTTP transports and
    # private destinations before forwarding it. No embedded Git credentials.
    validate_endpoint(url)
    if version == 1:
        settings = _request(client, "GET", "/api/settings")
        existing = settings.get("gitRepo") if isinstance(settings, dict) else None
        if existing == url:
            return
        if existing:
            raise InputNeeded(
                "Profilarr v1 already uses another database. Preserve it and switch repositories explicitly in Profilarr."
            )
        if operation not in {"apply", "retry", "start"}:
            return
        if not values.get(source + "::authorize_database", False):
            raise InputNeeded(
                "Authorize linking the selected profile database.",
                [source + "::authorize_database"],
            )
        _request(client, "POST", "/api/git/clone", body={"gitRepo": url})
        return
    databases = _list(_request(client, "GET", "/api/v1/databases"))
    if any(
        row.get("repository_url") == url or row.get("repositoryUrl") == url
        for row in databases
    ):
        return
    if operation not in {"apply", "retry", "start"}:
        return
    if not values.get(source + "::authorize_database", False):
        raise InputNeeded(
            "Authorize linking the selected profile database.",
            [source + "::authorize_database"],
        )
    body = {
        "name": values.get(source + "::database_name") or "DUMB profiles",
        "repository_url": url,
        "auto_pull": False,
        "sync_strategy": 0,
    }
    for field, native in (
        ("database_branch", "branch"),
        ("database_token", "personal_access_token"),
        ("git_user_name", "git_user_name"),
        ("git_user_email", "git_user_email"),
    ):
        if values.get(source + "::" + field):
            body[native] = values[source + "::" + field]
    if body.get("personal_access_token") and not (
        body.get("git_user_name") and body.get("git_user_email")
    ):
        raise InputNeeded(
            "Provide your configured Git identity when authorizing a private database.",
            [source + "::git_user_name", source + "::git_user_email"],
        )
    _request(client, "POST", "/api/v1/databases", body=body)


def apply_profiles(
    action,
    config,
    payload,
    operation="apply",
    *,
    client_factory=ApiClient,
    ownership_directory="/config/.dumb-setup/connections",
):
    source, family, _ = _scope(action)
    if family != "profilarr" or action.get("action") not in {"profiles", "policy"}:
        return None
    client = None
    try:
        values = _values(payload)
        client, version, native = _connect(source, config, payload, client_factory)
        _database(client, version, source, values, operation)
        selected = values.get(source + "::arr_instances") or []
        if not selected:
            raise InputNeeded(
                "Choose Arr assignments before selecting profiles.",
                [source + "::arr_instances"],
            )
        prepared, before_all, after_all = [], {}, {}
        sync_path = _ledger_path(
            ownership_directory, source + ".profile-sync", write=False
        )
        receipts = read_ownership(sync_path)
        for target_id in selected:
            target = _target(config, target_id, client_factory, payload)
            matches = _matching(native, target)
            if len(matches) != 1:
                raise InputNeeded(
                    "Configure this exact Profilarr Arr connection before selecting its profiles."
                )
            row = matches[0]
            inventory = _inventory(client, version, row)
            chosen = values.get(source + "::" + target_id + "::profiles") or []
            allowed = {option["value"] for option in inventory["profiles"]}
            if (
                not isinstance(chosen, list)
                or not chosen
                or not set(chosen).issubset(allowed)
            ):
                raise InputNeeded(
                    "Choose available database profiles for each selected Arr instance.",
                    [source + "::" + target_id + "::profiles"],
                )
            if version == 2:
                sync = inventory["native"]["syncData"].get("qualityProfiles") or {}
                before = copy.deepcopy(sync.get("selections") or [])
                additions = [
                    {
                        "databaseId": int(value.split(":", 1)[0]),
                        "profileName": value.split(":", 1)[1],
                    }
                    for value in chosen
                ]
                after = before + [p for p in additions if p not in before]
            else:
                sync = row.get("data_to_sync") or {}
                before = copy.deepcopy(sync.get("profiles") or [])
                after = list(dict.fromkeys(before + chosen))
            target_profiles = _list(
                _request(target["client"], "GET", "/api/v3/qualityprofile")
            )
            signature = fingerprint(
                {
                    "target": target_id,
                    "after": after,
                    "database": values.get(source + "::database_url"),
                    "source_scope": client._setup_ownership_scope,
                    "target_scope": target["ownership_scope"],
                }
            )
            queued = (receipts.get(target_id) or {}).get("signature") == signature
            do_sync = values.get(source + "::sync_now", False) and not queued
            before_all[target_id + "::selection"] = before
            after_all[target_id + "::selection"] = after
            # Snapshot actual native profiles too: drift between preview and apply
            # invalidates consent, including when a selected name already exists.
            before_all[target_id + "::native_profiles"] = target_profiles
            after_all[target_id + "::native_profiles"] = target_profiles
            if do_sync:
                after_all[target_id + "::sync_now"] = True
            prepared.append(
                (target_id, target, row, sync, before, after, do_sync, signature)
            )
        proposal = preview(
            _ledger_path(
                ownership_directory,
                source + ".profile-previews",
                write=operation == "preview",
            ),
            source
            + "::profiles::"
            + client._setup_ownership_scope
            + "::"
            + fingerprint([item[1]["ownership_scope"] for item in prepared]),
            before_all,
            after_all,
            managed=False,
            operation=operation,
            accepted_id=values.get(source + "::adoption_preview_id"),
            consent=values.get(source + "::adopt_existing", False),
        )
        if operation in {"inspect", "preview"}:
            result = {
                "execution": (
                    "waiting_input" if proposal["changes"] else "already_present"
                ),
                "verification": "not_checked",
                "reason": "Profilarr database/profile choices and current Arr profiles were inspected without policy changes.",
                "retryable": False,
                "evidence": proposal,
            }
            return _handoff(result, client, source)
        changing = any(
            before != after or do_sync
            for _, _, _, _, before, after, do_sync, _ in prepared
        )
        if changing and operation in {"verify", "recheck"}:
            raise InputNeeded(
                "Apply the accepted profile proposal before verification."
            )
        if changing and not proposal["approved"]:
            return _handoff(
                {
                    "execution": "waiting_input",
                    "verification": "not_checked",
                    "retryable": False,
                    "reason": "Preview and accept the selected profile configuration and synchronization before writes.",
                    "evidence": proposal,
                    "missing_inputs": [
                        source + "::adopt_existing",
                        source + "::adoption_preview_id",
                    ],
                },
                client,
                source,
            )
        results = []
        for target_id, target, row, sync, before, after, do_sync, signature in prepared:
            if (before != after or do_sync) and target.get("external"):
                resolve_target(
                    payload,
                    target_id,
                    client_factory=client_factory,
                    require_write=True,
                )
            if before != after:
                if version == 2:
                    cfg = sync.get("config") or {}
                    _form(
                        client,
                        "/arr/" + str(row["id"]) + "/sync",
                        "saveQualityProfiles",
                        {
                            "selections": json.dumps(after),
                            "priorities": "[]",
                            "trigger": cfg.get("trigger") or "manual",
                            "cron": cfg.get("cron") or "",
                        },
                    )
                else:
                    _request(
                        client,
                        "PUT",
                        "/api/arr/config/" + str(row["id"]),
                        body={**row, "data_to_sync": {**sync, "profiles": after}},
                    )
            baseline = (receipts.get(target_id) or {}).get("baseline")
            if do_sync:
                if version == 2:
                    baseline = _sync_state(client, row["id"]).get("lastSyncedAt")
                    _form(
                        client,
                        "/arr/" + str(row["id"]) + "/sync",
                        "syncQualityProfiles",
                        {},
                    )
                else:
                    _request(
                        client,
                        "POST",
                        "/api/import/profile",
                        body={"arrId": row["id"], "all": False, "profileNames": after},
                    )
                receipts[target_id] = {
                    "signature": signature,
                    "queued": True,
                    "baseline": baseline,
                }
                write_ownership(
                    _ledger_path(
                        ownership_directory, source + ".profile-sync", write=True
                    ),
                    receipts,
                )
            refreshed = _inventory(
                client,
                version,
                (
                    row
                    if version == 2
                    else next(
                        r
                        for r in _request(client, "GET", "/api/arr/config")["data"]
                        if r["id"] == row["id"]
                    )
                ),
            )
            actual = (
                (
                    (refreshed["native"].get("syncData") or {}).get("qualityProfiles")
                    or {}
                ).get("selections")
                if version == 2
                else (refreshed["native"].get("data_to_sync") or {}).get("profiles")
            )
            if actual != after:
                raise ApiError(
                    "Profilarr profile selections did not match native readback."
                )
            desired_names = {
                p["profileName"] if isinstance(p, dict) else p for p in after
            }
            readback = _list(
                _request(target["client"], "GET", "/api/v3/qualityprofile")
            )
            complete = desired_names.issubset({r.get("name") for r in readback})
            if values.get(source + "::sync_now", False) and version == 2:
                native_status = _sync_state(client, row["id"])
                if native_status.get("status") == "failed":
                    return _handoff(
                        {
                            "execution": "failed",
                            "verification": "failed",
                            "retryable": False,
                            "reason": "Profilarr reported a failed profile synchronization. Review its native sync findings before retrying.",
                            "evidence": {
                                "level": "native_sync_status",
                                "target": target_id,
                            },
                        },
                        client,
                        source,
                    )
                complete = (
                    complete
                    and native_status.get("status") == "idle"
                    and bool(native_status.get("lastSyncedAt"))
                    and native_status.get("lastSyncedAt") != baseline
                )
            if values.get(source + "::sync_now", False) and not complete:
                return _handoff(
                    {
                        "execution": "waiting_dependency",
                        "verification": "not_checked",
                        "retryable": True,
                        "reason": "Profilarr accepted synchronization. Waiting for selected profiles to appear in Arr.",
                        "evidence": {
                            "level": "profile_selection_readback",
                            "target": target_id,
                        },
                    },
                    client,
                    source,
                )
            results.append(
                {
                    "target": target_id,
                    "selection_readback": "passed",
                    "arr_profile_readback": "passed" if complete else "not_requested",
                }
            )
        return _handoff(
            {
                "execution": "applied" if changing else "already_present",
                "verification": "passed",
                "reason": "Selected Profilarr profile configuration was read back; unrelated profiles, schedules and media policies were preserved.",
                "retryable": False,
                "evidence": (
                    results[0] if len(results) == 1 else {"connections": results}
                ),
            },
            client,
            source,
        )
    except (
        InputNeeded,
        UnsupportedContract,
        ApiError,
        ValueError,
        OSError,
        TypeError,
        KeyError,
    ) as exc:
        return _handoff(_failure(exc), client, source)


def _sync_state(client, identifier):
    status = _request(client, "GET", "/api/v1/status")
    if not isinstance(status, dict) or not isinstance(status.get("arr"), list):
        raise UnsupportedContract(
            "Profilarr does not expose a supported native synchronization status."
        )
    row = next((item for item in status["arr"] if item.get("id") == identifier), None)
    section = ((row or {}).get("sync") or {}).get("qualityProfiles")
    if not isinstance(section, dict) or section.get("status") not in {
        "idle",
        "pending",
        "in_progress",
        "failed",
    }:
        raise UnsupportedContract(
            "Profilarr's native profile synchronization status is unsupported."
        )
    return section


def _handoff(result, client, source):
    session = getattr(client, "cookies", {}).get("session") if client else None
    if session:
        result["_private_values"] = {source + "::session": session}
        result["_clear_inputs"] = [source + "::password"]
        result["_clear_on_handoff"] = True
    return result


def inspect_profiles(action, config, payload, operation="inspect", **kwargs):
    return apply_profiles(
        action,
        config,
        payload,
        operation if operation in {"preview", "recheck", "verify"} else "inspect",
        **kwargs,
    )


def apply_profile_connection(
    action,
    config,
    payload,
    operation="apply",
    *,
    client_factory=ApiClient,
    ownership_directory="/config/.dumb-setup/connections",
):
    """Register an external Arr through native APIs, preserving existing policies."""
    source, family, _ = _scope(action)
    if family != "profilarr":
        return None
    try:
        values = _values(payload)
        target = resolve_target(
            payload, action["target"], client_factory=client_factory
        )
        client, version, native = _connect(source, config, payload, client_factory)
        matches = _matching(native, target)
        if len(matches) > 1:
            raise InputNeeded("Several Profilarr entries match this external endpoint.")
        if matches:
            return {
                "execution": "already_present",
                "verification": "unavailable",
                "retryable": False,
                "reason": "The exact external Profilarr connection is present. Profile and synchronization choices remain separate.",
                "evidence": {"level": "configuration_readback", "target": target["id"]},
            }
        if operation in {"inspect", "preview", "recheck", "verify"}:
            raise InputNeeded(
                "Apply this external Profilarr connection before verification."
            )
        if version == 1:
            _request(
                client,
                "POST",
                "/api/arr/config",
                body={
                    "name": "DUMB " + target["id"],
                    "type": target["family"],
                    "arrServer": target["url"],
                    "apiKey": target["key"],
                    "tags": [],
                    "data_to_sync": {},
                    "sync_method": "manual",
                    "sync_interval": 0,
                    "import_as_unique": False,
                },
            )
        else:
            # Default-delay provisioning is a separate native preference; creating
            # an external instance can write it. Require external write consent.
            resolve_target(
                payload, target["id"], client_factory=client_factory, require_write=True
            )
            _form(
                client,
                "/arr/new",
                "",
                {
                    "name": "DUMB " + target["id"],
                    "type": target["family"],
                    "url": target["url"],
                    "api_key": target["key"],
                    "tags": "[]",
                },
            )
        _, _, readback = _connect(source, config, payload, client_factory)
        if len(_matching(readback, target)) != 1:
            raise ApiError(
                "Profilarr external registration was not returned by native readback."
            )
        return {
            "execution": "applied",
            "verification": "unavailable",
            "retryable": False,
            "reason": "The external Profilarr connection was created and read back. No profile synchronization was enabled.",
            "evidence": {"level": "configuration_readback", "target": target["id"]},
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
