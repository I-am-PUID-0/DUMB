from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER
from utils import nzbdav_db
from utils.user_management import chown_recursive
from typing import Optional, Tuple
import xml.etree.ElementTree as ET
import json, os, time, urllib.request, urllib.error


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


def _collect_arr_entries() -> Tuple[list[dict], list[dict]]:
    radarr_entries = []
    sonarr_entries = []
    for svc_name, bucket in (("radarr", radarr_entries), ("sonarr", sonarr_entries)):
        svc_cfg = CONFIG_MANAGER.get(svc_name) or {}
        instances = (svc_cfg.get("instances") or {}) or {}
        for inst_key, inst in instances.items():
            if not inst.get("enabled"):
                continue
            if (inst.get("core_service") or "").lower() != "nzbdav":
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
            bucket.append({"Host": host, "ApiKey": token})
    return radarr_entries, sonarr_entries


def _join(host: str, path: str) -> str:
    return f"{host.rstrip('/')}/{path.lstrip('/')}"


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


def _get_sab_schema(host: str, key: str):
    schemas = _arr_req(_join(host, "/api/v3/downloadclient/schema"), key, "GET") or []
    for item in schemas:
        impl = (item.get("implementation") or "").lower()
        name = (item.get("implementationName") or "").lower()
        if "sab" in impl or "sab" in name:
            return item
    return schemas[0] if schemas else None


def _build_fields_from_schema(schema: dict, overrides: dict) -> list:
    fields = {}
    for f in schema.get("fields") or []:
        n = f.get("name")
        if not n:
            continue
        fields[n] = f.get("value")
    for k, v in (overrides or {}).items():
        fields[k] = v
    return [{"name": k, "value": v} for k, v in fields.items()]


def _normalize_path(p: str) -> str:
    return os.path.normpath((p or "").rstrip("/"))


def _ensure_arr_rootfolder(host: str, token: str, path: str) -> bool:
    """Ensure a root folder exists on a Sonarr/Radarr instance.
    Returns True if created, False if already present or on error."""
    if not (host and token and path):
        return False
    try:
        url = _join(host, "/api/v3/rootfolder")
        existing = _arr_req(url, token, "GET") or []
        if any(
            _normalize_path(x.get("path", "")) == _normalize_path(path)
            for x in (existing or [])
        ):
            return False
        _arr_req(url, token, "POST", {"path": path})
        logger.info("Created root folder '%s' on %s", path, host)
        return True
    except urllib.error.HTTPError as e:
        logger.warning("HTTP error ensuring rootfolder %s on %s: %s", path, host, e)
    except Exception as e:
        logger.warning("Failed to ensure rootfolder %s on %s: %s", path, host, e)
    return False


def _ensure_symlink_roots(paths: list[str]) -> None:
    user_id = CONFIG_MANAGER.get("puid")
    group_id = CONFIG_MANAGER.get("pgid")
    for path in paths:
        os.makedirs(path, exist_ok=True)
        chown_recursive(path, user_id, group_id)


def ensure_nzbdav_download_client(
    arr_host: str,
    arr_api_key: str,
    nzbdav_port: int,
    nzbdav_api_key: str,
    service: str,
    name: str = "nzbdav",
    category_map: Optional[dict] = None,
    category: Optional[str] = None,
    test_before_save: bool = False,
) -> Tuple[bool, Optional[int]]:
    def _set_field(fields_list, field_name: str, value) -> bool:
        for f in fields_list or []:
            if (f.get("name") or "").lower() == field_name.lower():
                f["value"] = value
                return True
        return False

    service = (service or "").lower()
    effective_category = (category or "").strip() or (category_map or {}).get(
        service, service
    )

    schema = _get_sab_schema(arr_host, arr_api_key)
    if not schema:
        raise RuntimeError("Could not fetch download client schema from Arr")

    fields_schema = schema.get("fields") or []
    field_names_raw = [f.get("name") for f in fields_schema]
    logger.debug("Download client schema fields for %s: %s", arr_host, field_names_raw)

    overrides = {
        "host": "127.0.0.1",
        "port": int(nzbdav_port),
        "apiKey": nzbdav_api_key,
        "useSsl": False,
        "urlBase": "",
    }

    schema_names = {str(f.get("name") or "").lower() for f in fields_schema}
    for key in ("category", "moviecategory", "tvcategory"):
        if key in schema_names:
            overrides[key] = effective_category

    for toggle in ("usecategory", "usetags"):
        if toggle in schema_names:
            overrides[toggle] = True

    fields = _build_fields_from_schema(schema, overrides)
    _set_field(fields, "useCategory", True)
    _set_field(fields, "useTags", True)

    if not _set_field(fields, "category", effective_category):
        _set_field(fields, "movieCategory", effective_category)
        _set_field(fields, "tvCategory", effective_category)

    desired = {
        "name": name,
        "enable": True,
        "protocol": "usenet",
        "priority": 1,
        "implementation": schema.get("implementation"),
        "implementationName": schema.get("implementationName"),
        "configContract": schema.get("configContract"),
        "infoLink": schema.get("infoLink"),
        "tags": [],
        "fields": fields,
    }

    if test_before_save:
        _arr_req(
            _join(arr_host, "/api/v3/downloadclient/test"), arr_api_key, "POST", desired
        )

    existing = (
        _arr_req(_join(arr_host, "/api/v3/downloadclient"), arr_api_key, "GET") or []
    )
    match = next(
        (c for c in existing if (c.get("name") or "").lower() == name.lower()), None
    )

    if match:
        client_id = match.get("id")
        put_body = desired.copy()
        put_body["id"] = client_id
        _arr_req(
            _join(arr_host, f"/api/v3/downloadclient/{client_id}"),
            arr_api_key,
            "PUT",
            put_body,
        )
        return True, client_id

    created = (
        _arr_req(
            _join(arr_host, "/api/v3/downloadclient"), arr_api_key, "POST", desired
        )
        or {}
    )
    return True, created.get("id")


def _wait_for_arr(
    host: str, token: str, timeout_s: int = 60, interval_s: float = 2.0
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


def _with_retries(
    fn,
    *args,
    attempts: int = 3,
    base_delay_s: float = 2.0,
    backoff: float = 1.6,
    **kwargs,
):
    last_err = None
    delay = max(0.1, base_delay_s)
    for i in range(max(1, attempts)):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            if i == attempts - 1:
                break
            time.sleep(delay)
            delay *= backoff
    raise last_err if last_err else RuntimeError("retry failed")


def _merge_instances(existing: list[dict], auto_list: list[dict]) -> list[dict]:
    merged = {}
    for item in existing or []:
        host = (item.get("Host") or "").strip()
        if host:
            merged[host.lower()] = item
    for item in auto_list or []:
        host = (item.get("Host") or "").strip()
        if host:
            merged[host.lower()] = item
    return sorted(
        merged.values(),
        key=lambda item: (
            (item.get("Host") or "").lower(),
            (item.get("ApiKey") or ""),
        ),
    )


def patch_nzbdav_config():
    config = CONFIG_MANAGER.get("nzbdav", {}) or {}
    backend_port = int(config.get("backend_port") or 8080)
    env_cfg = config.get("env", {}) if isinstance(config, dict) else {}

    try:
        nzbdav_api_key = nzbdav_db.get_config_value("api.key")
        if not nzbdav_api_key:
            nzbdav_api_key = env_cfg.get("FRONTEND_BACKEND_API_KEY") or ""
    except FileNotFoundError as e:
        return False, str(e)

    movies_root = "/mnt/debrid/nzbdav-symlinks/movies"
    shows_root = "/mnt/debrid/nzbdav-symlinks/shows"
    try:
        _ensure_symlink_roots([movies_root, shows_root])
    except Exception as e:
        logger.warning("Failed to ensure NzbDAV symlink roots: %s", e)

    radarr_entries, sonarr_entries = _collect_arr_entries()
    if not radarr_entries and not sonarr_entries:
        logger.info("No Radarr/Sonarr instances configured for NzbDAV.")
        return False, None

    try:
        existing = nzbdav_db.get_config_value("arr.instances")
    except FileNotFoundError as e:
        return False, str(e)
    try:
        existing_obj = json.loads(existing) if existing else {}
    except Exception:
        existing_obj = {}

    merged_radarr = _merge_instances(
        existing_obj.get("RadarrInstances", []), radarr_entries
    )
    merged_sonarr = _merge_instances(
        existing_obj.get("SonarrInstances", []), sonarr_entries
    )
    queue_rules = existing_obj.get("QueueRules", []) or []

    new_obj = {
        "RadarrInstances": merged_radarr,
        "SonarrInstances": merged_sonarr,
        "QueueRules": queue_rules,
    }
    new_value = json.dumps(new_obj, separators=(",", ":"), sort_keys=True)

    updated = False
    if new_value != (existing or ""):
        ok, err = nzbdav_db.set_config_value("arr.instances", new_value)
        if not ok:
            return False, err
        updated = True
        logger.info("Updated NzbDAV arr.instances configuration.")

    rclone_mount_dir = nzbdav_db.get_config_value("rclone.mount-dir")
    if not rclone_mount_dir:
        ok, err = nzbdav_db.set_config_value("rclone.mount-dir", "/mnt/debrid/nzbdav")
        if not ok:
            return updated, err
        updated = True
        logger.info("Set NzbDAV rclone.mount-dir to /mnt/debrid/nzbdav.")

    try:
        for entry in radarr_entries:
            host = entry.get("Host")
            api_key = entry.get("ApiKey")
            if not (host and api_key):
                continue
            if not _wait_for_arr(host, api_key):
                logger.warning("Timed out waiting for Radarr at %s", host)
                continue
            _with_retries(
                _ensure_arr_rootfolder,
                host,
                api_key,
                movies_root,
                attempts=3,
            )
        for entry in sonarr_entries:
            host = entry.get("Host")
            api_key = entry.get("ApiKey")
            if not (host and api_key):
                continue
            if not _wait_for_arr(host, api_key):
                logger.warning("Timed out waiting for Sonarr at %s", host)
                continue
            _with_retries(
                _ensure_arr_rootfolder,
                host,
                api_key,
                shows_root,
                attempts=3,
            )
    except Exception as e:
        logger.warning("Failed to ensure Arr root folders: %s", e)

    if not nzbdav_api_key:
        logger.warning("NzbDAV API key not found; skipping Arr download client setup.")
        return updated, None

    category_map = {"radarr": "movies", "sonarr": "tv"}
    for entry in radarr_entries:
        host = entry.get("Host")
        api_key = entry.get("ApiKey")
        if not (host and api_key):
            continue
        if not _wait_for_arr(host, api_key):
            logger.warning("Timed out waiting for Radarr at %s", host)
            continue
        try:
            _with_retries(
                ensure_nzbdav_download_client,
                host,
                api_key,
                backend_port,
                nzbdav_api_key,
                "radarr",
                name="nzbdav",
                category_map=category_map,
            )
        except Exception as e:
            logger.warning("Failed configuring Radarr download client: %s", e)

    for entry in sonarr_entries:
        host = entry.get("Host")
        api_key = entry.get("ApiKey")
        if not (host and api_key):
            continue
        if not _wait_for_arr(host, api_key):
            logger.warning("Timed out waiting for Sonarr at %s", host)
            continue
        try:
            _with_retries(
                ensure_nzbdav_download_client,
                host,
                api_key,
                backend_port,
                nzbdav_api_key,
                "sonarr",
                name="nzbdav",
                category_map=category_map,
            )
        except Exception as e:
            logger.warning("Failed configuring Sonarr download client: %s", e)

    return updated, None
