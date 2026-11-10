"""Scoped setup dispatch for existing owned Arr-companion reconcilers."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re

from utils.arrangement_http import ApiClient, ApiError
from utils.arrangement_external import target_options
from utils.arrangement_connections import (
    _native,
    _request,
    _values,
    _scope,
    InputNeeded,
    UnsupportedContract,
)

HELPERS = {"prowlarr", "neutarr", "profilarr"}


def schema_fields(resource, config, payload):
    source, family, _ = _scope(resource)
    if family not in HELPERS:
        return []
    allowed = (
        ("radarr", "sonarr")
        if family == "profilarr"
        else ("radarr", "sonarr", "lidarr", "whisparr")
    )
    options = [
        {"value": f"{key}.{name}", "label": f"{key.capitalize()} — {name}"}
        for key in allowed
        for name, entry in ((config.get(key) or {}).get("instances") or {}).items()
        if isinstance(entry, dict) and entry.get("enabled")
    ]
    options.extend(target_options(payload, allowed))
    policy_fields = []
    if family == "prowlarr":
        policy_fields.append(
            {
                "key": source + "::sync_level",
                "resource_id": source,
                "type": "select",
                "label": "Prowlarr synchronization policy",
                "options": [
                    {"value": value, "label": label}
                    for value, label in (
                        ("disabled", "Disabled"),
                        ("addOnly", "Add indexers only"),
                        ("fullSync", "Full synchronization"),
                    )
                ],
                "description": "An explicit policy choice can write indexer settings into selected Arr servers. Leaving it unset preserves existing policy; new external links start disabled.",
            }
        )
    if family == "neutarr":
        policy_fields.append(
            {
                "key": source + "::enable_integrations",
                "resource_id": source,
                "type": "boolean",
                "default": False,
                "label": "Authorize NeutArr searches using these connections' native schedules and limits",
                "description": "Existing disabled connections stay disabled unless you explicitly choose this activation. Search schedules and limits remain as configured in NeutArr.",
            }
        )
    return [
        *policy_fields,
        {
            "key": source + "::adopt_existing",
            "resource_id": source,
            "type": "boolean",
            "label": "Adopt matching native connection fields",
            "default": False,
        },
        {
            "key": source + "::adoption_preview_id",
            "resource_id": source,
            "type": "string",
            "label": "Accepted adoption preview",
        },
        {
            "key": f"{source}::arr_instances",
            "resource_id": source,
            "type": "multiselect",
            "label": "Connect these Arr instances",
            "options": options,
            "description": "Exact instance assignments. New NeutArr connections remain disabled until you review search policy.",
        },
    ]


def _resource(config, source):
    family, _, instance = source.partition(".")
    return ((config.get(family) or {}).get("instances") or {}).get(instance) or {}


def _selected(action, config, payload):
    source, family, instance = _scope(action)
    values = _values(payload)
    selected = values.get(
        f"{source}::arr_instances", _resource(config, source).get("arr_instances")
    )
    if not isinstance(selected, list):
        raise InputNeeded(
            "Choose the exact Arr instances to connect.", [f"{source}::arr_instances"]
        )
    if len(selected) > 50 or any(not isinstance(item, str) for item in selected):
        raise InputNeeded("Choose a bounded list of canonical Arr instance names.")
    allowed = {
        option["value"]
        for field in schema_fields(action, config, payload)
        for option in field.get("options") or []
    }
    if set(selected) - allowed:
        raise InputNeeded(
            "An assigned Arr instance is disabled, missing or unsupported by this companion."
        )
    return source, family, instance, selected


def _result(
    execution, reason, *, verification="not_checked", retryable=False, evidence=()
):
    return {
        "execution": execution,
        "verification": verification,
        "reason": reason,
        "retryable": retryable,
        "evidence": list(evidence),
    }


def _target(config, identifier):
    family = identifier.split(".", 1)[0]
    settings, url, key = _native(config, identifier)
    if not key:
        raise InputNeeded("Waiting for the selected Arr API key.")
    return settings, url, key, "v1" if family == "lidarr" else "v3"


def _inspect(action, config, payload, operation, client_factory, target_ids=None):
    source, family, instance, selected = _selected(action, config, payload)
    if target_ids is not None:
        selected = [identifier for identifier in selected if identifier in target_ids]
    if not selected:
        return _result(
            "already_present",
            "No Arr connections are selected for this companion.",
            verification="unavailable",
        )
    cfg = _resource(config, source)
    outcomes = []
    if family == "neutarr":
        root = (cfg.get("env") or {}).get("NEUTARR_CONFIG_DIR") or os.path.join(
            cfg.get("config_dir") or f"/neutarr/{instance.lower()}", "config"
        )
        for identifier in selected:
            target_family, _, target_key = identifier.partition(".")
            target_cfg, target_url, target_key_value, api = _target(config, identifier)
            path = Path(root) / f"{target_family}.json"
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                return _result(
                    "waiting_dependency",
                    "Waiting for NeutArr's per-application configuration.",
                    retryable=True,
                )
            document = json.loads(path.read_text())
            matches = [
                entry
                for entry in document.get("instances", [])
                if isinstance(entry, dict)
                and entry.get("api_url") == target_url
                and entry.get("api_key") == target_key_value
            ]
            if len(matches) != 1:
                return _result(
                    "waiting_input",
                    "The selected NeutArr connection is missing or ambiguous.",
                )
            entry = matches[0]
            outcomes.append(
                {
                    "target": identifier,
                    "configuration_readback": "passed",
                    "enabled": bool(entry.get("enabled", True)),
                }
            )
            if not entry.get("enabled", True):
                return _result(
                    "waiting_input",
                    "Connection fields are configured. Review search schedules and explicitly enable this connection in NeutArr.",
                    evidence=outcomes,
                )
        return _result(
            "already_present",
            "NeutArr connection fields were read back. Use NeutArr's authenticated Test Connection control to verify them; DUMB has not independently verified the consumer connection.",
            verification="unavailable",
            evidence=outcomes,
        )
    settings, url, key = _native(config, source)
    key = _values(payload).get(f"{source}::api_key") or key
    if not key:
        raise InputNeeded(
            "Provide an authorized application API key for connection readback.",
            [f"{source}::api_key"],
        )
    client = client_factory(url, headers={"X-Api-Key": key}, managed=True)
    route = "/api/v1/applications" if family == "prowlarr" else "/api/v1/arr"
    if family == "prowlarr":
        response = client.request("GET", route)
        if response.status_code in {404, 405}:
            route = "/api/v3/applications"
            response = client.request("GET", route)
        response.raise_for_status()
        rows = response.json()
    else:
        rows = _request(client, "GET", route)
    if not isinstance(rows, list):
        raise UnsupportedContract(
            "The installed companion API has an unsupported connection-list shape."
        )
    for identifier in selected:
        target_cfg, target_url, target_key, api = _target(config, identifier)
        arr_family = identifier.split(".", 1)[0]
        if family == "prowlarr":
            matches = [
                entry
                for entry in rows
                if isinstance(entry, dict)
                and str(entry.get("implementation") or "").lower() == arr_family
                and any(
                    str(field.get("name") or "").lower() == "baseurl"
                    and str(field.get("value") or "").rstrip("/")
                    == target_url.rstrip("/")
                    for field in entry.get("fields") or []
                )
            ]
        else:
            matches = [
                entry
                for entry in rows
                if isinstance(entry, dict)
                and entry.get("type") == arr_family
                and str(entry.get("url") or "").rstrip("/") == target_url.rstrip("/")
            ]
        if len(matches) != 1:
            return _result(
                "waiting_input",
                "A selected companion connection is missing or ambiguous.",
                evidence=outcomes,
            )
        entry = matches[0]
        if not entry.get("enable" if family == "prowlarr" else "enabled", True) or (
            family == "prowlarr"
            and str(entry.get("syncLevel", "")).lower() == "disabled"
        ):
            return _result(
                "waiting_input",
                "The selected companion connection is intentionally disabled. Enable it in the application when ready.",
                evidence=outcomes,
            )
        outcomes.append(
            {
                "target": identifier,
                "configuration_readback": "passed",
                "connection_id": entry.get("id"),
            }
        )
        if family == "prowlarr" and operation in {
            "recheck",
            "verify",
            "apply",
            "retry",
        }:
            response = client.request("POST", route + "/test", json=entry)
            if response.status_code in {404, 405}:
                return _result(
                    "already_present",
                    "Application settings were read back; this Prowlarr version does not expose a supported connection-test endpoint.",
                    verification="unavailable",
                    evidence=outcomes,
                )
            response.raise_for_status()
            if response.text.strip():
                findings = response.json()
                if findings not in (None, [], {}, "", "{}"):
                    return _result(
                        "waiting_input",
                        "Prowlarr reported application connection-test findings. Review the selected endpoint and credentials.",
                        verification="failed",
                        evidence=outcomes,
                    )
            outcomes[-1]["connection_test"] = "passed"
    verified = family == "prowlarr" and operation in {
        "recheck",
        "verify",
        "apply",
        "retry",
    }
    return _result(
        "already_present",
        "Selected companion connection configuration was read back. Profile selection and synchronization policy remain separate.",
        verification="passed" if verified else "unavailable",
        evidence=outcomes,
    )


def inspect_helper(
    action,
    config,
    payload,
    operation="inspect",
    *,
    client_factory=ApiClient,
    manager=None,
    target_ids=None,
):
    _, family, _ = _scope(action)
    if family not in HELPERS or action.get("action") != "arr_connections":
        return None
    try:
        return _inspect(action, config, payload, operation, client_factory, target_ids)
    except UnsupportedContract as exc:
        return {
            **_result("waiting_input", str(exc), verification="unavailable"),
            "automation": "unknown",
        }
    except InputNeeded as exc:
        return {**_result("waiting_input", str(exc)), "missing_inputs": exc.fields}
    except ApiError as exc:
        if exc.status_code in {401, 403}:
            return _result(
                "waiting_input",
                "The companion rejected application authorization. Review its generated application key.",
            )
        if exc.status_code in {404, 405}:
            return {
                **_result(
                    "waiting_input",
                    "The installed companion API does not expose a supported connection readback.",
                    verification="unavailable",
                ),
                "automation": "unknown",
            }
        return _result(
            "waiting_dependency",
            "The selected companion API is not ready.",
            retryable=True,
        )
    except (OSError, ValueError, TypeError, KeyError):
        return _result(
            "waiting_dependency",
            "The selected companion connection could not be read or authenticated yet.",
            retryable=True,
        )


def apply_helper(
    action,
    config,
    payload,
    operation="apply",
    *,
    client_factory=ApiClient,
    manager=None,
    target_ids=None,
):
    source, family, instance = _scope(action)
    if family not in HELPERS or action.get("action") != "arr_connections":
        return None
    if operation in {"inspect", "preview", "recheck", "verify"}:
        return inspect_helper(
            action,
            config,
            payload,
            operation,
            client_factory=client_factory,
            target_ids=target_ids,
        )
    try:
        source, family, instance, selected = _selected(action, config, payload)
        from utils import neutarr_settings, prowlarr_settings, profilarr_settings

        if manager is None:
            from utils.config_loader import CONFIG_MANAGER

            manager = CONFIG_MANAGER
        if manager.config is not config:
            return _result(
                "failed",
                "The setup configuration does not match its owning configuration manager.",
            )
        # This is accepted desired state even if the target is not ready yet.
        target_cfg = _resource(config, source)
        target_cfg["arr_instances"] = list(selected)
        manager.save_config()
        scoped = copy.deepcopy(config)
        scoped[family]["instances"] = {instance: copy.deepcopy(target_cfg)}
        collector = []
        patch = {
            "neutarr": neutarr_settings.patch_neutarr_config,
            "prowlarr": prowlarr_settings.patch_prowlarr_apps,
            "profilarr": profilarr_settings.patch_profilarr_config,
        }[family]
        ok, _ = patch(
            result_collector=collector, configuration=scoped, target_ids=target_ids
        )
        evidence = [
            {
                "action_id": result.action_id,
                "execution": result.execution,
                "verification": result.verification,
                "detail": result.detail,
            }
            for result in collector
        ]
        failures = [
            item
            for item in evidence
            if item["execution"] in {"failed", "waiting_dependency", "waiting_input"}
        ]
        if failures:
            execution = (
                "failed"
                if any(item["execution"] == "failed" for item in failures)
                else (
                    "waiting_input"
                    if any(item["execution"] == "waiting_input" for item in failures)
                    else "waiting_dependency"
                )
            )
            return _result(
                execution,
                "Selected companion links have unfinished configuration or require explicit policy/adoption review.",
                retryable=execution in {"failed", "waiting_dependency"},
                evidence=evidence,
            )
        if not ok:
            return _result(
                "failed",
                "The selected companion configuration could not be saved.",
                retryable=True,
                evidence=evidence,
            )
        result = inspect_helper(
            action,
            config,
            payload,
            operation,
            client_factory=client_factory,
            target_ids=target_ids,
        )
        result["evidence"] = evidence + list(result.get("evidence") or [])
        return result
    except InputNeeded as exc:
        return {**_result("waiting_input", str(exc)), "missing_inputs": exc.fields}
    except (ApiError, OSError, ValueError, TypeError, KeyError):
        return _result(
            "failed",
            "The selected companion could not be reconciled. Existing unrelated connections were retained.",
            retryable=True,
        )
