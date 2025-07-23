from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER
from collections import OrderedDict
import os, json


def patch_decypharr_config():
    config_path = CONFIG_MANAGER.get("decypharr", {}).get(
        "config_file", "/decypharr/config.json"
    )

    if not os.path.exists(config_path):
        logger.warning(f"Decypharr config file not found at {config_path}")
        return True, "Config file not found, skipping patching."

    try:
        with open(config_path, "r") as file:
            config_data = json.load(file)

        updated = False
        decypharr_config = CONFIG_MANAGER.get("decypharr", {})
        desired_log_level = decypharr_config.get("log_level", "INFO")
        desired_port = str(decypharr_config.get("port", 8282))
        debrid_service = decypharr_config.get("debrid_service", "realdebrid")
        api_key = decypharr_config.get("api_key", "")
        user_id = CONFIG_MANAGER.get("puid")
        group_id = CONFIG_MANAGER.get("pgid")

        if config_data.get("log_level") != desired_log_level.lower():
            config_data["log_level"] = desired_log_level.lower()
            logger.info(f"Decypharr log level set to {desired_log_level}")
            updated = True

        if config_data.get("port") != desired_port:
            config_data["port"] = desired_port
            logger.info(f"Decypharr port set to {desired_port}")
            updated = True

        qb = config_data.get("qbittorrent", {})
        if qb.get("download_folder") == "/decypharr/downloads":
            logger.info(
                "Default Decypharr config detected. Patching extended settings..."
            )

            config_data["debrids"] = [
                {
                    "name": debrid_service,
                    "api_key": api_key,
                    "download_api_keys": [api_key],
                    "folder": "/mnt/debrid/decypharr/__all__",
                    "rate_limit": "250/minute",
                    "use_webdav": True,
                    "torrents_refresh_interval": "15s",
                    "download_links_refresh_interval": "40m",
                    "workers": 50,
                    "auto_expire_links_after": "3d",
                    "folder_naming": "original_no_ext",
                    "rc_url": "http://127.0.0.1:5572",
                }
            ]

            config_data["qbittorrent"][
                "download_folder"
            ] = "/mnt/debrid/decypharr_downloads"

            final_config = OrderedDict()
            final_config["url_base"] = config_data.get("url_base", "/")
            final_config["port"] = config_data.get("port", "8282")
            final_config["log_level"] = config_data.get("log_level", "INFO")
            final_config["debrids"] = config_data.get("debrids", [])
            final_config["qbittorrent"] = config_data.get("qbittorrent", {})

            if "arrs" in config_data:
                final_config["arrs"] = config_data["arrs"]

            final_config["repair"] = config_data.get("repair", {})
            final_config["webdav"] = config_data.get("webdav", {})
            final_config["allowed_file_types"] = config_data.get(
                "allowed_file_types", []
            )
            final_config["use_auth"] = config_data.get("use_auth", True)

            with open(config_path, "w") as file:
                json.dump(final_config, file, indent=4)
            logger.info("Decypharr config.json patched with extended settings")

            required_dirs = [
                "/mnt/debrid/decypharr_downloads",
                "/mnt/debrid/decypharr_symlinks",
                "/mnt/debrid/decypharr_symlinks/movies",
                "/mnt/debrid/decypharr_symlinks/shows",
            ]
            for dir_path in required_dirs:
                try:
                    if not os.path.exists(dir_path):
                        os.makedirs(dir_path)
                        logger.info(f"Created directory: {dir_path}")
                    if user_id is not None and group_id is not None:
                        os.chown(dir_path, int(user_id), int(group_id))
                except Exception as e:
                    logger.warning(
                        f"Failed to ensure ownership or creation of {dir_path}: {e}"
                    )
            logger.info("Required default directories for Decypharr created")
            updated = True

        if updated:
            logger.info("Decypharr config.json patched with new settings")
            return True, None
        else:
            logger.info("No changes needed for Decypharr config.json")
            return False, None
    except Exception as e:
        logger.error(f"Error patching Decypharr config: {e}")
        return False, str(e)
