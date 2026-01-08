from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER
import xml.etree.ElementTree as ET
import os


EMBY_PORT_TAGS = (
    "HttpServerPortNumber",
    "PublicPort",
    "HttpPort",
)


def patch_emby_config(desired_port: int | None = None):
    config = CONFIG_MANAGER.get("emby", {})
    config_path = config.get("config_file", "/emby/config/system.xml")
    if not os.path.exists(config_path):
        logger.warning(f"Emby config file not found at {config_path}")
        return False, "Config file not found"

    if desired_port is None:
        desired_port = config.get("port")
    if not isinstance(desired_port, int) or desired_port <= 0:
        return False, "Invalid Emby port"

    try:
        tree = ET.parse(config_path)
        root = tree.getroot()
        desired_text = str(desired_port)
        updated = False
        found_any = False

        for tag in EMBY_PORT_TAGS:
            elem = root.find(tag)
            if elem is None:
                continue
            found_any = True
            if elem.text != desired_text:
                elem.text = desired_text
                updated = True

        if not found_any:
            for tag in ("HttpServerPortNumber", "PublicPort"):
                elem = ET.SubElement(root, tag)
                elem.text = desired_text
            updated = True

        if updated:
            tree.write(config_path, encoding="utf-8", xml_declaration=True)
            logger.info(f"Emby system.xml port set to {desired_text}")
        else:
            logger.debug("Emby system.xml port already matches desired value.")
        return updated, None
    except ET.ParseError as exc:
        logger.error(f"Error parsing Emby system.xml: {exc}")
        return False, str(exc)
    except Exception as exc:
        logger.error(f"Error patching Emby system.xml: {exc}")
        return False, str(exc)
