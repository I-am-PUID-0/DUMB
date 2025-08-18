from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER
from collections import OrderedDict
import os, json


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
            "uid": 3001,
            "gid": 3000,
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
        if "debrids" not in config_data or "usenet" not in config_data:
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
        if "arrs" in config_data:
            final_config["arrs"] = config_data["arrs"]
        final_config["repair"] = config_data.get("repair", {})
        final_config["webdav"] = config_data.get("webdav", {})
        if "rclone" in config_data:
            final_config["rclone"] = config_data["rclone"]
        final_config["allowed_file_types"] = config_data.get("allowed_file_types", [])
        final_config["use_auth"] = config_data.get("use_auth", True)

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
