from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER
from typing import Optional, Tuple
import xml.etree.ElementTree as ET
import json, os, time, urllib.request, urllib.error


ARR_SERVICES = [
    "sonarr",
    "radarr",
    "lidarr",
    "readarr",
    "whisparr",
    "whisparr-v3",
]

ARR_APP_MAP = {
    "sonarr": "Sonarr",
    "radarr": "Radarr",
    "lidarr": "Lidarr",
    "readarr": "Readarr",
    "whisparr": "Whisparr",
    "whisparr-v3": "Whisparr",
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
    except Exception as e:
        logger.warning("Failed reading ApiKey from %s: %s", config_xml_path, e)
    return ""


def _wait_for_api_key(
    config_xml_path: str, timeout_s: int = 60, interval_s: float = 2.0
) -> str:
    deadline = time.time() + max(1, timeout_s)
    while time.time() < deadline:
        token = _parse_arr_api_key(config_xml_path)
        if token:
            return token
        time.sleep(interval_s)
    return ""


def _collect_arr_entries() -> list[dict]:
    entries = []
    for svc_name in ARR_SERVICES:
        svc_cfg = CONFIG_MANAGER.get(svc_name) or {}
        instances = (svc_cfg.get("instances") or {}) or {}
        for inst_key, inst in instances.items():
            if not inst.get("enabled"):
                continue
            port = inst.get("port") or inst.get("host_port")
            try:
                port = str(int(port)) if port is not None else None
            except Exception:
                port = None
            if not port:
                logger.warning(
                    "Skipping %s instance %s: missing port", svc_name, inst_key
                )
                continue
            host = f"http://127.0.0.1:{port}"
            cfg_path = (inst.get("config_file") or "").strip()
            token = _parse_arr_api_key(cfg_path)
            if not token:
                logger.warning(
                    "Skipping %s instance %s: missing API key", svc_name, inst_key
                )
                continue
            entries.append(
                {
                    "service": svc_name,
                    "instance": inst_key,
                    "host": host,
                    "api_key": token,
                }
            )
    return entries


def _join(host: str, path: str) -> str:
    return f"{host.rstrip('/')}/{path.lstrip('/')}"


def _prowlarr_req(
    url: str,
    key: str,
    method: str = "GET",
    data: Optional[dict] = None,
    timeout: int = 10,
):
    headers = {"X-Api-Key": key, "Accept": "application/json"}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return raw.decode("utf-8")


def _arr_req(
    url: str,
    key: str,
    method: str = "GET",
    data: Optional[dict] = None,
    timeout: int = 10,
):
    headers = {"X-Api-Key": key, "Accept": "application/json"}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return raw.decode("utf-8")


def _wait_for_arr(
    host: str, token: str, timeout_s: int = 30, interval_s: float = 2.0
) -> bool:
    deadline = time.time() + max(1, timeout_s)
    url = _join(host, "/api/v3/system/status")
    while time.time() < deadline:
        try:
            _arr_req(url, token, "GET", timeout=5)
            return True
        except Exception:
            time.sleep(interval_s)
    return False


def _wait_for_prowlarr(
    host: str, token: str, timeout_s: int = 30, interval_s: float = 2.0
) -> bool:
    deadline = time.time() + max(1, timeout_s)
    url = _join(host, "/api/v1/system/status")
    while time.time() < deadline:
        try:
            _prowlarr_req(url, token, "GET", timeout=5)
            return True
        except Exception:
            time.sleep(interval_s)
    return False


def _get_prowlarr_schemas(host: str, token: str) -> list:
    for path in ("/api/v1/applications/schema", "/api/v3/applications/schema"):
        try:
            data = _prowlarr_req(_join(host, path), token, "GET")
            if isinstance(data, list):
                return data
        except Exception:
            continue
    return []


def _find_schema(schemas: list, app_name: str) -> Optional[dict]:
    target = (app_name or "").lower()
    for item in schemas:
        impl = (item.get("implementation") or "").lower()
        name = (item.get("implementationName") or "").lower()
        if target in impl or target in name:
            return item
    return None


def _build_fields_from_schema(schema: dict, overrides: dict) -> list:
    fields = {}
    for f in schema.get("fields") or []:
        n = f.get("name")
        if not n:
            continue
        fields[n] = f.get("value")

    for key, value in (overrides or {}).items():
        for existing in list(fields.keys()):
            if existing.lower() == key.lower():
                fields[existing] = value
                break

    return [{"name": k, "value": v} for k, v in fields.items()]


def _is_application_current(existing: dict, desired: dict) -> bool:
    if not existing or not desired:
        return False
    for key in ("enable", "syncLevel", "implementation", "configContract"):
        if (existing.get(key) or "") != (desired.get(key) or ""):
            return False
    existing_fields = {
        (f.get("name") or "").lower(): f.get("value")
        for f in (existing.get("fields") or [])
    }
    desired_fields = {
        (f.get("name") or "").lower(): f.get("value")
        for f in (desired.get("fields") or [])
    }
    for key, value in desired_fields.items():
        if existing_fields.get(key) != value:
            return False
    return True


def _build_application_payload(
    schema: dict,
    app_name: str,
    arr_host: str,
    arr_api_key: str,
    instance_name: str,
) -> dict:
    name = f"{app_name} ({instance_name})"
    overrides = {
        "baseUrl": arr_host,
        "url": arr_host,
        "host": arr_host,
        "apiKey": arr_api_key,
        "syncLevel": "fullSync",
    }
    fields = _build_fields_from_schema(schema, overrides)
    return {
        "name": name,
        "enable": True,
        "syncLevel": "fullSync",
        "implementation": schema.get("implementation"),
        "implementationName": schema.get("implementationName"),
        "configContract": schema.get("configContract"),
        "infoLink": schema.get("infoLink"),
        "tags": [],
        "fields": fields,
    }


def _find_existing_application(
    existing: list, desired: dict, schema: dict, arr_host: str
) -> Optional[dict]:
    if not existing:
        return None
    name = (desired.get("name") or "").lower()
    match = next((c for c in existing if (c.get("name") or "").lower() == name), None)
    if match:
        return match
    impl = (schema.get("implementation") or "").lower()
    arr_host_norm = arr_host.rstrip("/")
    for item in existing:
        if (item.get("implementation") or "").lower() != impl:
            continue
        for field in item.get("fields") or []:
            if (field.get("name") or "").lower() == "baseurl":
                if (field.get("value") or "").rstrip("/") == arr_host_norm:
                    return item
    return None


def _apply_application(host: str, token: str, desired: dict, match: Optional[dict]):
    if match:
        app_id = match.get("id")
        put_body = desired.copy()
        put_body["id"] = app_id
        _prowlarr_req(
            _join(host, f"/api/v1/applications/{app_id}"),
            token,
            "PUT",
            put_body,
        )
        return True, app_id
    created = (
        _prowlarr_req(_join(host, "/api/v1/applications"), token, "POST", desired)
        or {}
    )
    return True, created.get("id")


def patch_prowlarr_apps() -> Tuple[bool, Optional[str]]:
    logger.info("Starting Prowlarr application sync.")
    prowlarr_cfg = CONFIG_MANAGER.get("prowlarr") or {}
    instances = (prowlarr_cfg.get("instances") or {}) or {}
    arr_entries = _collect_arr_entries()
    if not arr_entries:
        return True, None

    for inst_key, inst in instances.items():
        if not inst.get("enabled"):
            continue
        port = inst.get("port") or inst.get("host_port")
        try:
            port = str(int(port)) if port is not None else None
        except Exception:
            port = None
        if not port:
            logger.warning("Skipping Prowlarr %s: missing port", inst_key)
            continue
        host = f"http://127.0.0.1:{port}"
        cfg_path = (inst.get("config_file") or "").strip()
        token = _parse_arr_api_key(cfg_path)
        if not token:
            token = _wait_for_api_key(cfg_path)
        if not token:
            logger.warning("Skipping Prowlarr %s: missing API key", inst_key)
            continue
        if not _wait_for_prowlarr(host, token):
            logger.warning("Prowlarr %s is not responding; skipping app sync.", inst_key)
            continue

        schemas = _get_prowlarr_schemas(host, token)
        if not schemas:
            logger.warning("Prowlarr %s schema not available; skipping app sync.", inst_key)
            continue
        existing_apps = (
            _prowlarr_req(_join(host, "/api/v1/applications"), token, "GET") or []
        )

        for entry in arr_entries:
            app_name = ARR_APP_MAP.get(entry["service"], entry["service"].capitalize())
            arr_host = entry["host"]
            arr_key = entry["api_key"]
            schema = _find_schema(schemas, app_name)
            if not schema:
                logger.warning(
                    "Prowlarr %s could not find schema for %s.", inst_key, app_name
                )
                continue
            try:
                desired = _build_application_payload(
                    schema, app_name, arr_host, arr_key, entry["instance"]
                )
                match = _find_existing_application(
                    existing_apps, desired, schema, arr_host
                )
                if match:
                    logger.debug(
                        "Prowlarr %s application %s (%s) already configured.",
                        inst_key,
                        app_name,
                        entry["instance"],
                    )
                    continue
                if not _wait_for_arr(arr_host, arr_key, timeout_s=15, interval_s=2.0):
                    logger.warning(
                        "Arr %s (%s) not ready; skipping Prowlarr app sync.",
                        app_name,
                        entry["instance"],
                    )
                    continue
                ok, app_id = _apply_application(host, token, desired, match)
                if ok:
                    logger.info(
                        "Prowlarr %s synced application %s (%s).",
                        inst_key,
                        app_name,
                        entry["instance"],
                    )
            except urllib.error.HTTPError as e:
                logger.warning(
                    "Prowlarr %s app sync failed for %s: %s",
                    inst_key,
                    app_name,
                    e,
                )
            except Exception as e:
                logger.warning(
                    "Prowlarr %s app sync failed for %s: %s",
                    inst_key,
                    app_name,
                    e,
                )

    return True, None
