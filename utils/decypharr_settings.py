from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER
from utils.core_services import get_core_services, has_core_service
from utils.versions import Versions
from collections import OrderedDict
import os, json, time
import threading
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Helpers: provider -> folder mapping
# ---------------------------------------------------------------------------


def _provider_folder(name_lc: str, mount_root: str) -> str:
    """
    Map provider -> expected terminal directory, mirroring NON-embedded layout.
    - realdebrid  -> __all__
    - torbox      -> torrents
    - alldebrid   -> torrents
    - debridlink  -> torrents
    - default     -> other
    """
    if name_lc == "realdebrid":
        return os.path.join(mount_root, name_lc, "__all__")
    elif name_lc in ("torbox", "alldebrid", "debridlink"):
        return os.path.join(mount_root, name_lc, "torrents")
    else:
        return os.path.join(mount_root, name_lc, "other")


def _slugify_category(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    safe = []
    for ch in text:
        if ch.isalnum():
            safe.append(ch)
        elif ch in ("-", "_"):
            safe.append(ch)
        else:
            safe.append("-")
    result = "".join(safe).strip("-")
    while "--" in result:
        result = result.replace("--", "-")
    return result


# ---------------------------------------------------------------------------
# Helpers: Arr discovery & API
# ---------------------------------------------------------------------------


def _parse_arr_api_key(config_xml_path: str) -> str:
    """Best-effort extraction of <ApiKey> from a Sonarr/Radarr config.xml."""
    try:
        if not (config_xml_path and os.path.exists(config_xml_path)):
            return ""
        tree = ET.parse(config_xml_path)
        root = tree.getroot()
        node = root.find(".//ApiKey")
        if node is not None and (node.text or "").strip():
            return node.text.strip()
    except Exception as e:
        logger.warning(f"Failed reading ApiKey from {config_xml_path}: {e}")
    return ""


def _collect_arr_entries(decypharr_cfg: dict) -> list:
    """
    Build the arrs[] list from CONFIG_MANAGER instances of sonarr/radarr/lidarr
    whose core_service list includes "decypharr". Host is always 127.0.0.1 and the
    port is taken from the instance config; token from the instance's config.xml.
    Includes per-instance labeling using instance_name.
    """
    entries = []
    for svc_name in ("sonarr", "radarr", "lidarr", "whisparr"):
        svc_cfg = CONFIG_MANAGER.get(svc_name) or {}
        instances = (svc_cfg.get("instances") or {}) or {}
        for inst_key, inst in instances.items():
            if not inst.get("enabled"):
                continue
            if not has_core_service(inst, "decypharr"):
                continue

            # Port precedence: instance.port or instance.host_port
            port = inst.get("port") or inst.get("host_port")
            try:
                port = str(int(port)) if port is not None else None
            except Exception:
                port = None

            host = f"http://127.0.0.1:{port}" if port else ""

            cfg_path = (inst.get("config_file") or "").strip()
            token = _parse_arr_api_key(cfg_path)

            inst_name = (
                inst.get("instance_name") or inst.get("name") or inst_key or ""
            ).strip()
            label = f"{svc_name}:{inst_name}" if inst_name else svc_name

            entries.append(
                {
                    "name": label,
                    "host": host,
                    "token": token,
                    "download_uncached": bool(
                        decypharr_cfg.get("arrs_download_uncached", False)
                    ),
                    "source": "auto",
                }
            )
    return entries


def _instance_core_services(svc_name: str, instance_name: str) -> list[str]:
    svc_cfg = CONFIG_MANAGER.get(svc_name) or {}
    instances = (svc_cfg.get("instances") or {}) or {}
    target = (instance_name or "").strip().lower()
    if not target:
        return []
    for inst_key, inst in instances.items():
        if not isinstance(inst, dict):
            continue
        candidates = [
            inst_key,
            inst.get("instance_name"),
            inst.get("name"),
        ]
        if any((c or "").strip().lower() == target for c in candidates):
            return get_core_services(inst)
    return []


# --- HTTP helpers for Arr ----------------------------------------------------


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
            logger.warning("Arr API error response from %s: %s", url, body)
        raise


def _normalize_path(p: str) -> str:
    return os.path.normpath((p or "").rstrip("/"))


def _debrid_key(entry: dict) -> str:
    if not isinstance(entry, dict):
        return ""
    return (entry.get("provider") or entry.get("name") or "").strip().lower()


def _has_usenet_providers(config_data: dict) -> bool:
    try:
        providers = (config_data.get("usenet") or {}).get("providers") or []
        return bool([p for p in providers if isinstance(p, dict) and p.get("host")])
    except Exception:
        return False


def _read_decypharr_api_token(config_dir: str) -> str:
    auth_path = os.path.join(config_dir or "/decypharr", "auth.json")
    try:
        if not os.path.exists(auth_path):
            return ""
        with open(auth_path, "r") as handle:
            data = json.load(handle)
        return (data.get("api_token") or "").strip()
    except Exception:
        return ""


def _arr_url(host: str, api_version: str, path: str) -> str:
    return _join(host, f"/api/{api_version}/{path.lstrip('/')}")


def _get_lidarr_rootfolder_payload(host: str, token: str, path: str) -> Optional[dict]:
    quality_profiles = _arr_req(_arr_url(host, "v1", "qualityprofile"), token, "GET") or []
    meta_profiles = _arr_req(_arr_url(host, "v1", "metadataprofile"), token, "GET") or []
    if not meta_profiles:
        meta_profiles = (
            _arr_req(_arr_url(host, "v1", "metadata/profile"), token, "GET") or []
        )
    quality_id = next(
        (p.get("id") for p in quality_profiles if isinstance(p, dict) and p.get("id")),
        None,
    )
    meta_id = next(
        (p.get("id") for p in meta_profiles if isinstance(p, dict) and p.get("id")),
        None,
    )
    if not quality_id or not meta_id:
        logger.warning(
            "Lidarr rootfolder payload missing profiles (quality=%s, metadata=%s)",
            quality_id,
            meta_id,
        )
        return None
    name = os.path.basename(path.rstrip("/")) or path
    return {
        "name": name,
        "path": path,
        "defaultQualityProfileId": quality_id,
        "defaultMetadataProfileId": meta_id,
    }


def _ensure_arr_rootfolder(
    host: str, token: str, path: str, api_version: str = "v3"
) -> bool:
    """Ensure a root folder exists on a Sonarr/Radarr instance.
    Returns True if created, False if already present or on error."""
    if not (host and token and path):
        return False
    try:
        url = _arr_url(host, api_version, "rootfolder")
        existing = _arr_req(url, token, "GET") or []
        if any(
            _normalize_path(x.get("path", "")) == _normalize_path(path)
            for x in (existing or [])
        ):
            return False
        payload = {"path": path}
        if api_version == "v1":
            payload = _get_lidarr_rootfolder_payload(host, token, path) or payload
            if payload == {"path": path}:
                return False
        _arr_req(url, token, "POST", payload)
        logger.info(f"Created root folder '{path}' on {host}")
        return True
    except urllib.error.HTTPError as e:
        logger.warning(f"HTTP error ensuring rootfolder {path} on {host}: {e}")
    except Exception as e:
        logger.warning(f"Failed to ensure rootfolder {path} on {host}: {e}")
    return False


def _coerce_chmod_value(current_value, desired_value: str):
    if isinstance(current_value, int):
        try:
            return int(str(desired_value))
        except ValueError:
            return current_value
    return str(desired_value)


def _ensure_arr_permissions(
    host: str,
    token: str,
    chmod_folder: str = "777",
    set_permissions: bool = True,
    api_version: str = "v3",
    chmod_file: str = "666",
) -> bool:
    if not (host and token):
        return False
    try:
        url = _arr_url(host, api_version, "config/mediamanagement")
        current = _arr_req(url, token, "GET") or {}
        if not isinstance(current, dict):
            return False
        desired = current.copy()
        perms_key = "setPermissions"
        if "setPermissionsLinux" in current and "setPermissions" not in current:
            perms_key = "setPermissionsLinux"
        desired[perms_key] = bool(set_permissions)
        desired["chmodFolder"] = _coerce_chmod_value(
            current.get("chmodFolder"), chmod_folder
        )
        if "chmodFile" in current:
            desired["chmodFile"] = _coerce_chmod_value(
                current.get("chmodFile"), chmod_file
            )
        updated = (
            desired.get(perms_key) != current.get(perms_key)
            or desired.get("chmodFolder") != current.get("chmodFolder")
            or desired.get("chmodFile") != current.get("chmodFile")
        )
        if not updated:
            return False
        _arr_req(url, token, "PUT", desired)
        verify = _arr_req(url, token, "GET") or {}
        if (
            isinstance(verify, dict)
            and verify.get(perms_key) != desired.get(perms_key)
        ):
            logger.warning(
                "Arr permissions update did not persist on %s: %s=%s",
                host,
                perms_key,
                verify.get(perms_key),
            )
        logger.info(
            "Updated Arr permissions on %s: %s=%s chmodFolder=%s",
            host,
            perms_key,
            desired.get(perms_key),
            desired.get("chmodFolder"),
        )
        return True
    except urllib.error.HTTPError as e:
        logger.warning(f"HTTP error updating Arr permissions on {host}: {e}")
    except Exception as e:
        logger.warning(f"Failed to update Arr permissions on {host}: {e}")
    return False


# --- Download client (qBittorrent) upsert -----------------------------------


def _get_qbt_schema(host: str, key: str, api_version: str = "v3"):
    schemas = (
        _arr_req(_arr_url(host, api_version, "downloadclient/schema"), key, "GET")
        or []
    )
    for item in schemas:
        impl = (item.get("implementation") or "").lower()
        name = (item.get("implementationName") or "").lower()
        if "qbit" in impl or "qbit" in name:
            return item
    return schemas[0] if schemas else None


def _get_sab_schema(host: str, key: str, api_version: str = "v3"):
    schemas = (
        _arr_req(_arr_url(host, api_version, "downloadclient/schema"), key, "GET")
        or []
    )
    for item in schemas:
        impl = (item.get("implementation") or "").lower()
        name = (item.get("implementationName") or "").lower()
        if "sab" in impl or "sab" in name:
            return item
    return None


def _build_fields_from_schema(schema: dict, overrides: dict) -> list:
    fields = {}
    for f in schema.get("fields") or []:
        n = f.get("name")
        if not n:
            continue
        fields[n] = f.get("value")
    for k, v in (overrides or {}).items():
        if k in fields:
            fields[k] = v
    return [{"name": k, "value": v} for k, v in fields.items()]


def ensure_decypharr_download_client(
    arr_host: str,
    arr_api_key: str,
    decypharr_port: int,
    service: str,
    name: str = "decypharr",
    category_map: Optional[dict] = None,
    category: Optional[str] = None,
    test_before_save: bool = False,
    api_version: str = "v3",
) -> Tuple[bool, Optional[int]]:
    """Create or update a qBittorrent client named `decypharr` that points to Decypharr.

    Username = Arr host (e.g. http://127.0.0.1:7878)
    Password = Arr ApiKey
    Host = 127.0.0.1
    Port = decypharr_port
    Category = category_map.get(service) or service

    Returns (changed, client_id).
    """

    def _set_field(fields_list, field_name: str, value) -> bool:
        # Set a schema-built field value by case-insensitive name match.
        for f in fields_list or []:
            if (f.get("name") or "").lower() == field_name.lower():
                f["value"] = value
                return True
        return False

    service = (service or "").lower()

    # Fall back to category_map[service] or service if needed.
    effective_category = (category or "").strip() or (category_map or {}).get(
        service, service
    )
    logger.info(
        "Categorizing Decypharr download client for %s as '%s'",
        service,
        effective_category,
    )

    schema = _get_qbt_schema(arr_host, arr_api_key, api_version=api_version)
    if not schema:
        raise RuntimeError("Could not fetch download client schema from Arr")

    # Log schema field names to remove ambiguity about what Arr expects
    fields_schema = schema.get("fields") or []
    field_names_raw = [f.get("name") for f in fields_schema]
    logger.debug("Download client schema fields for %s: %s", arr_host, field_names_raw)

    impl = schema.get("implementation")
    impl_name = schema.get("implementationName")
    contract = schema.get("configContract")
    info_link = schema.get("infoLink")

    overrides = {
        "host": "127.0.0.1",
        "port": int(decypharr_port),
        "useSsl": False,
        "urlBase": "",
        "username": arr_host,
        "password": arr_api_key,
    }

    # Attempt to inject category via schema name(s) if present.
    schema_names = {str(f.get("name") or "").lower() for f in fields_schema}

    category_aliases = [
        "category",
        "torrentcategory",
        "moviecategory",
        "tvcategory",
    ]

    # Prefer setting the canonical field name that actually exists in the schema
    for key in category_aliases:
        if key in schema_names:
            overrides[key] = effective_category

    # Some implementations gate category/tags behind a toggle
    for toggle in ["usecategory", "usetags"]:
        if toggle in schema_names:
            overrides[toggle] = True

    # Build fields using the schema + overrides
    fields = _build_fields_from_schema(schema, overrides)

    # Final guarantee: force-set category/toggles by walking the built fields
    # (covers schema case differences like 'Category' vs 'category').
    _set_field(fields, "useCategory", True)
    _set_field(fields, "useTags", True)

    # Try common category field names
    if not _set_field(fields, "category", effective_category):
        _set_field(fields, "torrentCategory", effective_category)
        _set_field(fields, "movieCategory", effective_category)
        _set_field(fields, "tvCategory", effective_category)

    desired = {
        "name": name,
        "enable": True,
        "protocol": "torrent",
        "priority": 1,
        "removeCompletedDownloads": True,
        "removeFailedDownloads": True,
        "implementation": impl,
        "implementationName": impl_name,
        "configContract": contract,
        "infoLink": info_link,
        "tags": [],
        "fields": fields,
    }

    if test_before_save:
        _arr_req(
            _arr_url(arr_host, api_version, "downloadclient/test"),
            arr_api_key,
            "POST",
            desired,
        )

    def _verify_saved(client_id: Optional[int]) -> None:
        # Best-effort readback to confirm what Arr persisted.
        if not client_id:
            return
        try:
            saved = (
                _arr_req(
                    _arr_url(arr_host, api_version, f"downloadclient/{client_id}"),
                    arr_api_key,
                    "GET",
                )
                or {}
            )
            saved_fields = {
                f.get("name"): f.get("value") for f in (saved.get("fields") or [])
            }
            logger.debug(
                "Saved download client fields for %s: %s", arr_host, saved_fields
            )
        except Exception as e:
            logger.warning(
                "Could not re-read saved download client for verification: %s", e
            )

    existing = (
        _arr_req(
            _arr_url(arr_host, api_version, "downloadclient"), arr_api_key, "GET"
        )
        or []
    )
    match = next(
        (c for c in existing if (c.get("name") or "").lower() == name.lower()), None
    )

    if match:
        client_id = match.get("id")
        put_body = desired.copy()
        put_body["id"] = client_id
        _arr_req(
            _arr_url(arr_host, api_version, f"downloadclient/{client_id}"),
            arr_api_key,
            "PUT",
            put_body,
        )
        _verify_saved(client_id)
        return True, client_id

    created = (
        _arr_req(
            _arr_url(arr_host, api_version, "downloadclient"),
            arr_api_key,
            "POST",
            desired,
        )
        or {}
    )
    client_id = created.get("id")
    _verify_saved(client_id)
    return True, client_id


def ensure_decypharr_sabnzbd_client(
    arr_host: str,
    arr_api_key: str,
    decypharr_port: int,
    api_token: str,
    service: str,
    name: str = "decypharr-usenet",
    category_map: Optional[dict] = None,
    category: Optional[str] = None,
    test_before_save: bool = False,
    api_version: str = "v3",
) -> Tuple[bool, Optional[int]]:
    """Create or update a Sabnzbd client named `decypharr-usenet` that points to Decypharr."""

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

    schema = _get_sab_schema(arr_host, arr_api_key, api_version=api_version)
    if not schema:
        raise RuntimeError("Could not fetch Sabnzbd schema from Arr")

    fields_schema = schema.get("fields") or []
    schema_names = {str(f.get("name") or "").lower() for f in fields_schema}

    overrides = {
        "host": "127.0.0.1",
        "port": int(decypharr_port),
        "useSsl": False,
        "urlBase": "",
        "apiKey": api_token,
    }

    for key, value in overrides.items():
        if key.lower() in schema_names:
            overrides[key] = value

    # Try common category field names
    for key in ["category", "nzbcategory"]:
        if key in schema_names:
            overrides[key] = effective_category

    fields = _build_fields_from_schema(schema, overrides)
    _set_field(fields, "category", effective_category)
    _set_field(fields, "nzbCategory", effective_category)

    desired = {
        "name": name,
        "enable": True,
        "protocol": "usenet",
        "priority": 1,
        "removeCompletedDownloads": True,
        "removeFailedDownloads": True,
        "implementation": schema.get("implementation"),
        "implementationName": schema.get("implementationName"),
        "configContract": schema.get("configContract"),
        "infoLink": schema.get("infoLink"),
        "tags": [],
        "fields": fields,
    }

    if test_before_save:
        _arr_req(
            _arr_url(arr_host, api_version, "downloadclient/test"),
            arr_api_key,
            "POST",
            desired,
        )

    existing = (
        _arr_req(_arr_url(arr_host, api_version, "downloadclient"), arr_api_key, "GET")
        or []
    )
    match = next(
        (c for c in existing if (c.get("name") or "").lower() == name.lower()), None
    )

    if match:
        client_id = match.get("id")
        put_body = desired.copy()
        put_body["id"] = client_id
        _arr_req(
            _arr_url(arr_host, api_version, f"downloadclient/{client_id}"),
            arr_api_key,
            "PUT",
            put_body,
        )
        return True, client_id

    created = (
        _arr_req(
            _arr_url(arr_host, api_version, "downloadclient"),
            arr_api_key,
            "POST",
            desired,
        )
        or {}
    )
    return True, created.get("id")


# ---------------------------------------------------------------------------
# Resilience helpers (wait + retries)
# ---------------------------------------------------------------------------


def _wait_for_arr(
    host: str,
    token: str,
    timeout_s: int = 60,
    interval_s: float = 2.0,
    api_version: str = "v3",
) -> bool:
    """Poll /api/{version}/system/status until the Arr instance is responding or timeout."""
    deadline = time.time() + max(1, timeout_s)
    url = _arr_url(host, api_version, "system/status")
    while time.time() < deadline:
        if _shutdown_requested():
            return False
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
    """Run fn with retries. Returns fn result or raises last error."""
    last_err = None
    delay = max(0.1, base_delay_s)
    for i in range(max(1, attempts)):
        if _shutdown_requested():
            raise RuntimeError("Shutdown requested")
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            if i == attempts - 1:
                break
            time.sleep(delay)
            delay *= backoff
    raise last_err if last_err else RuntimeError("retry failed")


def _wait_for_decypharr(
    host: str, port: int, timeout_s: int = 10, interval_s: float = 1.0
) -> bool:
    deadline = time.time() + max(1, timeout_s)
    url = f"http://{host}:{port}/"
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                resp.read(1)
            return True
        except Exception:
            time.sleep(interval_s)
    return False


_DECYPHARR_RETRY_LOCK = threading.Lock()
_DECYPHARR_RETRY_SCHEDULED = False


def _schedule_decypharr_retry(delay_s: int = 15) -> None:
    global _DECYPHARR_RETRY_SCHEDULED
    with _DECYPHARR_RETRY_LOCK:
        if _DECYPHARR_RETRY_SCHEDULED:
            return
        _DECYPHARR_RETRY_SCHEDULED = True

    def _runner():
        global _DECYPHARR_RETRY_SCHEDULED
        try:
            time.sleep(max(1, delay_s))
            patch_decypharr_config()
        finally:
            with _DECYPHARR_RETRY_LOCK:
                _DECYPHARR_RETRY_SCHEDULED = False

    threading.Thread(target=_runner, daemon=True).start()


def _shutdown_requested() -> bool:
    try:
        from utils.dependencies import get_process_handler

        handler = get_process_handler()
    except Exception:
        return False
    return bool(getattr(handler, "shutting_down", False))


# ---------------------------------------------------------------------------
# Main patch entrypoint
# ---------------------------------------------------------------------------


def patch_decypharr_config():
    if _shutdown_requested():
        return False, "Shutdown requested"
    config_path = CONFIG_MANAGER.get("decypharr", {}).get(
        "config_file", "/decypharr/config.json"
    )

    if not os.path.exists(config_path):
        logger.warning(f"Decypharr config file not found at {config_path}")
        return False, "Config file not found, skipping patching."

    try:
        with open(config_path, "r") as file:
            config_data = json.load(file)

        updated = False
        decypharr_config = CONFIG_MANAGER.get("decypharr", {})
        desired_log_level = decypharr_config.get("log_level") or "INFO"
        desired_port = str(decypharr_config.get("port", 8282))
        user_id = CONFIG_MANAGER.get("puid")
        group_id = CONFIG_MANAGER.get("pgid")
        branch_name = (decypharr_config.get("branch") or "").strip().lower()
        beta_enabled = branch_name == "beta"
        versions = Versions()
        supports_stable_features = False
        latest_release = None
        try:
            repo_owner = decypharr_config.get("repo_owner", "sirrobot01")
            repo_name = decypharr_config.get("repo_name", "decypharr")
            supports_stable_features, latest_release, _ = versions.is_latest_release_gt(
                repo_owner, repo_name, "1.1.6"
            )
        except Exception as e:
            logger.debug("Decypharr release check failed: %s", e)
        features_enabled = beta_enabled or supports_stable_features
        mount_type = (decypharr_config.get("mount_type") or "").strip().lower()
        if features_enabled and not mount_type:
            mount_type = "dfs"
        if not mount_type and not features_enabled:
            # Legacy default when mount_type is unset
            mount_type = "rclone"
        mount_path = (decypharr_config.get("mount_path") or "/mnt/debrid/decypharr").strip()
        logger.info(
            "Decypharr config patch: path=%s beta=%s stable=%s latest=%s mount_type=%s mount_path=%s",
            config_path,
            beta_enabled,
            supports_stable_features,
            latest_release or "unknown",
            mount_type or "(empty)",
            mount_path,
        )
        if features_enabled:
            cfg_updated = False
            if not decypharr_config.get("branch_enabled"):
                decypharr_config["branch_enabled"] = True
                cfg_updated = True
            if (decypharr_config.get("branch") or "").strip() != "beta":
                decypharr_config["branch"] = "beta"
                cfg_updated = True
            if decypharr_config.get("release_version_enabled"):
                decypharr_config["release_version_enabled"] = False
                cfg_updated = True
            if cfg_updated:
                CONFIG_MANAGER.save_config()

        # Embedded rclone toggle + api_keys map (provider -> api_key)
        use_embedded = mount_type == "rclone"
        api_keys_map = {
            (k or "").strip().lower(): (v or "").strip()
            for k, v in (decypharr_config.get("api_keys") or {}).items()
            if (k or "").strip() and (v or "").strip()
        }

        # Default embedded rclone block (merged with any existing)
        default_embedded_rclone = {
            "enabled": True,
            "mount_path": mount_path,
            "vfs_cache_mode": "off",
            "vfs_cache_max_age": "1h",
            "vfs_cache_poll_interval": "1m",
            "vfs_read_chunk_size": "128M",
            "vfs_read_chunk_size_limit": "off",
            "vfs_read_ahead": "128k",
            "uid": user_id,
            "gid": group_id,
            "attr_timeout": "1s",
            "dir_cache_time": "5m",
        }

        # Discover rclone instances (legacy path for NON-embedded mode)
        rclone_instances = (CONFIG_MANAGER.get("rclone") or {}).get(
            "instances", {}
        ) or {}
        debrid_instances = [
            inst
            for inst in rclone_instances.values()
            if inst.get("enabled") and has_core_service(inst, "decypharr")
        ]
        usenet_instance = next(
            (
                inst
                for inst in rclone_instances.values()
                if (inst.get("key_type") or "").lower() == "usenet"
            ),
            None,
        )

        # Helper: extract RC URL from an rclone instance
        def _safe_extract_rc_url(inst, fallback):
            try:
                return extract_rc_url(inst) or fallback
            except Exception:
                return fallback

        usenet_rc_url = _safe_extract_rc_url(usenet_instance, "http://127.0.0.1:5573")

        # Basic fields
        if (config_data.get("log_level") or "").lower() != desired_log_level.lower():
            config_data["log_level"] = desired_log_level.lower()
            logger.info(f"Decypharr log level set to {desired_log_level}")
            updated = True

        if str(config_data.get("port")) != desired_port:
            config_data["port"] = desired_port
            logger.info(f"Decypharr port set to {desired_port}")
            updated = True

        # Ensure embedded rclone block when enabled (merge defaults without clobbering)
        if use_embedded or (features_enabled and mount_type == "rclone"):
            embedded_rc = config_data.get("rclone", {})
            merged_rc = {**default_embedded_rclone, **(embedded_rc or {})}
            if embedded_rc != merged_rc:
                config_data["rclone"] = merged_rc
                logger.info(
                    "Ensured default embedded rclone settings in Decypharr config"
                )
                updated = True

        # Feature mount config (DFS / rclone / external)
        if features_enabled:
            if "mount" in config_data:
                logger.info(
                    "Decypharr config patch: mount key present (type=%s keys=%s)",
                    type(config_data.get("mount")).__name__,
                    list((config_data.get("mount") or {}).keys()),
                )
            else:
                logger.info("Decypharr config patch: mount key missing in config")
            mount_block = config_data.get("mount") or {}
            desired_type = mount_type or (mount_block.get("type") or "dfs")
            desired_mount_path = mount_path
            if mount_block.get("type") != desired_type:
                mount_block["type"] = desired_type
                updated = True
            if mount_block.get("mount_path") != desired_mount_path:
                mount_block["mount_path"] = desired_mount_path
                updated = True
            if desired_type == "dfs":
                dfs_cfg = decypharr_config.get("dfs") or {}
                dfs_defaults = {
                    "cache_dir": "/decypharr/cache/dfs",
                    "chunk_size": "10MB",
                    "disk_cache_size": "50GB",
                    "cache_expiry": "24h",
                    "cache_cleanup_interval": "1h",
                    "daemon_timeout": "30m",
                    "uid": int(user_id) if user_id is not None else 0,
                    "gid": int(group_id) if group_id is not None else 0,
                    "umask": "022",
                    "allow_other": True,
                    "default_permissions": True,
                }
                merged_dfs = {**dfs_defaults, **(mount_block.get("dfs") or {}), **dfs_cfg}
                if mount_block.get("dfs") != merged_dfs:
                    mount_block["dfs"] = merged_dfs
                    updated = True
            elif desired_type == "rclone":
                embedded_rc = config_data.get("rclone", {})
                merged_rc = {**default_embedded_rclone, **(embedded_rc or {})}
                if mount_block.get("rclone") != merged_rc:
                    mount_block["rclone"] = merged_rc
                    updated = True
            elif desired_type == "external_rclone":
                rc_inst = debrid_instances[0] if debrid_instances else None
                rc_url = _safe_extract_rc_url(rc_inst, "http://127.0.0.1:5572")
                external_rc = mount_block.get("external_rclone") or {}
                desired_external = {
                    "rc_url": rc_url,
                    "rc_username": external_rc.get("rc_username", ""),
                    "rc_password": external_rc.get("rc_password", ""),
                }
                if external_rc != desired_external:
                    mount_block["external_rclone"] = desired_external
                    updated = True
            config_data["mount"] = mount_block
            if not config_data.get("download_folder"):
                config_data["download_folder"] = "/mnt/debrid/decypharr_downloads"
                updated = True

        # Bootstrap defaults if config is minimal
        if "debrids" not in config_data and "usenet" not in config_data:
            logger.info(
                "Default Decypharr config detected. Patching extended settings..."
            )

            if features_enabled:
                config_data["debrids"] = []
                for name_lc, api_key in api_keys_map.items():
                    config_data["debrids"].append(
                        {
                            "provider": name_lc,
                            "name": name_lc,
                            "api_key": api_key,
                            "download_api_keys": [api_key],
                            "rate_limit": "250/minute",
                            "torrents_refresh_interval": "10m",
                            "download_links_refresh_interval": "5m",
                            "workers": 4000,
                            "auto_expire_links_after": "3d",
                        }
                    )

                # Mount configuration (features)
                mount_block = {
                    "type": mount_type or "dfs",
                    "mount_path": mount_path,
                }
                if mount_block["type"] == "dfs":
                    dfs_cfg = decypharr_config.get("dfs") or {}
                    dfs_defaults = {
                        "cache_dir": "/decypharr/cache/dfs",
                        "chunk_size": "10MB",
                        "disk_cache_size": "50GB",
                        "cache_expiry": "24h",
                        "cache_cleanup_interval": "1h",
                        "daemon_timeout": "30m",
                        "uid": int(user_id) if user_id is not None else 0,
                        "gid": int(group_id) if group_id is not None else 0,
                        "umask": "022",
                        "allow_other": True,
                        "default_permissions": True,
                    }
                    mount_block["dfs"] = {**dfs_defaults, **dfs_cfg}
                elif mount_block["type"] == "rclone":
                    embedded_rc = config_data.get("rclone", {})
                    merged_rc = {**default_embedded_rclone, **(embedded_rc or {})}
                    mount_block["rclone"] = merged_rc

                config_data["mount"] = mount_block
                config_data["download_folder"] = "/mnt/debrid/decypharr_downloads"
            else:
                # Legacy (non-beta) defaults
                config_data["debrids"] = []

                if use_embedded:
                    mount_root = config_data.get("rclone", {}).get(
                        "mount_path", default_embedded_rclone["mount_path"]
                    )
                    for name_lc, api_key in api_keys_map.items():
                        folder = _provider_folder(name_lc, mount_root) + "/"
                        config_data["debrids"].append(
                            {
                                "name": name_lc,
                                "api_key": api_key,
                                "download_api_keys": [api_key],
                                "folder": folder,
                                "rate_limit": "250/minute",
                                "use_webdav": True,
                                "torrents_refresh_interval": "15s",
                                "download_links_refresh_interval": "40m",
                                "workers": 50,
                                "auto_expire_links_after": "3d",
                                "folder_naming": "original_no_ext",
                                "rc_url": "",  # embedded rclone -> no external RC needed
                            }
                        )
                else:
                    for inst in debrid_instances:
                        name = (inst.get("key_type", "unknown") or "unknown").lower()
                        api_key = inst.get("api_key", "")
                        rc_url = _safe_extract_rc_url(inst, "http://127.0.0.1:5572")

                        if name == "realdebrid":
                            folder = "/mnt/debrid/decypharr_realdebrid/__all__"
                        elif name == "torbox":
                            folder = "/mnt/debrid/decypharr_torbox/torrents"
                        elif name == "alldebrid":
                            folder = "/mnt/debrid/decypharr_alldebrid/torrents/"
                        elif name == "debridlink":
                            folder = "/mnt/debrid/decypharr_debridlink/torrents/"
                        else:
                            folder = "/mnt/debrid/decypharr_other"

                        config_data["debrids"].append(
                            {
                                "name": name,
                                "api_key": api_key,
                                "download_api_keys": [api_key] if api_key else [],
                                "folder": folder,
                                "rate_limit": "250/minute",
                                "use_webdav": True,
                                "torrents_refresh_interval": "15s",
                                "download_links_refresh_interval": "40m",
                                "workers": 50,
                                "auto_expire_links_after": "3d",
                                "folder_naming": "original_no_ext",
                                "rc_url": rc_url,
                            }
                        )

                config_data["qbittorrent"] = {
                    "download_folder": "/mnt/debrid/decypharr_downloads"
                }
                config_data["sabnzbd"] = {
                    "download_folder": "/mnt/debrid/decypharr_downloads"
                }
                config_data["usenet"] = {
                    "mount_folder": "/mnt/debrid/decypharr_usenet/__all__",
                    "chunks": 15,
                    "rc_url": usenet_rc_url,
                }
            updated = True

        # Feature mode: synchronize/merge debrids[] from api_keys_map (idempotent)
        if features_enabled:
            if not isinstance(config_data.get("debrids"), list):
                config_data["debrids"] = []

            existing = {
                _debrid_key(d): d
                for d in config_data["debrids"]
                if isinstance(d, dict)
            }

            changed = False
            for name_lc, api_key in api_keys_map.items():
                d = existing.get(name_lc)
                if not d:
                    d = {
                        "provider": name_lc,
                        "name": name_lc,
                        "api_key": api_key,
                        "download_api_keys": [api_key],
                        "rate_limit": "250/minute",
                        "torrents_refresh_interval": "10m",
                        "download_links_refresh_interval": "5m",
                        "workers": 4000,
                        "auto_expire_links_after": "3d",
                    }
                    config_data["debrids"].append(d)
                    existing[name_lc] = d
                    changed = True
                else:
                    if d.get("provider") != name_lc:
                        d["provider"] = name_lc
                        changed = True
                    if d.get("name") != name_lc:
                        d["name"] = name_lc
                        changed = True
                    if d.get("api_key") != api_key:
                        d["api_key"] = api_key
                        changed = True
                    dl_keys = set(d.get("download_api_keys") or [])
                    if api_key and api_key not in dl_keys:
                        dl_keys.add(api_key)
                        d["download_api_keys"] = list(dl_keys)
                        changed = True
                    if not d.get("rate_limit"):
                        d["rate_limit"] = "250/minute"
                        changed = True
                    if not d.get("torrents_refresh_interval"):
                        d["torrents_refresh_interval"] = "10m"
                        changed = True
                    if not d.get("download_links_refresh_interval"):
                        d["download_links_refresh_interval"] = "5m"
                        changed = True
                    if not d.get("workers"):
                        d["workers"] = 4000
                        changed = True
                    if not d.get("auto_expire_links_after"):
                        d["auto_expire_links_after"] = "3d"
                        changed = True

            if changed:
                logger.info("Synchronized Decypharr debrids from feature api_keys map")
                updated = True

        # Embedded mode: synchronize/merge debrids[] from api_keys_map (idempotent)
        elif use_embedded:
            if not isinstance(config_data.get("debrids"), list):
                config_data["debrids"] = []

            mount_root = (
                mount_path
                if beta_enabled
                else config_data.get("rclone", {}).get(
                    "mount_path", default_embedded_rclone["mount_path"]
                )
            )
            existing = {
                _debrid_key(d): d
                for d in config_data["debrids"]
                if isinstance(d, dict)
            }

            changed = False
            for name_lc, api_key in api_keys_map.items():
                desired_folder = _provider_folder(name_lc, mount_root) + "/"
                d = existing.get(name_lc)
                if not d:
                    d = {
                        "provider": name_lc,
                        "name": name_lc,
                        "api_key": api_key,
                        "download_api_keys": [api_key],
                        "folder": desired_folder,
                        "rate_limit": "250/minute",
                        "use_webdav": True,
                        "torrents_refresh_interval": "15s",
                        "download_links_refresh_interval": "40m",
                        "workers": 50,
                        "auto_expire_links_after": "3d",
                        "folder_naming": "original_no_ext",
                        "rc_url": "",
                    }
                    config_data["debrids"].append(d)
                    existing[name_lc] = d
                    changed = True
                else:
                    if d.get("provider") != name_lc:
                        d["provider"] = name_lc
                        changed = True
                    if d.get("api_key") != api_key:
                        d["api_key"] = api_key
                        changed = True
                    dl_keys = set(d.get("download_api_keys") or [])
                    if api_key not in dl_keys:
                        dl_keys.add(api_key)
                        d["download_api_keys"] = list(dl_keys)
                        changed = True
                    if d.get("folder") != desired_folder:
                        d["folder"] = desired_folder
                        changed = True
                    if d.get("use_webdav") is not True:
                        d["use_webdav"] = True
                        changed = True
                    if not beta_enabled and d.get("rc_url") != "http://127.0.0.1:5572":
                        d["rc_url"] = "http://127.0.0.1:5572"
                        changed = True

            if changed:
                logger.info("Synchronized Decypharr debrids from embedded api_keys map")
                updated = True

            # Final safety pass to ensure folder layout even if user edited manually
            changed = False
            for d in config_data["debrids"]:
                name_lc = _debrid_key(d) or "unknown"
                desired_folder = _provider_folder(name_lc, mount_root) + "/"
                if d.get("folder") != desired_folder:
                    d["folder"] = desired_folder
                    changed = True
            if changed:
                logger.info("Adjusted debrid folders for embedded rclone mount layout")
                updated = True

        # ---- Build/Sync arrs from CONFIG_MANAGER (sonarr/radarr) ----
        desired_arrs = []
        try:
            desired_arrs = _collect_arr_entries(decypharr_config)
            if desired_arrs:
                if config_data.get("arrs") != desired_arrs:
                    config_data["arrs"] = desired_arrs
                    logger.info("Synchronized arrs (sonarr/radarr) from instances")
                    updated = True
        except Exception as e:
            logger.warning(f"Failed to synchronize arrs: {e}")

        # ---- Ensure Arr root folders (combined symlinks) ----
        root_paths = []
        try:
            arrs = desired_arrs or []
            for entry in arrs:
                name_label = (entry.get("name") or "")
                svc, _, instance_name = name_label.partition(":")
                svc = svc.strip().lower()
                instance_name = instance_name.strip()
                core_services = _instance_core_services(svc, instance_name)
                use_combined = "decypharr" in core_services and "nzbdav" in core_services
                base_root = (
                    "/mnt/debrid/combined_symlinks"
                    if use_combined
                    else "/mnt/debrid/decypharr_symlinks"
                )
                logger.info(
                    "Arr rootfolder base for %s:%s set to %s (combined=%s)",
                    svc,
                    instance_name or "default",
                    base_root,
                    use_combined,
                )
                instance_slug = _slugify_category(f"{svc}-{instance_name}") or _slugify_category(
                    svc
                )
                root_path = f"{base_root}/{instance_slug}"
                root_paths.append(root_path)
                try:
                    if not os.path.exists(root_path):
                        os.makedirs(root_path, exist_ok=True)
                        logger.info(f"Created directory: {root_path}")
                    if user_id is not None and group_id is not None:
                        os.chown(root_path, int(user_id), int(group_id))
                    os.chmod(root_path, 0o777)
                except Exception as e:
                    logger.warning(
                        f"Failed to ensure rootfolder directory {root_path}: {e}"
                    )
                host = entry.get("host")
                token = entry.get("token")
                api_version = "v1" if svc == "lidarr" else "v3"
                if not _wait_for_arr(
                    host, token, timeout_s=60, interval_s=2.0, api_version=api_version
                ):
                    logger.warning(
                        f"Arr not up yet, skipping rootfolder ensure for {host}"
                    )
                    continue
                try:
                    if svc in ("radarr", "sonarr", "lidarr", "whisparr"):
                        _with_retries(
                            _ensure_arr_permissions,
                            host,
                            token,
                            attempts=3,
                            api_version=api_version,
                        )
                        _with_retries(
                            _ensure_arr_rootfolder,
                            host,
                            token,
                            root_path,
                            attempts=3,
                            api_version=api_version,
                        )
                except Exception as e:
                    logger.warning(
                        f"Rootfolder ensure failed after retries for {host}: {e}"
                    )
        except Exception as e:
            logger.warning(f"Failed to ensure Arr root folders: {e}")

        # ---- Ensure download client 'decypharr' on each Arr ----
        try:
            arrs = desired_arrs or []
            decypharr_port = int(decypharr_config.get("port", 8282))
            usenet_enabled = _has_usenet_providers(config_data)
            api_token = _read_decypharr_api_token(
                decypharr_config.get("config_dir", "/decypharr")
            )
            if not _wait_for_decypharr(
                "127.0.0.1", decypharr_port, timeout_s=10, interval_s=1.0
            ):
                logger.warning(
                    "Decypharr backend not reachable on %s:%s; skipping download client setup.",
                    "127.0.0.1",
                    decypharr_port,
                )
                _schedule_decypharr_retry()
            else:
                for entry in arrs:
                    svc = (entry.get("name") or "").split(":", 1)[0].lower()
                    host = entry.get("host")
                    token = entry.get("token")
                    api_version = "v1" if svc == "lidarr" else "v3"
                    if not _wait_for_arr(
                        host, token, timeout_s=60, interval_s=2.0, api_version=api_version
                    ):
                        logger.warning(
                            f"Arr not up yet, skipping download client ensure for {host}"
                        )
                        continue
                    try:
                        _with_retries(
                            ensure_decypharr_download_client,
                            arr_host=host,
                            arr_api_key=token,
                            decypharr_port=decypharr_port,
                            service=svc,
                            name="decypharr",
                            category=entry.get("name"),
                            category_map={
                                "radarr": "radarr",
                                "sonarr": "sonarr",
                                "lidarr": "lidarr",
                                "whisparr": "whisparr",
                            },
                            test_before_save=False,
                            api_version=api_version,
                            attempts=3,
                        )
                        if usenet_enabled:
                            if not api_token:
                                logger.warning(
                                    "Decypharr API token missing; skipping Sabnzbd client for %s",
                                    host,
                                )
                            else:
                                _with_retries(
                                    ensure_decypharr_sabnzbd_client,
                                    arr_host=host,
                                    arr_api_key=token,
                                    decypharr_port=decypharr_port,
                                    api_token=api_token,
                                    service=svc,
                                    name="decypharr-usenet",
                                    category=entry.get("name"),
                                    category_map={
                                        "radarr": "radarr",
                                        "sonarr": "sonarr",
                                        "lidarr": "lidarr",
                                        "whisparr": "whisparr",
                                    },
                                    test_before_save=False,
                                    api_version=api_version,
                                    attempts=3,
                                )
                    except Exception as e:
                        logger.warning(
                            f"Download client ensure failed after retries for {host}: {e}"
                        )
        except Exception as e:
            logger.warning(f"Failed to ensure Decypharr download client on Arr: {e}")

        # ----- Build final_config (ordered) -----
        final_config = OrderedDict()
        final_config["url_base"] = config_data.get("url_base", "/")
        final_config["port"] = config_data.get("port", "8282")
        final_config["log_level"] = config_data.get("log_level", "INFO")

        if features_enabled:
            # Ensure a mount block is always present for feature configs
            mount_block = config_data.get("mount") or {}
            if not mount_block:
                mount_block = {
                    "type": mount_type or "dfs",
                    "mount_path": mount_path,
                }
                if mount_block["type"] == "dfs":
                    dfs_cfg = decypharr_config.get("dfs") or {}
                    dfs_defaults = {
                        "cache_dir": "/decypharr/cache/dfs",
                        "chunk_size": "10MB",
                        "disk_cache_size": "50GB",
                        "cache_expiry": "24h",
                        "cache_cleanup_interval": "1h",
                        "daemon_timeout": "30m",
                        "uid": int(user_id) if user_id is not None else 0,
                        "gid": int(group_id) if group_id is not None else 0,
                        "umask": "022",
                        "allow_other": True,
                        "default_permissions": True,
                    }
                    mount_block["dfs"] = {**dfs_defaults, **dfs_cfg}
                elif mount_block["type"] == "rclone":
                    embedded_rc = config_data.get("rclone", {})
                    merged_rc = {**default_embedded_rclone, **(embedded_rc or {})}
                    mount_block["rclone"] = merged_rc
                elif mount_block["type"] == "external_rclone":
                    rc_inst = debrid_instances[0] if debrid_instances else None
                    rc_url = _safe_extract_rc_url(rc_inst, "http://127.0.0.1:5572")
                    mount_block["external_rclone"] = {
                        "rc_url": rc_url,
                        "rc_username": "",
                        "rc_password": "",
                    }
                config_data["mount"] = mount_block
                updated = True
                logger.info("Injected missing feature mount block into Decypharr config")

            if config_data.get("download_folder"):
                final_config["download_folder"] = config_data["download_folder"]
            if "debrids" in config_data:
                final_config["debrids"] = config_data.get("debrids", [])
            if config_data.get("mount"):
                final_config["mount"] = config_data.get("mount", {})
            logger.info(
                "Decypharr config patch: final_config mount present=%s",
                "mount" in final_config,
            )
            if config_data.get("arrs"):
                final_config["arrs"] = config_data["arrs"]
            final_config["repair"] = config_data.get("repair", {})
            if config_data.get("usenet"):
                final_config["usenet"] = config_data.get("usenet", {})
            final_config["allowed_file_types"] = config_data.get("allowed_file_types", [])
        else:
            # Include debrids when:
            #   - any non-usenet rclone instances exist, OR
            #   - embedded mode is enabled and we have debrids configured
            has_non_usenet_instances = any(
                (inst.get("key_type") or "").lower() != "usenet"
                for inst in rclone_instances.values()
            )
            if has_non_usenet_instances or (use_embedded and config_data.get("debrids")):
                final_config["debrids"] = config_data.get("debrids", [])
                final_config["qbittorrent"] = config_data.get("qbittorrent", {})

            if any(
                (inst.get("key_type") or "").lower() == "usenet"
                for inst in rclone_instances.values()
            ):
                final_config["sabnzbd"] = config_data.get("sabnzbd", {})
                final_config["usenet"] = config_data.get("usenet", {})

            if config_data.get("arrs"):
                final_config["arrs"] = config_data["arrs"]
            final_config["repair"] = config_data.get("repair", {})
            final_config["webdav"] = config_data.get("webdav", {})
            if "rclone" in config_data:
                final_config["rclone"] = config_data["rclone"]
            final_config["allowed_file_types"] = config_data.get("allowed_file_types", [])
            # final_config["use_auth"] = config_data.get("use_auth", True)

        # ----- Preserve any extra keys Decypharr/user added -----
        known_keys = set(final_config.keys())
        skip_keys = {"rclone", "qbittorrent", "sabnzbd"} if features_enabled else set()
        for k, v in config_data.items():
            if k not in known_keys and k not in skip_keys:
                final_config[k] = v

        # Persist if changed
        if updated or final_config != config_data:
            with open(config_path, "w") as file:
                json.dump(final_config, file, indent=4)
            logger.info("Decypharr config.json patched with extended settings")
            updated = True
        else:
            logger.info("No changes needed for Decypharr config.json")

        # Ensure directories exist & ownership is correct
        required_dirs = [
            "/mnt/debrid/decypharr_downloads",
            "/mnt/debrid/decypharr_symlinks",
        ]
        required_dirs.extend(root_paths or [])
        if use_embedded:
            required_dirs.append(
                config_data.get("rclone", {}).get(
                    "mount_path", default_embedded_rclone["mount_path"]
                )
            )

        for dir_path in required_dirs:
            try:
                if not os.path.exists(dir_path):
                    os.makedirs(dir_path, exist_ok=True)
                    logger.info(f"Created directory: {dir_path}")
                if user_id is not None and group_id is not None:
                    os.chown(dir_path, int(user_id), int(group_id))
            except Exception as e:
                logger.warning(
                    f"Failed to ensure ownership or creation of {dir_path}: {e}"
                )

        return (True, None) if updated else (False, None)

    except Exception as e:
        logger.error(f"Error patching Decypharr config: {e}")
        return False, str(e)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def extract_rc_url(instance):
    if not instance:
        return None

    command = instance.get("command")
    if not isinstance(command, list):
        return None

    try:
        rc_index = command.index("--rc-addr")
        port_part = command[rc_index + 1]
        if port_part.startswith(":"):
            return f"http://127.0.0.1{port_part}"
    except (ValueError, IndexError):
        pass

    return None
