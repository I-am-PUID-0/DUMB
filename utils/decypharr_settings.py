from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER
import json, os


def patch_decypharr_config():
    config_path = CONFIG_MANAGER.get("decypharr", {}).get(
        "config_file", "/decypharr/config.json"
    )

    if not os.path.exists(config_path):
        logger.warning(f"Decypharr config file not found at {config_path}")
        return

    try:
        with open(config_path, "r") as file:
            config_data = json.load(file)

        updated = False
        decypharr_config = CONFIG_MANAGER.get("decypharr", {})
        desired_log_level = decypharr_config.get("log_level", "INFO")
        desired_port = str(decypharr_config.get("port", 8282))

        if config_data.get("log_level") != desired_log_level:
            config_data["log_level"] = desired_log_level
            updated = True

        if config_data.get("port") != desired_port:
            config_data["port"] = desired_port
            updated = True

        if updated:
            with open(config_path, "w") as file:
                json.dump(config_data, file, indent=4)
            logger.info("Decypharr config.json patched with new settings")
            return True, None
        else:
            logger.info("No changes needed for Decypharr config.json")
            return False, None
    except Exception as e:
        logger.error(f"Error patching Decypharr config: {e}")
