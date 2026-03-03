from __future__ import annotations

from utils.config_loader import CONFIG_MANAGER
from utils.core_services import get_core_services
from utils.global_logger import logger
from utils.user_management import chown_single
import json
import os
import xml.etree.ElementTree as ET
from typing import Any, Dict


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


def _collect_arr_instances() -> Dict[str, list[dict[str, str]]]:
    result: Dict[str, list[dict[str, str]]] = {svc: [] for svc in ARR_SERVICES}
    for svc_name in ARR_SERVICES:
        svc_cfg = CONFIG_MANAGER.get(svc_name) or {}
        instances = (svc_cfg.get("instances") or {}) or {}
        for inst_name, inst in instances.items():
            if not isinstance(inst, dict) or not inst.get("enabled"):
                continue
            if not inst.get("use_neutarr"):
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
                continue

            api_key = _parse_arr_api_key((inst.get("config_file") or "").strip())
            result[svc_name].append(
                {
                    "name": inst_name,
                    "url": f"http://127.0.0.1:{port}",
                    "api_key": api_key,
                    "core_services": get_core_services(inst),
                }
            )
    return result


def _load_app_json(config_root: str, app_type: str) -> Dict[str, Any]:
    """Load per-app JSON config from NeutArr's config directory."""
    json_path = os.path.join(config_root, f"{app_type}.json")
    if os.path.isfile(json_path):
        try:
            with open(json_path, "r") as fh:
                return json.load(fh)
        except Exception as exc:
            logger.warning("Failed reading NeutArr config %s: %s", json_path, exc)
    return {"instances": [{"name": "Default", "api_url": "", "api_key": "", "enabled": True}]}


def _save_app_json(config_root: str, app_type: str, config: Dict[str, Any]) -> None:
    """Persist per-app JSON config to NeutArr's config directory, preserving ownership."""
    os.makedirs(config_root, exist_ok=True)
    json_path = os.path.join(config_root, f"{app_type}.json")
    try:
        with open(json_path, "w") as fh:
            json.dump(config, fh, indent=2)
        try:
            user_id = int(CONFIG_MANAGER.get("puid"))
            group_id = int(CONFIG_MANAGER.get("pgid"))
            chown_single(json_path, user_id, group_id)
            os.chmod(json_path, 0o664)
        except (TypeError, ValueError):
            pass
    except Exception as exc:
        logger.warning("Failed writing NeutArr config %s: %s", json_path, exc)


def patch_neutarr_config() -> tuple[bool, str | None]:
    neutarr_cfg = CONFIG_MANAGER.get("neutarr", {}) or {}
    instances = (neutarr_cfg.get("instances") or {}) or {}
    if not instances:
        return True, None

    arr_instances = _collect_arr_instances()
    if not any(arr_instances.values()):
        return True, None

    for inst_name, inst_cfg in instances.items():
        if not isinstance(inst_cfg, dict):
            continue
        neutarr_cores = get_core_services(inst_cfg)

        # Derive config root from env or config_dir
        env = inst_cfg.get("env") or {}
        config_root = env.get("NEUTARR_CONFIG_DIR") or os.path.join(
            inst_cfg.get("config_dir") or f"/neutarr/{inst_name.lower()}", "config"
        )

        try:
            for app_type, desired_instances in arr_instances.items():
                if neutarr_cores:
                    desired_instances = [
                        inst
                        for inst in desired_instances
                        if set(inst.get("core_services") or []) & set(neutarr_cores)
                    ]
                if not desired_instances:
                    continue

                url_fields = APP_URL_FIELDS.get(app_type, ("api_url",))
                current_cfg = _load_app_json(config_root, app_type)
                existing_instances = current_cfg.get("instances") or []

                # Build lookup by normalised name
                by_name = {
                    str(inst.get("name", "")).strip().lower(): inst
                    for inst in existing_instances
                    if isinstance(inst, dict)
                }

                for desired in desired_instances:
                    name_key = desired.get("name", "").strip().lower()
                    target = by_name.get(name_key)
                    if target is None:
                        # Default slot exists — reuse it; otherwise append new entry
                        target = by_name.get("default")
                    if target is not None:
                        target["name"] = desired["name"]
                        for field in url_fields:
                            target[field] = desired["url"]
                        if desired.get("api_key"):
                            target["api_key"] = desired["api_key"]
                        target["enabled"] = True
                    else:
                        new_inst: Dict[str, Any] = {
                            "name": desired["name"],
                            "api_url": desired["url"],
                            "api_key": desired.get("api_key", ""),
                            "enabled": True,
                        }
                        for field in url_fields:
                            new_inst[field] = desired["url"]
                        existing_instances.append(new_inst)

                current_cfg["instances"] = existing_instances
                _save_app_json(config_root, app_type, current_cfg)

        except Exception as exc:
            logger.warning(
                "Failed to update NeutArr config for instance %s: %s", inst_name, exc
            )
            return False, str(exc)

    return True, None


def any_arr_uses_neutarr() -> bool:
    for svc_name in ARR_SERVICES:
        svc_cfg = CONFIG_MANAGER.get(svc_name) or {}
        instances = (svc_cfg.get("instances") or {}) or {}
        for inst in instances.values():
            if not isinstance(inst, dict):
                continue
            if inst.get("enabled") and inst.get("use_neutarr"):
                return True
    return False
