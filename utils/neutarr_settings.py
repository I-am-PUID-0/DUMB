from __future__ import annotations
from contextvars import ContextVar

from utils.config_loader import CONFIG_MANAGER
from utils.core_services import get_core_services
from utils.global_logger import logger
from utils.user_management import chown_single
import json
import os
import tempfile
import threading
import hashlib
import stat
from pathlib import Path
import defusedxml.ElementTree as ET
from typing import Any, Dict
from utils.integration_result import ActionResult, emit_action
from utils.integration_ownership import read_ownership, write_ownership

_CONFIG_LOCK = threading.RLock()
_OWNERSHIP_DIRECTORY = "/config/.dumb-setup/connections/neutarr"


def _entry_fingerprint(entry):
    public = {
        "name": entry.get("name"),
        "url": entry.get("api_url") or entry.get("address"),
    }
    return hashlib.sha256(json.dumps(public, sort_keys=True).encode()).hexdigest()


def _ownership_path(instance_name, *, write=False):
    root = Path(_OWNERSHIP_DIRECTORY)
    if write:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.exists():
        info = root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise ValueError(
                "NeutArr ownership storage must be private and controller-owned"
            )
    elif root.is_symlink():
        raise ValueError("NeutArr ownership storage cannot be a symbolic link")
    return root / (hashlib.sha256(str(instance_name).encode()).hexdigest() + ".json")


_CONFIG_OVERRIDE = ContextVar("arr_settings_configuration", default=None)


def _setting(key, default=None):
    configuration = _CONFIG_OVERRIDE.get()
    return (
        configuration.get(key, default)
        if configuration is not None
        else CONFIG_MANAGER.get(key, default)
    )


ARR_SERVICES = ("sonarr", "radarr", "lidarr", "whisparr")
APP_URL_FIELDS = {
    "sonarr": ("api_url",),
    "radarr": ("api_url", "address"),
    "lidarr": ("api_url",),
    "whisparr": ("api_url",),
}


def _parse_arr_api_key(config_xml_path: str) -> str:
    try:
        if not (config_xml_path and os.path.exists(config_xml_path)):
            return ""
        tree = ET.parse(config_xml_path)
        root = tree.getroot()
        node = root.find(".//ApiKey")
        if node is not None and (node.text or "").strip():
            return node.text.strip()
    except Exception as exc:
        logger.warning("Failed reading ApiKey from %s: %s", config_xml_path, exc)
    return ""


def _collect_arr_instances(selected_ids=None) -> Dict[str, list[dict[str, str]]]:
    result: Dict[str, list[dict[str, str]]] = {svc: [] for svc in ARR_SERVICES}
    for svc_name in ARR_SERVICES:
        svc_cfg = _setting(svc_name) or {}
        instances = (svc_cfg.get("instances") or {}) or {}
        for inst_name, inst in instances.items():
            if not isinstance(inst, dict) or not inst.get("enabled"):
                continue
            if selected_ids is None:
                if not inst.get("use_neutarr"):
                    continue
            elif f"{svc_name}.{inst_name}" not in selected_ids:
                continue
            port = inst.get("port") or inst.get("host_port")
            try:
                port = str(int(port)) if port is not None else None
            except Exception:
                port = None
            if not port:
                logger.warning(
                    "Skipping NeutArr link for %s instance %s: missing port",
                    svc_name,
                    inst_name,
                )

            api_key = _parse_arr_api_key((inst.get("config_file") or "").strip())
            result[svc_name].append(
                {
                    "name": inst_name,
                    "url": _arr_url(inst, port) if port else "",
                    "identity": f"{svc_name}.{inst_name}",
                    "api_key": api_key,
                    "core_services": get_core_services(inst),
                }
            )
    return result


def _arr_url(instance: dict, port: str) -> str:
    base = ""
    path = instance.get("config_file") or ""
    if path and os.path.isfile(path):
        try:
            root = ET.parse(path).getroot()
            base = (root.findtext(".//UrlBase") or "").strip().strip("/")
        except (ET.ParseError, OSError):
            pass
    return f"http://127.0.0.1:{port}" + (f"/{base}" if base else "")


def _load_app_json(config_root: str, app_type: str) -> Dict[str, Any]:
    """Missing configuration is fresh; malformed existing configuration is an error."""
    json_path = os.path.join(config_root, f"{app_type}.json")
    if not os.path.exists(json_path):
        return {"instances": []}
    with open(json_path, encoding="utf-8") as fh:
        config = json.load(fh)
    if not isinstance(config, dict) or not isinstance(
        config.get("instances", []), list
    ):
        raise ValueError("NeutArr configuration has an unsupported shape")
    return config


def _save_app_json(config_root: str, app_type: str, config: Dict[str, Any]) -> None:
    """Atomically replace a private credential-bearing file; propagate any failure."""
    os.makedirs(config_root, mode=0o700, exist_ok=True)
    json_path = os.path.join(config_root, f"{app_type}.json")
    fd, temporary = tempfile.mkstemp(prefix=f".{app_type}.", dir=config_root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        uid, gid = _setting("puid"), _setting("pgid")
        if uid is not None and gid is not None:
            chown_single(temporary, int(uid), int(gid))
        os.chmod(temporary, 0o600)
        os.replace(temporary, json_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def patch_neutarr_config(
    result_collector=None, *, configuration=None, target_ids=None
) -> tuple[bool, str | None]:
    token = _CONFIG_OVERRIDE.set(configuration)
    try:
        with _CONFIG_LOCK:
            return _patch_neutarr_config(result_collector, target_ids)
    finally:
        _CONFIG_OVERRIDE.reset(token)


def _patch_neutarr_config(
    result_collector=None, target_ids=None
) -> tuple[bool, str | None]:
    neutarr_cfg = _setting("neutarr", {}) or {}
    instances = neutarr_cfg.get("instances") or {}
    errors = []

    for inst_name, inst_cfg in instances.items():
        if not isinstance(inst_cfg, dict) or not inst_cfg.get("enabled"):
            continue
        selected_ids = inst_cfg.get("arr_instances")
        if selected_ids is not None and (
            not isinstance(selected_ids, list)
            or any(not isinstance(value, str) for value in selected_ids)
        ):
            errors.append(f"NeutArr {inst_name}: invalid Arr assignments")
            continue
        arr_instances = _collect_arr_instances(selected_ids)
        neutarr_cores = (
            set(get_core_services(inst_cfg)) if selected_ids is None else set()
        )
        env = inst_cfg.get("env") or {}
        config_root = env.get("NEUTARR_CONFIG_DIR") or os.path.join(
            inst_cfg.get("config_dir") or f"/neutarr/{inst_name.lower()}", "config"
        )
        try:
            ownership = read_ownership(_ownership_path(inst_name))
            if any(not isinstance(value, dict) for value in ownership.values()):
                raise ValueError("Invalid NeutArr ownership metadata")
        except (OSError, ValueError):
            errors.append(
                f"NeutArr {inst_name}: unable to read protected connection ownership"
            )
            emit_action(
                ActionResult(
                    action_id=f"neutarr.ownership:{inst_name}",
                    service_key="neutarr",
                    action="arr_links",
                    instance_key=inst_name,
                    execution="failed",
                    detail="Protected NeutArr ownership metadata could not be read. Existing settings were retained.",
                    retryable=True,
                ),
                result_collector,
            )
            continue
        for app_type, selected in arr_instances.items():
            if target_ids is not None and not any(
                value.startswith(app_type + ".") for value in target_ids
            ):
                continue
            desired_instances = [
                item
                for item in selected
                if not neutarr_cores
                or neutarr_cores.intersection(item.get("core_services") or [])
            ]
            pending_results = []
            try:
                current_cfg = _load_app_json(config_root, app_type)
                before = json.dumps(current_cfg, sort_keys=True)
                existing_instances = current_cfg.get("instances") or []
                desired_ids = {item["identity"] for item in desired_instances}
                owned = ownership.setdefault(app_type, {})
                # NeutArr's native UI reconstructs instance dictionaries using
                # known fields only. Restore ownership from the protected sidecar
                # when public identity still matches, preserving UI enablement.
                for identity, record in owned.items():
                    if target_ids is not None and identity not in target_ids:
                        continue
                    if not isinstance(record, dict):
                        raise ValueError("Invalid NeutArr owned connection record")
                    matching = [
                        entry
                        for entry in existing_instances
                        if isinstance(entry, dict)
                        and record.get("fingerprint") == _entry_fingerprint(entry)
                    ]
                    if len(matching) == 1 and not matching[0].get("_dumb_id"):
                        matching[0]["_dumb_id"] = identity
                for existing in existing_instances:
                    if (
                        isinstance(existing, dict)
                        and str(existing.get("_dumb_id") or "").startswith(
                            f"{app_type}."
                        )
                        and existing["_dumb_id"] not in desired_ids
                        and (target_ids is None or existing["_dumb_id"] in target_ids)
                        and (
                            not owned.get(existing["_dumb_id"])
                            or owned[existing["_dumb_id"]].get("fingerprint")
                            == _entry_fingerprint(existing)
                        )
                    ):
                        # Retire only entries created by this adapter. Keep the entry,
                        # policies and credentials available for inspection/re-enablement.
                        existing["enabled"] = False
                        existing["_dumb_retired"] = True
                for desired in desired_instances:
                    identity = desired["identity"]
                    if target_ids is not None and identity not in target_ids:
                        continue
                    action_id = f"neutarr.link:{inst_name}->{identity}"

                    def result(execution, detail, *, changed=False, retryable=False):
                        pending_results.append(
                            ActionResult(
                                action_id=action_id,
                                service_key="neutarr",
                                action="arr_link",
                                instance_key=inst_name,
                                execution=execution,
                                detail=detail,
                                changed=changed,
                                retryable=retryable,
                            )
                        )

                    target = next(
                        (
                            item
                            for item in existing_instances
                            if isinstance(item, dict)
                            and item.get("_dumb_id") == identity
                        ),
                        None,
                    )
                    if (
                        target is not None
                        and owned.get(identity)
                        and owned[identity].get("fingerprint")
                        != _entry_fingerprint(target)
                    ):
                        result(
                            "waiting_input",
                            "The connection identity changed in NeutArr. Review it before updating managed fields.",
                        )
                        continue
                    if not desired.get("url"):
                        result(
                            "waiting_input",
                            "Set a valid Arr port before linking this instance.",
                        )
                        continue
                    if not desired.get("api_key"):
                        result(
                            "waiting_dependency",
                            "Waiting for the Arr API key.",
                            retryable=True,
                        )
                        continue
                    if target is None:
                        collision = any(
                            isinstance(item, dict)
                            and str(item.get("name") or "").casefold()
                            == desired["name"].casefold()
                            for item in existing_instances
                        )
                        if collision:
                            result(
                                "waiting_input",
                                "An existing user connection has this name. Rename it or configure the connection manually.",
                            )
                            continue
                        # A demonstrably empty upstream Default slot can be consumed
                        # once; assigning ownership immediately prevents subsequent reuse.
                        target = next(
                            (
                                item
                                for item in existing_instances
                                if isinstance(item, dict)
                                and not item.get("_dumb_id")
                                and item.get("name") == "Default"
                                and not any(
                                    item.get(field)
                                    for field in ("api_url", "address", "api_key")
                                )
                                and item.get("enabled", True)
                            ),
                            None,
                        )
                        if target is None:
                            target = {"enabled": False}
                            existing_instances.append(target)
                        target["enabled"] = False
                        target["_dumb_id"] = identity
                    old = dict(target)
                    target["name"] = desired["name"]
                    for field in APP_URL_FIELDS.get(app_type, ("api_url",)):
                        target[field] = desired["url"]
                    target["api_key"] = desired["api_key"]
                    owned[identity] = {"fingerprint": _entry_fingerprint(target)}
                    if not target.get("enabled", True):
                        result(
                            "waiting_input",
                            "The managed connection is disabled. Enable it in NeutArr when ready.",
                            changed=old != target,
                        )
                    else:
                        result(
                            "applied" if old != target else "already_present",
                            "Arr connection configured; connection testing remains available in NeutArr.",
                            changed=old != target,
                        )
                current_cfg["instances"] = existing_instances
                if before != json.dumps(current_cfg, sort_keys=True):
                    _save_app_json(config_root, app_type, current_cfg)
                if owned:
                    write_ownership(_ownership_path(inst_name, write=True), ownership)
                for item in pending_results:
                    emit_action(item, result_collector)
            except Exception:
                errors.append(
                    f"NeutArr {inst_name}: unable to reconcile {app_type} configuration"
                )
                emit_action(
                    ActionResult(
                        action_id=f"neutarr.config:{inst_name}:{app_type}",
                        service_key="neutarr",
                        action="arr_link",
                        instance_key=inst_name,
                        execution="failed",
                        detail="NeutArr configuration could not be read or saved. Existing files were retained.",
                        retryable=True,
                    ),
                    result_collector,
                )
                logger.warning(
                    "Failed to reconcile NeutArr %s %s configuration.",
                    inst_name,
                    app_type,
                )
    return (False, "; ".join(errors)) if errors else (True, None)


def any_arr_uses_neutarr() -> bool:
    for svc_name in ARR_SERVICES:
        svc_cfg = _setting(svc_name) or {}
        instances = (svc_cfg.get("instances") or {}) or {}
        for inst in instances.values():
            if not isinstance(inst, dict):
                continue
            if inst.get("enabled") and inst.get("use_neutarr"):
                return True
    return False
