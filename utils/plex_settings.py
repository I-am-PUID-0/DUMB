from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER
import xml.etree.ElementTree as ET
import os


def patch_plex_config():
    config_path = CONFIG_MANAGER.get("plex", {}).get(
        "config_file", "/plex/Plex Media Server/Preferences.xml"
    )

    if not os.path.exists(config_path):
        logger.warning(f"Plex config file not found at {config_path}")
        return False, "Config file not found"

    try:
        tree = ET.parse(config_path)
        root = tree.getroot()

        updated = False
        desired_friendly_name = CONFIG_MANAGER.get("plex", {}).get(
            "friendly_name", "DUMB"
        )

        if root.attrib.get("FriendlyName") != desired_friendly_name:
            root.set("FriendlyName", desired_friendly_name)
            updated = True

        if updated:
            tree.write(config_path, encoding="utf-8", xml_declaration=True)
            logger.info(
                f"Plex Preferences.xml patched with FriendlyName='{desired_friendly_name}'"
            )
            return True, None
        else:
            logger.info("No changes needed for Plex Preferences.xml")
            return False, None

    except ET.ParseError as e:
        logger.error(f"Error parsing Plex Preferences.xml: {e}")
        return False, str(e)
    except Exception as e:
        logger.error(f"Error patching Plex config: {e}")
        return False, str(e)
