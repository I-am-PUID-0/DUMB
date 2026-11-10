"""Apply and inspect one exact Arr edge without shrinking desired ownership scope."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote

from utils.arrangement_http import ApiClient
from utils.arrangement_link_actions import (
    HELPER_FAMILIES,
    SUPPORTED_FAMILIES,
    selected_targets,
)
from utils.arrangement_connections import apply_connection, inspect_connection, _native
from utils.arrangement_helpers import apply_helper, inspect_helper, _resource


def _unselected(reason):
    return {
        "execution": "declined",
        "verification": "unavailable",
        "reason": reason,
        "retryable": False,
        "evidence": {"level": "unselected_connection"},
    }


def _inspect_retirement(action, config, payload, factory):
    source, target, family = action["source"], action["target"], action["service_key"]
    cfg = _resource(config, source)
    try:
        if family == "neutarr":
            root = (cfg.get("env") or {}).get("NEUTARR_CONFIG_DIR") or os.path.join(
                cfg.get("config_dir") or "/neutarr/default", "config"
            )
            path = Path(root) / (target.partition(".")[0] + ".json")
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > 2 * 1024 * 1024
            ):
                raise ValueError("Native retirement readback unavailable")
            rows = json.loads(path.read_text()).get("instances") or []
            from utils.neutarr_settings import _ownership_path, _entry_fingerprint
            from utils.integration_ownership import read_ownership

            instance_name = source.partition(".")[2]
            ownership = read_ownership(_ownership_path(instance_name))
            owned = (ownership.get(target.partition(".")[0]) or {}).get(target) or {}
            if not owned.get("fingerprint"):
                raise ValueError("Protected native ownership is unavailable")
            matching = [
                row
                for row in rows
                if isinstance(row, dict)
                and (
                    row.get("_dumb_id") == target
                    or _entry_fingerprint(row) == owned["fingerprint"]
                )
            ]
            if not matching and rows:
                raise ValueError("Protected native ownership drifted")
            if any(row.get("enabled", True) for row in matching):
                raise ValueError("An owned connection remains active")
        elif family == "prowlarr":
            from utils.prowlarr_settings import _load_ownership

            owned_id = (_load_ownership(cfg).get("applications") or {}).get(target)
            _settings, url, key = _native(config, source)
            values = (payload.get("arrangement") or {}).get("values") or {}
            key = values.get(source + "::api_key") or key
            if not key:
                raise ValueError("Application authorization pending")
            client = factory(url, headers={"X-Api-Key": key}, managed=True)
            response = client.request("GET", "/api/v1/applications")
            if response.status_code in {404, 405}:
                response = client.request("GET", "/api/v3/applications")
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("Native retirement contract unavailable")
            owned = next(
                (
                    row
                    for row in rows
                    if isinstance(row, dict) and row.get("id") == owned_id
                ),
                None,
            )
            if owned_id is None:
                _target_cfg, target_url, _key = _native(config, target)
                candidates = [
                    row
                    for row in rows
                    if isinstance(row, dict)
                    and str(row.get("implementation") or "").lower()
                    == target.partition(".")[0]
                    and any(
                        str(field.get("name", "")).lower() == "baseurl"
                        and str(field.get("value", "")).rstrip("/")
                        == target_url.rstrip("/")
                        for field in row.get("fields") or []
                    )
                ]
                if any(
                    row.get("enable", True)
                    and str(row.get("syncLevel", "")).lower() != "disabled"
                    for row in candidates
                ):
                    raise ValueError(
                        "An active matching connection has unknown ownership"
                    )
            if (
                owned
                and owned.get("enable", True)
                and str(owned.get("syncLevel", "")).lower() != "disabled"
            ):
                raise ValueError("An owned connection remains active")
        else:
            from utils.profilarr_settings import (
                detect_profilarr_layout,
                _normalize_arr_name,
                _json_tags,
            )

            directory = cfg.get("config_dir") or "/profilarr/default"
            layout = detect_profilarr_layout(directory)
            path = (
                Path(directory)
                / "config"
                / ("data/profilarr.db" if layout == "v2" else "profilarr.db")
            )
            if layout not in {"v1", "v2"} or path.is_symlink() or not path.is_file():
                raise ValueError("Native retirement database unavailable")
            arr_family, _, instance = target.partition(".")
            arr_cfg = ((config.get(arr_family) or {}).get("instances") or {}).get(
                instance
            ) or {}
            name = _normalize_arr_name(instance, arr_cfg, arr_family)
            table = "arr_instances" if layout == "v2" else "arr_config"
            with sqlite3.connect(
                "file:" + quote(str(path), safe="/") + "?mode=ro", uri=True, timeout=1
            ) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(" + table + ")")
                }
                if not {"name", "tags"}.issubset(columns):
                    raise ValueError("Native ownership schema is unavailable")
                rows = connection.execute("SELECT name, tags FROM " + table).fetchall()
            if any(
                "dumb:auto" in _json_tags(tags)
                and ("dumb:instance:" + target in _json_tags(tags) or row_name == name)
                for row_name, tags in rows
            ):
                raise ValueError("An owned connection remains present")
    except Exception:
        return {
            "execution": "waiting_input",
            "verification": "unavailable",
            "retryable": False,
            "reason": "The removed owned connection could not be confirmed inactive. Review its native settings before confirming retirement.",
            "missing_inputs": [],
            "evidence": {"level": "retirement_not_verified", "target": target},
        }
    return {
        "execution": "applied",
        "verification": "passed",
        "retryable": False,
        "reason": "The removed owned connection was read back as inactive or absent. Other selected and user-owned connections were preserved.",
        "evidence": {
            "level": "configuration_readback",
            "retired": True,
            "target": target,
            "consumer_connection_tested": False,
        },
    }


def _dispatch(
    action,
    config,
    payload,
    operation,
    *,
    read_only,
    manager,
    client_factory,
    ownership_directory,
):
    if (
        action.get("action") != "arr_connection"
        or action.get("service_key") not in SUPPORTED_FAMILIES
    ):
        return None
    source, target, family = (
        action.get("source"),
        action.get("target"),
        action["service_key"],
    )
    if not isinstance(source, str) or not isinstance(target, str):
        return {
            "execution": "waiting_input",
            "verification": "unavailable",
            "reason": "The connection needs exact source and target identities.",
            "retryable": False,
        }
    selected = selected_targets(source, config, payload)
    if selected is None:
        return {
            "execution": "waiting_input",
            "verification": "not_checked",
            "reason": "Choose the full explicit Arr assignment before applying this connection.",
            "retryable": False,
            "missing_inputs": [source + "::arr_instances"],
        }
    retiring = (
        target not in selected
        and family in HELPER_FAMILIES
        and (action.get("retirement_pending") or action.get("retirement_required"))
    )
    if target not in selected and not retiring:
        return _unselected(
            "This exact connection is not selected. Existing native connections are preserved; remove them in the application if desired."
        )
    if family in {"prowlarr", "neutarr"} and (
        target.startswith("external_arr.")
        or (
            family == "prowlarr"
            and source + "::sync_level"
            in ((payload.get("arrangement") or {}).get("values") or {})
        )
        or (
            family == "neutarr"
            and ((payload.get("arrangement") or {}).get("values") or {}).get(
                source + "::enable_integrations"
            )
            is True
        )
        or operation == "preview"
        or ((payload.get("arrangement") or {}).get("values") or {}).get(
            source + "::adopt_existing"
        )
    ):
        from utils.arrangement_external_links import apply_external_link

        result = apply_external_link(
            action,
            config,
            payload,
            operation,
            client_factory=client_factory,
            ownership_directory=ownership_directory,
        )
        if result is not None:
            return result
    elif target.startswith("external_arr.") and family == "profilarr":
        from utils.arrangement_profiles import apply_profile_connection

        return apply_profile_connection(
            action,
            config,
            payload,
            operation,
            client_factory=client_factory,
            ownership_directory=ownership_directory,
        )
    if retiring and read_only:
        return _inspect_retirement(action, config, payload, client_factory)
    request = copy.deepcopy(action)
    request["action"] = "arr_connections"
    if family in HELPER_FAMILIES:
        callback = inspect_helper if read_only else apply_helper
        result = callback(
            request,
            config,
            payload,
            operation,
            manager=manager,
            client_factory=client_factory,
            target_ids={target},
        )
    else:
        callback = inspect_connection if read_only else apply_connection
        result = callback(
            request,
            config,
            payload,
            operation,
            client_factory=client_factory,
            ownership_directory=ownership_directory,
            target_ids={target},
        )
    if result is None:
        return {
            "execution": "waiting_input",
            "verification": "unavailable",
            "reason": "This installed application has no supported exact connection adapter.",
            "automation": "unknown",
            "retryable": False,
        }
    if retiring and result.get("execution") in {"applied", "already_present"}:
        return _inspect_retirement(action, config, payload, client_factory)
    return result


def apply_link(
    action,
    config,
    payload,
    operation="apply",
    *,
    manager=None,
    client_factory=ApiClient,
    ownership_directory="/config/.dumb-setup/connections",
):
    return _dispatch(
        action,
        config,
        payload,
        "apply" if operation == "start" else operation,
        read_only=False,
        manager=manager,
        client_factory=client_factory,
        ownership_directory=ownership_directory,
    )


def inspect_link(
    action,
    config,
    payload,
    operation="recheck",
    *,
    manager=None,
    client_factory=ApiClient,
    ownership_directory="/config/.dumb-setup/connections",
):
    return _dispatch(
        action,
        config,
        payload,
        operation,
        read_only=True,
        manager=manager,
        client_factory=client_factory,
        ownership_directory=ownership_directory,
    )
