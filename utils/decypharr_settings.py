from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER
from collections import OrderedDict
import os, json, time
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
    Build the arrs[] list from CONFIG_MANAGER instances of sonarr/radarr
    whose `core_service` == "decypharr". Host is always 127.0.0.1 and the
    port is taken from the instance config; token from the instance's config.xml.
    Includes per-instance labeling using instance_name.
    """
    entries = []
    for svc_name in ("sonarr", "radarr"):
        svc_cfg = CONFIG_MANAGER.get(svc_name) or {}
        instances = (svc_cfg.get("instances") or {}) or {}
        for inst_key, inst in instances.items():
            if not inst.get("enabled"):
                continue
            if (inst.get("core_service") or "").lower() != "decypharr":
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return raw.decode("utf-8")


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
        logger.info(f"Created root folder '{path}' on {host}")
        return True
    except urllib.error.HTTPError as e:
        logger.warning(f"HTTP error ensuring rootfolder {path} on {host}: {e}")
    except Exception as e:
        logger.warning(f"Failed to ensure rootfolder {path} on {host}: {e}")
    return False


# --- Download client (qBittorrent) upsert -----------------------------------


def _get_qbt_schema(host: str, key: str):
    schemas = _arr_req(_join(host, "/api/v3/downloadclient/schema"), key, "GET") or []
    for item in schemas:
        impl = (item.get("implementation") or "").lower()
        name = (item.get("implementationName") or "").lower()
        if "qbit" in impl or "qbit" in name:
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


def ensure_decypharr_download_client(
    arr_host: str,
    arr_api_key: str,
    decypharr_port: int,
    service: str,
    name: str = "decypharr",
    category_map: Optional[dict] = None,
    category: Optional[str] = None,
    test_before_save: bool = False,
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

    schema = _get_qbt_schema(arr_host, arr_api_key)
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
            _join(arr_host, "/api/v3/downloadclient/test"), arr_api_key, "POST", desired
        )

    def _verify_saved(client_id: Optional[int]) -> None:
        # Best-effort readback to confirm what Arr persisted.
        if not client_id:
            return
        try:
            saved = (
                _arr_req(
                    _join(arr_host, f"/api/v3/downloadclient/{client_id}"),
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
        _verify_saved(client_id)
        return True, client_id

    created = (
        _arr_req(
            _join(arr_host, "/api/v3/downloadclient"), arr_api_key, "POST", desired
        )
        or {}
    )
    client_id = created.get("id")
    _verify_saved(client_id)
    return True, client_id


# ---------------------------------------------------------------------------
# Resilience helpers (wait + retries)
# ---------------------------------------------------------------------------


def _wait_for_arr(
    host: str, token: str, timeout_s: int = 60, interval_s: float = 2.0
) -> bool:
    """Poll /api/v3/system/status until the Arr instance is responding or timeout."""
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
    """Run fn with retries. Returns fn result or raises last error."""
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


# ---------------------------------------------------------------------------
# Main patch entrypoint
# ---------------------------------------------------------------------------


def patch_decypharr_config():
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

        # Embedded rclone toggle + api_keys map (provider -> api_key)
        use_embedded = bool(decypharr_config.get("use_embedded_rclone", False))
        api_keys_map = {
            (k or "").strip().lower(): (v or "").strip()
            for k, v in (decypharr_config.get("api_keys") or {}).items()
            if (k or "").strip() and (v or "").strip()
        }

        # Default embedded rclone block (merged with any existing)
        default_embedded_rclone = {
            "enabled": True,
            "mount_path": "/mnt/debrid/decypharr",
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
            if inst.get("enabled") and inst.get("core_service") == "decypharr"
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
        if use_embedded:
            embedded_rc = config_data.get("rclone", {})
            merged_rc = {**default_embedded_rclone, **(embedded_rc or {})}
            if embedded_rc != merged_rc:
                config_data["rclone"] = merged_rc
                logger.info(
                    "Ensured default embedded rclone settings in Decypharr config"
                )
                updated = True

        # Bootstrap defaults if config is minimal
        if "debrids" not in config_data and "usenet" not in config_data:
            logger.info(
                "Default Decypharr config detected. Patching extended settings..."
            )

            # Prepare debrids list
            config_data["debrids"] = []

            if use_embedded:
                # Create debrids from api_keys_map (embedded path)
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
                # NON-embedded: derive debrids from rclone instances tied to decypharr
                for inst in debrid_instances:
                    name = (inst.get("key_type", "unknown") or "unknown").lower()
                    api_key = inst.get("api_key", "")
                    rc_url = _safe_extract_rc_url(inst, "http://127.0.0.1:5572")

                    # Legacy per-provider default folders
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

            # Basic download paths
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

        # Embedded mode: synchronize/merge debrids[] from api_keys_map (idempotent)
        if use_embedded:
            if not isinstance(config_data.get("debrids"), list):
                config_data["debrids"] = []

            mount_root = config_data.get("rclone", {}).get(
                "mount_path", default_embedded_rclone["mount_path"]
            )
            existing = {
                (d.get("name", "").lower()): d
                for d in config_data["debrids"]
                if isinstance(d, dict)
            }

            changed = False
            for name_lc, api_key in api_keys_map.items():
                desired_folder = _provider_folder(name_lc, mount_root) + "/"
                d = existing.get(name_lc)
                if not d:
                    d = {
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
                    if d.get("rc_url") != "http://127.0.0.1:5572":
                        d["rc_url"] = "http://127.0.0.1:5572"
                        changed = True

            if changed:
                logger.info("Synchronized Decypharr debrids from embedded api_keys map")
                updated = True

            # Final safety pass to ensure folder layout even if user edited manually
            changed = False
            for d in config_data["debrids"]:
                name_lc = (d.get("name") or "unknown").lower()
                desired_folder = _provider_folder(name_lc, mount_root) + "/"
                if d.get("folder") != desired_folder:
                    d["folder"] = desired_folder
                    changed = True
            if changed:
                logger.info("Adjusted debrid folders for embedded rclone mount layout")
                updated = True

        # ---- Build/Sync arrs from CONFIG_MANAGER (sonarr/radarr) ----
        try:
            desired_arrs = _collect_arr_entries(decypharr_config)
            if desired_arrs:
                if config_data.get("arrs") != desired_arrs:
                    config_data["arrs"] = desired_arrs
                    logger.info("Synchronized arrs (sonarr/radarr) from instances")
                    updated = True
        except Exception as e:
            logger.warning(f"Failed to synchronize arrs: {e}")

        # ---- Ensure Arr root folders (movies/shows symlinks) ----
        try:
            arrs = config_data.get("arrs") or []
            movies_root = "/mnt/debrid/decypharr_symlinks/movies"
            shows_root = "/mnt/debrid/decypharr_symlinks/shows"
            for entry in arrs:
                svc = (entry.get("name") or "").split(":", 1)[0].lower()
                host = entry.get("host")
                token = entry.get("token")
                if not _wait_for_arr(host, token, timeout_s=60, interval_s=2.0):
                    logger.warning(
                        f"Arr not up yet, skipping rootfolder ensure for {host}"
                    )
                    continue
                try:
                    if svc == "radarr":
                        _with_retries(
                            _ensure_arr_rootfolder, host, token, movies_root, attempts=3
                        )
                    elif svc == "sonarr":
                        _with_retries(
                            _ensure_arr_rootfolder, host, token, shows_root, attempts=3
                        )
                except Exception as e:
                    logger.warning(
                        f"Rootfolder ensure failed after retries for {host}: {e}"
                    )
        except Exception as e:
            logger.warning(f"Failed to ensure Arr root folders: {e}")

        # ---- Ensure download client 'decypharr' on each Arr ----
        try:
            arrs = config_data.get("arrs") or []
            for entry in arrs:
                svc = (entry.get("name") or "").split(":", 1)[0].lower()
                host = entry.get("host")
                token = entry.get("token")
                if not _wait_for_arr(host, token, timeout_s=60, interval_s=2.0):
                    logger.warning(
                        f"Arr not up yet, skipping download client ensure for {host}"
                    )
                    continue
                try:
                    _with_retries(
                        ensure_decypharr_download_client,
                        arr_host=host,
                        arr_api_key=token,
                        decypharr_port=int(decypharr_config.get("port", 8282)),
                        service=svc,
                        name="decypharr",
                        category=entry.get("name"),
                        category_map={"radarr": "radarr", "sonarr": "sonarr"},
                        test_before_save=False,
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

        # Usenet remains gated by presence of usenet instances
        if any(
            (inst.get("key_type") or "").lower() == "usenet"
            for inst in rclone_instances.values()
        ):
            final_config["sabnzbd"] = config_data.get("sabnzbd", {})
            final_config["usenet"] = config_data.get("usenet", {})

        # Pass-throughs
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
        for k, v in config_data.items():
            if k not in known_keys:
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
            "/mnt/debrid/decypharr_symlinks/movies",
            "/mnt/debrid/decypharr_symlinks/shows",
        ]
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
