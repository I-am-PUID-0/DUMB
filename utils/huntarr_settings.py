from __future__ import annotations

from utils.config_loader import CONFIG_MANAGER
from utils.core_services import get_core_services
from utils.global_logger import logger
from utils.user_management import chown_single
import copy
import json
import os
import sqlite3
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


def _ensure_db_schema(db_path: str) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_type TEXT NOT NULL UNIQUE,
                config_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    try:
        user_id = int(CONFIG_MANAGER.get("puid"))
        group_id = int(CONFIG_MANAGER.get("pgid"))
    except (TypeError, ValueError):
        return
    try:
        chown_single(db_path, user_id, group_id)
        os.chmod(db_path, 0o664)
    except Exception as exc:
        logger.warning("Failed updating Huntarr DB permissions for %s: %s", db_path, exc)


def _load_default_app_config(repo_dir: str, app_type: str) -> Dict[str, Any]:
    default_path = os.path.join(
        repo_dir, "src", "primary", "default_configs", f"{app_type}.json"
    )
    if os.path.isfile(default_path):
        try:
            with open(default_path, "r") as handle:
                return json.load(handle)
        except Exception as exc:
            logger.warning("Failed reading Huntarr defaults for %s: %s", app_type, exc)
    return {"instances": []}


def _load_app_config(conn: sqlite3.Connection, app_type: str) -> Dict[str, Any]:
    cur = conn.execute(
        "SELECT config_data FROM app_configs WHERE app_type = ?", (app_type,)
    )
    row = cur.fetchone()
    if not row:
        return {}
    try:
        return json.loads(row[0])
    except Exception as exc:
        logger.warning("Failed parsing Huntarr config for %s: %s", app_type, exc)
        return {}


def _save_app_config(conn: sqlite3.Connection, app_type: str, config: Dict[str, Any]):
    conn.execute(
        """
        INSERT OR REPLACE INTO app_configs (app_type, config_data, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        """,
        (app_type, json.dumps(config, indent=2)),
    )
    conn.commit()


def _collect_arr_instances() -> Dict[str, list[dict[str, str]]]:
    result: Dict[str, list[dict[str, str]]] = {svc: [] for svc in ARR_SERVICES}
    for svc_name in ARR_SERVICES:
        svc_cfg = CONFIG_MANAGER.get(svc_name) or {}
        instances = (svc_cfg.get("instances") or {}) or {}
        for inst_name, inst in instances.items():
            if not isinstance(inst, dict) or not inst.get("enabled"):
                continue
            if not inst.get("use_huntarr"):
                continue
            port = inst.get("port") or inst.get("host_port")
            try:
                port = str(int(port)) if port is not None else None
            except Exception:
                port = None
            if not port:
                logger.warning(
                    "Skipping Huntarr link for %s instance %s: missing port",
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


def _merge_instance(
    existing: dict,
    desired: dict,
    url_fields: tuple[str, ...],
) -> None:
    existing["name"] = desired["name"]
    for field in url_fields:
        existing[field] = desired["url"]
    if desired.get("api_key"):
        existing["api_key"] = desired["api_key"]
    if "enabled" in existing:
        existing["enabled"] = True


def patch_huntarr_config() -> tuple[bool, str | None]:
    huntarr_cfg = CONFIG_MANAGER.get("huntarr", {}) or {}
    instances = (huntarr_cfg.get("instances") or {}) or {}
    if not instances:
        return True, None

    arr_instances = _collect_arr_instances()
    if not any(arr_instances.values()):
        return True, None

    for inst_name, inst_cfg in instances.items():
        if not isinstance(inst_cfg, dict):
            continue
        huntarr_cores = get_core_services(inst_cfg)
        db_path = inst_cfg.get("config_file")
        if not db_path:
            config_dir = inst_cfg.get("config_dir") or "/huntarr/default"
            db_path = os.path.join(config_dir, "config", "huntarr.db")

        _ensure_db_schema(db_path)
        repo_dir = inst_cfg.get("config_dir") or "/huntarr/default"

        try:
            with sqlite3.connect(db_path) as conn:
                for app_type, desired_instances in arr_instances.items():
                    if huntarr_cores:
                        desired_instances = [
                            inst
                            for inst in desired_instances
                            if set(inst.get("core_services") or []) & set(huntarr_cores)
                        ]
                    if not desired_instances:
                        continue
                    url_fields = APP_URL_FIELDS.get(app_type, ("api_url",))
                    current_cfg = _load_app_config(conn, app_type)
                    if not current_cfg:
                        current_cfg = _load_default_app_config(repo_dir, app_type)

                    default_instances = current_cfg.get("instances") or []
                    default_template = (
                        copy.deepcopy(default_instances[0])
                        if default_instances
                        else {"name": "Default"}
                    )
                    existing_instances = current_cfg.get("instances") or []
                    desired_names = {
                        desired.get("name", "").strip().lower()
                        for desired in desired_instances
                    }
                    if desired_names:
                        existing_instances = [
                            inst
                            for inst in existing_instances
                            if str(inst.get("name", "")).strip().lower()
                            not in ("default", "instance 1: default")
                        ]
                    by_name = {
                        str(inst.get("name", "")).lower(): inst
                        for inst in existing_instances
                        if isinstance(inst, dict)
                    }

                    for desired in desired_instances:
                        name_key = desired.get("name", "").lower()
                        if name_key in by_name:
                            _merge_instance(by_name[name_key], desired, url_fields)
                        else:
                            new_inst = copy.deepcopy(default_template)
                            _merge_instance(new_inst, desired, url_fields)
                            existing_instances.append(new_inst)

                    current_cfg["instances"] = existing_instances
                    _save_app_config(conn, app_type, current_cfg)
        except Exception as exc:
            logger.warning(
                "Failed to update Huntarr config for instance %s: %s", inst_name, exc
            )
            return False, str(exc)

    return True, None


def any_arr_uses_huntarr() -> bool:
    for svc_name in ARR_SERVICES:
        svc_cfg = CONFIG_MANAGER.get(svc_name) or {}
        instances = (svc_cfg.get("instances") or {}) or {}
        for inst in instances.values():
            if not isinstance(inst, dict):
                continue
            if inst.get("enabled") and inst.get("use_huntarr"):
                return True
    return False
