from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER
from utils.core_services import get_core_services
from utils.user_management import chown_recursive
from typing import Optional, Tuple
import xml.etree.ElementTree as ET
import json, os, time, urllib.request, urllib.error
import tempfile
import zipfile
import threading


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

CUSTOM_INDEXER_URLS = {
    "stremthru.yml": "https://raw.githubusercontent.com/dreulavelle/Prowlarr-Indexers/main/Custom/stremthru.yml",
    "zilean.yml": "https://raw.githubusercontent.com/dreulavelle/Prowlarr-Indexers/main/Custom/zilean.yml",
}
CUSTOM_INDEXER_REPO_ZIP = (
    "https://github.com/dreulavelle/Prowlarr-Indexers/archive/refs/heads/main.zip"
)

_INDEXER_SCHEMA_LOGGED = False
_CUSTOM_INDEXER_SYNC_LOCK = threading.Lock()
_CUSTOM_INDEXER_SYNC_TS = 0.0


def _replace_links_block(content: str, links: list[str]) -> str:
    lines = content.splitlines()
    out = []
    i = 0
    replaced = False
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("links:"):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(line)
            i += 1
            while i < len(lines):
                next_line = lines[i]
                if next_line.strip().startswith("-") and next_line.startswith(
                    (indent + "  ", indent + "-")
                ):
                    i += 1
                    continue
                break
            list_indent = indent + "  "
            for link in links:
                out.append(f"{list_indent}- {link}")
            replaced = True
            continue
        out.append(line)
        i += 1
    if not replaced:
        out.append("links:")
        for link in links:
            out.append(f"  - {link}")
    trailing_newline = "\n" if content.endswith("\n") else ""
    return "\n".join(out) + trailing_newline


def ensure_custom_indexers(config_dir: str, zilean_port: int) -> None:
    global _CUSTOM_INDEXER_SYNC_TS
    with _CUSTOM_INDEXER_SYNC_LOCK:
        now = time.time()
        if now - _CUSTOM_INDEXER_SYNC_TS < 60:
            return
        _CUSTOM_INDEXER_SYNC_TS = now
    root_dir = config_dir
    indexer_root = os.path.join(config_dir, "indexer")
    if os.path.isdir(indexer_root):
        root_dir = indexer_root
    custom_dir = os.path.join(root_dir, "Definitions", "Custom")
    os.makedirs(custom_dir, exist_ok=True)
    links_map = {
        "stremthru.yml": [
            "https://stremthru.13377001.xyz/v0/torznab",
            "http://stremthru:8080",
        ],
        "zilean.yml": [
            f"http://127.0.0.1:{zilean_port}",
            "https://zileanfortheweebs.midnightignite.me",
        ],
    }
    def _write_with_links(path: str, content: str, link_overrides: list[str]) -> None:
        updated = _replace_links_block(content, link_overrides)
        with open(path, "w") as handle:
            handle.write(updated)

    try:
        with urllib.request.urlopen(CUSTOM_INDEXER_REPO_ZIP, timeout=60) as resp:
            zip_bytes = resp.read()
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "custom.zip")
            with open(zip_path, "wb") as handle:
                handle.write(zip_bytes)
            with zipfile.ZipFile(zip_path) as archive:
                for entry in archive.namelist():
                    if not entry.endswith(".yml"):
                        continue
                    parts = entry.split("/")
                    if len(parts) < 3 or parts[-2] != "Custom":
                        continue
                    filename = parts[-1]
                    target_path = os.path.join(custom_dir, filename)
                    if os.path.exists(target_path) and filename not in links_map:
                        continue
                    with archive.open(entry) as src:
                        content = src.read().decode("utf-8")
                    if filename in links_map:
                        _write_with_links(target_path, content, links_map[filename])
                    elif not os.path.exists(target_path):
                        with open(target_path, "w") as handle:
                            handle.write(content)
    except Exception as exc:
        logger.warning("Failed to sync Prowlarr custom indexers: %s", exc)

    for filename, url in CUSTOM_INDEXER_URLS.items():
        target_path = os.path.join(custom_dir, filename)
        if os.path.exists(target_path):
            try:
                with open(target_path, "r") as handle:
                    raw = handle.read()
                _write_with_links(target_path, raw, links_map.get(filename, []))
                continue
            except Exception as exc:
                logger.warning(
                    "Failed to update Prowlarr custom indexer %s: %s",
                    filename,
                    exc,
                )
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
            _write_with_links(target_path, raw, links_map.get(filename, []))
        except Exception as exc:
            logger.warning(
                "Failed to sync Prowlarr custom indexer %s: %s", filename, exc
            )
    try:
        user_id = int(CONFIG_MANAGER.get("puid"))
        group_id = int(CONFIG_MANAGER.get("pgid"))
        chown_recursive(custom_dir, user_id, group_id)
    except Exception as exc:
        logger.warning("Failed to update ownership for %s: %s", custom_dir, exc)


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
            core_services = get_core_services(inst)
            entries.append(
                {
                    "service": svc_name,
                    "instance": inst_key,
                    "host": host,
                    "api_key": token,
                    "core_services": core_services,
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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return raw.decode("utf-8")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        if body:
            logger.warning("Prowlarr API error response from %s: %s", url, body)
        raise


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
    host: str,
    token: str,
    timeout_s: int = 30,
    interval_s: float = 2.0,
    api_version: str = "v3",
) -> bool:
    deadline = time.time() + max(1, timeout_s)
    url = _join(host, f"/api/{api_version}/system/status")
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


def _get_prowlarr_indexer_schemas(host: str, token: str) -> list:
    for path in ("/api/v1/indexer/schema", "/api/v3/indexer/schema"):
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


def _find_indexer_schema(schemas: list, indexer_name: str) -> Optional[dict]:
    target = (indexer_name or "").lower()
    for item in schemas:
        impl = (item.get("implementation") or "").lower()
        name = (item.get("implementationName") or "").lower()
        display = (item.get("name") or "").lower()
        definition = (item.get("definitionName") or "").lower()
        desc = (item.get("description") or "").lower()
        if target in (impl, name, display, definition) or target in desc:
            return item
    return None


def _log_indexer_schema_names(schemas: list) -> None:
    global _INDEXER_SCHEMA_LOGGED
    if _INDEXER_SCHEMA_LOGGED:
        return
    _INDEXER_SCHEMA_LOGGED = True
    names = []
    for item in schemas:
        if not isinstance(item, dict):
            continue
        names.append(
            {
                "implementation": item.get("implementation"),
                "implementationName": item.get("implementationName"),
                "name": item.get("name"),
                "definitionName": item.get("definitionName"),
            }
        )
    logger.debug("Prowlarr indexer schemas: %s", names)


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
    # Keep user-managed tags intact: only require DUMB-managed tags to be present.
    existing_tags = {int(tag) for tag in (existing.get("tags") or []) if isinstance(tag, int)}
    required_tags = {int(tag) for tag in (desired.get("tags") or []) if isinstance(tag, int)}
    if not required_tags.issubset(existing_tags):
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
    tag_ids: Optional[list[int]] = None,
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
        "tags": tag_ids or [],
        "fields": fields,
    }


def _get_prowlarr_tags(host: str, token: str) -> list[dict]:
    for path in ("/api/v1/tag", "/api/v3/tag"):
        try:
            data = _prowlarr_req(_join(host, path), token, "GET")
            if isinstance(data, list):
                return data
        except Exception:
            continue
    return []


def _ensure_tag_ids(host: str, token: str, labels: list[str]) -> dict[str, int]:
    tag_map = {}
    if not labels:
        return tag_map
    existing = _get_prowlarr_tags(host, token)
    for tag in existing:
        label = (tag.get("label") or "").strip().lower()
        tag_id = tag.get("id")
        if label and isinstance(tag_id, int):
            tag_map[label] = tag_id
    for label in labels:
        label_lc = label.strip().lower()
        if not label_lc or label_lc in tag_map:
            continue
        created = None
        for path in ("/api/v1/tag", "/api/v3/tag"):
            try:
                created = _prowlarr_req(
                    _join(host, path), token, "POST", {"label": label_lc}
                )
                break
            except Exception:
                continue
        if isinstance(created, dict):
            tag_id = created.get("id")
            if isinstance(tag_id, int):
                tag_map[label_lc] = tag_id
    return tag_map


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


def _find_existing_indexer(existing: list, desired: dict) -> Optional[dict]:
    if not existing:
        return None
    name = (desired.get("name") or "").lower()
    match = next((c for c in existing if (c.get("name") or "").lower() == name), None)
    if match:
        return match
    impl = (desired.get("implementation") or "").lower()
    for item in existing:
        if (item.get("implementation") or "").lower() == impl:
            return item
    return None


def _is_indexer_current(existing: dict, desired: dict) -> bool:
    if not existing or not desired:
        return False
    for key in ("enable", "implementation", "configContract"):
        if (existing.get(key) or "") != (desired.get(key) or ""):
            return False
    if sorted(existing.get("tags") or []) != sorted(desired.get("tags") or []):
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


def _build_indexer_payload(
    schema: dict,
    indexer_name: str,
    base_url: str,
    tag_ids: Optional[list[int]] = None,
    enabled: bool = True,
) -> dict:
    definition_name = (schema.get("definitionName") or "").strip()
    definition_file = f"Custom/{definition_name}" if definition_name else ""
    overrides = {
        "baseUrl": base_url,
        "url": base_url,
        "host": base_url,
        "apiUrl": base_url,
        "indexerUrl": base_url,
        "link": base_url,
    }
    if definition_file:
        overrides["definitionFile"] = definition_file
    fields = _build_fields_from_schema(schema, overrides)
    protocol = (schema.get("protocol") or "").lower()
    payload = {
        "name": indexer_name,
        "enable": enabled,
        "priority": 25,
        "appProfileId": 0,
        "protocol": protocol or None,
        "implementation": schema.get("implementation"),
        "implementationName": schema.get("implementationName"),
        "configContract": schema.get("configContract"),
        "infoLink": schema.get("infoLink"),
        "minimumSeeders": 1,
        "definitionFile": definition_file or None,
        "tags": tag_ids or [],
        "fields": fields,
    }
    return payload


def _get_default_app_profile_id(host: str, token: str) -> Optional[int]:
    for path in ("/api/v1/appProfile", "/api/v1/appprofile", "/api/v3/appProfile"):
        try:
            data = _prowlarr_req(_join(host, path), token, "GET")
            if isinstance(data, list) and data:
                for item in data:
                    if isinstance(item, dict) and item.get("id"):
                        return int(item["id"])
        except Exception:
            continue
    return None


def _apply_indexer(host: str, token: str, desired: dict, match: Optional[dict]):
    if match:
        idx_id = match.get("id")
        put_body = desired.copy()
        put_body["id"] = idx_id
        _prowlarr_req(
            _join(host, f"/api/v1/indexer/{idx_id}"),
            token,
            "PUT",
            put_body,
        )
        return True, idx_id
    created = (
        _prowlarr_req(_join(host, "/api/v1/indexer"), token, "POST", desired) or {}
    )
    return True, created.get("id")


def ensure_zilean_indexer(host: str, token: str, base_url: str, tag_ids: list[int]):
    schemas = _get_prowlarr_indexer_schemas(host, token)
    if not schemas:
        logger.warning("Prowlarr indexer schema not available; skipping Zilean sync.")
        return
    schema = _find_indexer_schema(schemas, "zilean")
    if not schema:
        _log_indexer_schema_names(schemas)
        logger.warning("Prowlarr could not find schema for Zilean.")
        return
    logger.debug(
        "Prowlarr Zilean schema: %s",
        {
            "implementation": schema.get("implementation"),
            "implementationName": schema.get("implementationName"),
            "name": schema.get("name"),
            "definitionName": schema.get("definitionName"),
            "protocol": schema.get("protocol"),
            "fields": [f.get("name") for f in (schema.get("fields") or [])],
        },
    )
    desired = _build_indexer_payload(
        schema, "Zilean", base_url, tag_ids=tag_ids, enabled=True
    )
    app_profile_id = _get_default_app_profile_id(host, token)
    if app_profile_id:
        desired["appProfileId"] = app_profile_id
    desired.pop("minimumSeeders", None)
    existing = _prowlarr_req(_join(host, "/api/v1/indexer"), token, "GET") or []

    def _match_by_definition(existing_items: list, definition_file: str) -> Optional[dict]:
        for item in existing_items:
            for field in item.get("fields") or []:
                if (field.get("name") or "").lower() == "definitionfile":
                    value = (field.get("value") or "").lower()
                    if definition_file.lower() in value:
                        return item
        return None

    match = _match_by_definition(existing, "custom/zilean")
    if match:
        def _get_field(fields_list: list, field_name: str) -> Optional[str]:
            for field in fields_list:
                if (field.get("name") or "").lower() == field_name.lower():
                    return field.get("value") or ""
            return ""

        def _set_field(fields_list: list, field_name: str, value) -> None:
            for field in fields_list:
                if (field.get("name") or "").lower() == field_name.lower():
                    field["value"] = value
                    return
            fields_list.append({"name": field_name, "value": value})

        fields = match.get("fields") or []
        current_base = _get_field(fields, "baseUrl")
        current_base = (current_base or "").strip()
        if current_base == base_url:
            logger.debug("Prowlarr Zilean base URL already set; leaving as-is.")
            return
        wrong_values = {
            "",
            "https://stremthru.13377001.xyz/v0/torznab",
            "http://stremthru:8080",
        }
        local_prefixes = ("http://127.0.0.1:", "http://localhost:")
        if current_base and current_base not in wrong_values and not current_base.startswith(local_prefixes):
            logger.debug("Prowlarr Zilean base URL set by user; leaving as-is.")
            return
        _set_field(fields, "baseUrl", base_url)
        _set_field(fields, "definitionFile", "Custom/zilean")
        put_body = match.copy()
        put_body["fields"] = fields
        if not put_body.get("definitionFile"):
            put_body["definitionFile"] = "Custom/zilean"
        put_body["baseUrl"] = base_url
        _prowlarr_req(
            _join(host, f"/api/v1/indexer/{match.get('id')}"),
            token,
            "PUT",
            put_body,
        )
        logger.info("Prowlarr updated Zilean base URL.")
        return
    ok, _ = _apply_indexer(host, token, desired, None)
    if ok:
        logger.info("Prowlarr created Zilean indexer.")


def ensure_stremthru_indexer(host: str, token: str, tag_ids: list[int]):
    schemas = _get_prowlarr_indexer_schemas(host, token)
    if not schemas:
        logger.warning("Prowlarr indexer schema not available; skipping StremThru sync.")
        return
    schema = _find_indexer_schema(schemas, "stremthru")
    if not schema:
        _log_indexer_schema_names(schemas)
        logger.warning("Prowlarr could not find schema for StremThru.")
        return
    logger.debug(
        "Prowlarr StremThru schema: %s",
        {
            "implementation": schema.get("implementation"),
            "implementationName": schema.get("implementationName"),
            "name": schema.get("name"),
            "definitionName": schema.get("definitionName"),
            "protocol": schema.get("protocol"),
            "fields": [f.get("name") for f in (schema.get("fields") or [])],
        },
    )
    desired = _build_indexer_payload(
        schema,
        "StremThru",
        "https://stremthru.13377001.xyz/v0/torznab",
        tag_ids=tag_ids,
        enabled=True,
    )
    app_profile_id = _get_default_app_profile_id(host, token)
    if app_profile_id:
        desired["appProfileId"] = app_profile_id
    existing = _prowlarr_req(_join(host, "/api/v1/indexer"), token, "GET") or []

    def _match_by_definition(existing_items: list, definition_file: str) -> Optional[dict]:
        for item in existing_items:
            for field in item.get("fields") or []:
                if (field.get("name") or "").lower() == "definitionfile":
                    value = (field.get("value") or "").lower()
                    if definition_file.lower() in value:
                        return item
        return None

    match = _match_by_definition(existing, "custom/stremthru")
    if match:
        def _get_field(fields_list: list, field_name: str) -> Optional[str]:
            for field in fields_list:
                if (field.get("name") or "").lower() == field_name.lower():
                    return field.get("value") or ""
            return ""

        def _set_field(fields_list: list, field_name: str, value) -> None:
            for field in fields_list:
                if (field.get("name") or "").lower() == field_name.lower():
                    field["value"] = value
                    return
            fields_list.append({"name": field_name, "value": value})

        fields = match.get("fields") or []
        current_base = _get_field(fields, "baseUrl")
        stremthru_url = "https://stremthru.13377001.xyz/v0/torznab"
        wrong_values = {
            "",
            "http://127.0.0.1:8182",
            "https://zileanfortheweebs.midnightignite.me",
        }
        if current_base and current_base.strip().lower() == stremthru_url.lower():
            logger.debug("Prowlarr StremThru base URL already set; leaving as-is.")
            return
        if current_base and current_base.strip() not in wrong_values:
            logger.debug("Prowlarr StremThru base URL set by user; leaving as-is.")
            return
        _set_field(fields, "baseUrl", stremthru_url)
        _set_field(fields, "definitionFile", "Custom/stremthru")
        put_body = match.copy()
        put_body["fields"] = fields
        if not put_body.get("definitionFile"):
            put_body["definitionFile"] = "Custom/stremthru"
        put_body["baseUrl"] = stremthru_url
        _prowlarr_req(
            _join(host, f"/api/v1/indexer/{match.get('id')}"),
            token,
            "PUT",
            put_body,
        )
        logger.info("Prowlarr updated StremThru base URL.")
        return
    ok, _ = _apply_indexer(host, token, desired, None)
    if ok:
        logger.info("Prowlarr created StremThru indexer.")


def _apply_application(host: str, token: str, desired: dict, match: Optional[dict]):
    if match:
        app_id = match.get("id")
        put_body = desired.copy()
        existing_tags = [tag for tag in (match.get("tags") or []) if isinstance(tag, int)]
        desired_tags = [tag for tag in (desired.get("tags") or []) if isinstance(tag, int)]
        # Preserve user-defined tags while ensuring managed tags are present.
        put_body["tags"] = sorted(set(existing_tags + desired_tags))
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
    prowlarr_cfg = CONFIG_MANAGER.get("prowlarr") or {}
    instances = (prowlarr_cfg.get("instances") or {}) or {}
    enabled_instances = [
        (inst_key, inst)
        for inst_key, inst in instances.items()
        if isinstance(inst, dict) and inst.get("enabled")
    ]
    if not enabled_instances:
        return True, "Prowlarr disabled"

    logger.info("Starting Prowlarr application sync.")
    zilean_port = 8182
    zilean_enabled = False
    decypharr_enabled = False
    try:
        zilean_cfg = CONFIG_MANAGER.get("zilean", {}) or {}
        zilean_port = int(zilean_cfg.get("port", 8182))
        zilean_enabled = bool(zilean_cfg.get("enabled"))
        decypharr_cfg = CONFIG_MANAGER.get("decypharr", {}) or {}
        decypharr_enabled = bool(decypharr_cfg.get("enabled"))
        for _, inst in enabled_instances:
            config_dir = inst.get("config_dir")
            if config_dir:
                ensure_custom_indexers(config_dir, zilean_port)
    except Exception as exc:
        logger.warning("Prowlarr custom indexer sync skipped: %s", exc)
    arr_entries = _collect_arr_entries()
    if not arr_entries:
        return True, None

    for inst_key, inst in enabled_instances:
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
        needed_tags = sorted(
            {
                svc
                for entry in arr_entries
                for svc in entry.get("core_services") or []
                if svc in ("decypharr", "nzbdav")
            }
        )
        if decypharr_enabled and "decypharr" not in needed_tags:
            needed_tags.append("decypharr")
        tag_map = _ensure_tag_ids(host, token, needed_tags)
        if decypharr_enabled:
            zilean_url = (
                f"http://127.0.0.1:{zilean_port}"
                if zilean_enabled
                else "https://zileanfortheweebs.midnightignite.me"
            )
            tag_ids = []
            if "decypharr" in tag_map:
                tag_ids = [tag_map["decypharr"]]
            try:
                ensure_zilean_indexer(host, token, zilean_url, tag_ids)
                ensure_stremthru_indexer(host, token, tag_ids)
            except Exception as exc:
                logger.warning(
                    "Prowlarr %s custom indexer sync failed: %s", inst_key, exc
                )

        for entry in arr_entries:
            app_name = ARR_APP_MAP.get(entry["service"], entry["service"].capitalize())
            arr_host = entry["host"]
            arr_key = entry["api_key"]
            core_services = entry.get("core_services") or []
            tag_ids = []
            if core_services:
                tag_ids = [tag_map[svc] for svc in core_services if svc in tag_map]
            schema = _find_schema(schemas, app_name)
            if not schema:
                logger.warning(
                    "Prowlarr %s could not find schema for %s.", inst_key, app_name
                )
                continue
            try:
                desired = _build_application_payload(
                    schema,
                    app_name,
                    arr_host,
                    arr_key,
                    entry["instance"],
                    tag_ids=tag_ids,
                )
                match = _find_existing_application(
                    existing_apps, desired, schema, arr_host
                )
                if match and _is_application_current(match, desired):
                    logger.debug(
                        "Prowlarr %s application %s (%s) already configured.",
                        inst_key,
                        app_name,
                        entry["instance"],
                    )
                    continue
                api_version = "v1" if entry["service"] == "lidarr" else "v3"
                if not _wait_for_arr(
                    arr_host,
                    arr_key,
                    timeout_s=15,
                    interval_s=2.0,
                    api_version=api_version,
                ):
                    logger.warning(
                        "Arr %s (%s) not ready; skipping Prowlarr app sync.",
                        app_name,
                        entry["instance"],
                    )
                    continue
                ok, app_id = _apply_application(host, token, desired, match)
                if ok:
                    if match:
                        logger.info(
                            "Prowlarr %s updated application %s (%s).",
                            inst_key,
                            app_name,
                            entry["instance"],
                        )
                    else:
                        logger.info(
                            "Prowlarr %s created application %s (%s).",
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
