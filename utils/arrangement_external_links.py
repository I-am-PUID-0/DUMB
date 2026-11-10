"""First-class external targets for companion applications, without fake installs."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

from utils.arrangement_external import resolve_target, target_family
from utils.arrangement_adoption import preview
from utils.arrangement_connections import (
    _scope,
    _values,
    _client,
    _native,
    _request,
    _list,
    _ledger_path,
    _failure,
    InputNeeded,
    ApiError,
)
from utils.arrangement_http import ApiClient
from utils.integration_ownership import read_ownership, write_ownership
from utils.private_files import atomic_write_private_text


def apply_external_link(
    action,
    config,
    payload,
    operation="apply",
    *,
    client_factory=ApiClient,
    ownership_directory="/config/.dumb-setup/connections",
):
    target_id = action.get("target", "")
    source, family, _ = _scope(action)
    if action.get("action") != "arr_connection" or family not in {
        "prowlarr",
        "neutarr",
        "profilarr",
    }:
        return None
    try:
        external = target_id.startswith("external_arr.")
        if external:
            target = resolve_target(payload, target_id, client_factory=client_factory)
        else:
            _, url, key = _native(config, target_id)
            if not key:
                raise InputNeeded("Waiting for the selected Arr API key.")
            target = {
                "id": target_id,
                "url": url,
                "key": key,
                "family": target_family(payload, target_id),
            }

        from utils.arrangement_link_actions import selected_targets

        selected = selected_targets(source, config, payload)
        if selected is None:
            raise InputNeeded(
                "Choose exact Arr assignments before configuring this connection.",
                [source + "::arr_instances"],
            )
        if (
            not external
            and operation not in {"preview", "inspect"}
            and not _values(payload).get(source + "::adopt_existing")
            and source + "::sync_level" not in _values(payload)
            and not _values(payload).get(source + "::enable_integrations")
        ):
            return None
        retiring = target_id not in selected
        if family == "profilarr":
            from utils.arrangement_profiles import apply_profile_connection

            return apply_profile_connection(
                action,
                config,
                payload,
                operation,
                client_factory=client_factory,
                ownership_directory=ownership_directory,
            )
        values = _values(payload)
        ledger = _ledger_path(ownership_directory, source + ".external", write=False)
        records = read_ownership(ledger)
        source_cfg, source_url, source_key = _native(config, source)
        source_scope = hashlib.sha256(
            json.dumps(
                {
                    "url": source_url,
                    "key": values.get(source + "::api_key") or source_key,
                    "family": family,
                    "config_dir": source_cfg.get("config_dir"),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        target_scope = (
            target.get("ownership_scope")
            or hashlib.sha256(
                json.dumps(
                    {
                        "url": target["url"],
                        "key": target["key"],
                        "family": target["family"],
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        )
        old_record = records.get(target_id) or {}
        record = (
            old_record
            if old_record.get("source_scope") == source_scope
            and old_record.get("target_scope") == target_scope
            else {}
        )
        if family == "neutarr":
            settings, _, _ = _native(config, source)
            directory = (settings.get("env") or {}).get(
                "NEUTARR_CONFIG_DIR"
            ) or os.path.join(settings.get("config_dir") or "", "config")
            path = Path(directory) / (target["family"] + ".json")
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 2097152:
                raise InputNeeded(
                    "Waiting for the initialized native NeutArr configuration."
                )
            document = json.loads(path.read_text())
            native = document.get("instances") or []
            matches = [row for row in native if row.get("api_url") == target["url"]]
            if len(matches) > 1:
                raise InputNeeded(
                    "Several NeutArr entries match this external endpoint."
                )
            existing = matches[0] if matches else None
            placeholder = False
            if existing is None and not retiring:
                defaults = [
                    row
                    for row in native
                    if isinstance(row, dict)
                    and row.get("name") == "Default"
                    and not row.get("api_url")
                    and not row.get("api_key")
                ]
                if len(defaults) == 1:
                    existing, placeholder = defaults[0], True
            owned = bool(
                existing
                and record.get("name") == existing.get("name")
                and record.get("url") == existing.get("api_url")
            )
            if retiring:
                if not owned:
                    raise InputNeeded(
                        "The removed external connection has no matching protected ownership. Review it manually."
                    )
                desired = {**existing, "enabled": False}
            else:
                desired = {
                    **(existing or {}),
                    "name": (
                        ("DUMB " + target_id)
                        if placeholder
                        else (existing or {}).get("name") or "DUMB " + target_id
                    ),
                    "api_url": target["url"],
                    "api_key": target["key"],
                    "enabled": (
                        True
                        if values.get(source + "::enable_integrations") is True
                        else (existing or {}).get("enabled", False)
                    ),
                }
            before = existing or {}
        else:
            placeholder = False
            client, _ = _client(config, source, values, client_factory)
            native = _list(_request(client, "GET", "/api/v1/applications"))
            matches = [row for row in native if row.get("id") == record.get("id")]
            if not matches:
                matches = [
                    row
                    for row in native
                    if str(row.get("implementation") or "").lower() == target["family"]
                    and any(
                        f.get("name", "").lower() == "baseurl"
                        and str(f.get("value", "")).rstrip("/") == target["url"]
                        for f in row.get("fields") or []
                    )
                ]
            if len(matches) > 1:
                raise InputNeeded(
                    "Several Prowlarr entries match this external endpoint."
                )
            existing = matches[0] if matches else None
            owned = bool(existing and existing.get("id") == record.get("id"))
            if retiring:
                if not owned:
                    raise InputNeeded(
                        "The removed external connection has no matching protected ownership. Review it manually."
                    )
                desired = {**existing, "syncLevel": "disabled"}
            else:
                schemas = _list(_request(client, "GET", "/api/v1/applications/schema"))
                template = next(
                    (
                        r
                        for r in schemas
                        if str(r.get("implementation", "")).lower() == target["family"]
                    ),
                    None,
                )
                if template is None:
                    raise InputNeeded(
                        "This Prowlarr version has no application schema for the selected Arr family."
                    )
                desired = copy.deepcopy(existing or template)
                desired.pop("id", None)
                desired["name"] = (existing or {}).get("name") or "DUMB " + target_id
                if not existing:
                    desired["syncLevel"] = "disabled"
                    desired["tags"] = []
                if source + "::sync_level" in values:
                    level = values[source + "::sync_level"]
                    if level not in {"disabled", "addOnly", "fullSync"}:
                        raise InputNeeded(
                            "Choose a supported native Prowlarr synchronization policy."
                        )
                    desired["syncLevel"] = level
                _, source_url, _ = _native(config, source)
                wanted = {
                    "baseurl": target["url"],
                    "apikey": target["key"],
                    "prowlarrurl": source_url,
                }
                for field in desired.get("fields") or []:
                    name = str(field.get("name") or "").lower()
                    if name in wanted:
                        field["value"] = wanted[name]

            def public_comparison(row):
                return {
                    **{k: row.get(k) for k in ("name", "syncLevel", "tags")},
                    **{
                        f["name"]: f.get("value")
                        for f in row.get("fields") or []
                        if "name" in f
                    },
                }

            before = public_comparison(existing or {})
        after = desired if family == "neutarr" else public_comparison(desired)
        if family == "neutarr":
            before = existing or {}
        key_digest = hashlib.sha256(target["key"].encode()).hexdigest()
        if family == "prowlarr" and owned and record.get("key_digest") == key_digest:
            for key in before:
                if key.lower() == "apikey" and before[key] == "********":
                    before[key] = after.get(key)
        proposal = preview(
            _ledger_path(
                ownership_directory,
                source + ".external-previews",
                write=operation == "preview",
            ),
            source + "::" + target_id + "::" + source_scope + "::" + target_scope,
            before,
            after,
            managed=owned or not existing or placeholder,
            operation=operation,
            accepted_id=values.get(source + "::adoption_preview_id"),
            consent=values.get(source + "::adopt_existing", False),
        )
        different = before != after
        if operation in {"preview", "inspect"}:
            return {
                "execution": "waiting_input" if different else "already_present",
                "verification": "not_checked",
                "reason": "The external connection proposal was inspected without application writes.",
                "retryable": False,
                "evidence": proposal,
            }
        if different and operation in {"verify", "recheck"}:
            raise InputNeeded(
                "Apply the reviewed external connection before verification."
            )
        if different:
            if existing and not owned and not placeholder and not proposal["approved"]:
                return {
                    "execution": "waiting_input",
                    "verification": "not_checked",
                    "retryable": False,
                    "reason": "Preview and accept these exact native fields before adopting the existing connection.",
                    "missing_inputs": [
                        source + "::adopt_existing",
                        source + "::adoption_preview_id",
                    ],
                    "evidence": proposal,
                }
            if external and (
                (family == "prowlarr" and desired.get("syncLevel") != "disabled")
                or (family == "neutarr" and desired.get("enabled") is True)
            ):
                resolve_target(
                    payload,
                    target_id,
                    client_factory=client_factory,
                    require_write=True,
                )
            if family == "neutarr":
                if existing:
                    native[native.index(existing)] = desired
                else:
                    native.append(desired)
                document["instances"] = native
                atomic_write_private_text(path, json.dumps(document, indent=2) + "\n")
            else:
                if existing:
                    desired["id"] = existing["id"]
                saved = _request(
                    client,
                    "PUT" if existing else "POST",
                    "/api/v1/applications"
                    + ("/" + str(existing["id"]) if existing else ""),
                    body=desired,
                )
                saved_id = existing["id"] if existing else (saved or {}).get("id")
                if not isinstance(saved_id, int):
                    raise ApiError(
                        "The native application did not return a saved identity."
                    )
                record["id"] = saved_id
            record.update(
                name=desired["name"],
                url=target["url"],
                key_digest=key_digest,
                source_scope=source_scope,
                target_scope=target_scope,
            )
            records[target_id] = record
            write_ownership(
                _ledger_path(ownership_directory, source + ".external", write=True),
                records,
            )
        if family == "neutarr":
            rows = json.loads(path.read_text()).get("instances") or []
            readback = next(
                (
                    row
                    for row in rows
                    if row.get("name") == desired["name"]
                    and row.get("api_url") == target["url"]
                ),
                None,
            )
            passed = readback is not None and all(
                readback.get(k) == v for k, v in desired.items()
            )
            active = bool((readback or {}).get("enabled"))
        else:
            rows = _list(_request(client, "GET", "/api/v1/applications"))
            readback = next(
                (row for row in rows if row.get("id") == record.get("id")), existing
            )
            actual = public_comparison(readback or {})
            for key in actual:
                if key.lower() == "apikey" and actual[key] == "********":
                    actual[key] = after.get(key)
            passed = bool(readback and actual == after)
            active = (readback or {}).get("syncLevel") != "disabled"
            if passed and not retiring:
                findings = _request(
                    client, "POST", "/api/v1/applications/test", body=readback
                )
                if findings not in (None, [], {}, "", "{}"):
                    raise InputNeeded(
                        "Prowlarr's native external connection test reported findings."
                    )
        if not passed:
            raise ApiError("External connection readback did not match the proposal.")
        return {
            "execution": "applied" if different else "already_present",
            "verification": (
                "passed"
                if retiring or (family == "prowlarr" and active)
                else "unavailable"
            ),
            "retryable": False,
            "reason": (
                "The external companion connection was read back as inactive."
                if retiring
                else (
                    "External connection fields were read back. Review and enable the native synchronization or search policy when ready."
                    if not active
                    else "The external companion connection was read back and tested."
                )
            ),
            "evidence": {
                "target": target_id,
                "level": "configuration_readback",
                "retired": retiring,
                "enabled": active,
            },
        }
    except (InputNeeded, ApiError, ValueError, OSError, TypeError, KeyError) as exc:
        return _failure(exc)
