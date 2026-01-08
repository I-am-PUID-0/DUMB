from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER
import xml.etree.ElementTree as ET
import os


JELLYFIN_PORT_TAGS = (
    "HttpServerPortNumber",
    "PublicPort",
    "HttpPort",
    "InternalHttpPort",
    "PublicHttpPort",
)


def _patch_xml_port(config_path: str, desired_port: int, prefer_network_tags: bool):
    tree = ET.parse(config_path)
    root = tree.getroot()
    desired_text = str(desired_port)
    updated = False
    found_any = False

    for tag in JELLYFIN_PORT_TAGS:
        elem = root.find(tag)
        if elem is None:
            continue
        found_any = True
        if elem.text != desired_text:
            elem.text = desired_text
            updated = True

    if not found_any:
        if prefer_network_tags:
            for tag in ("InternalHttpPort", "PublicHttpPort"):
                elem = ET.SubElement(root, tag)
                elem.text = desired_text
        else:
            for tag in ("HttpServerPortNumber", "PublicPort"):
                elem = ET.SubElement(root, tag)
                elem.text = desired_text
        updated = True

    if updated:
        tree.write(config_path, encoding="utf-8", xml_declaration=True)
    return updated


def patch_jellyfin_config(desired_port: int | None = None):
    config = CONFIG_MANAGER.get("jellyfin", {})
    config_dir = config.get("config_dir", "/jellyfin")
    network_path = os.path.join(config_dir, "config", "network.xml")
    config_paths = [network_path]

    if desired_port is None:
        desired_port = config.get("port")
    if not isinstance(desired_port, int) or desired_port <= 0:
        return False, "Invalid Jellyfin port"

    try:
        any_updated = False
        updated_paths = []
        for path in config_paths:
            if not path:
                continue
            if not os.path.exists(path):
                if os.path.basename(path).lower() != "network.xml":
                    continue
                os.makedirs(os.path.dirname(path), exist_ok=True)
                root = ET.Element(
                    "NetworkConfiguration",
                    attrib={
                        "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
                        "xmlns:xsd": "http://www.w3.org/2001/XMLSchema",
                    },
                )
                for tag, value in (
                    ("BaseUrl", ""),
                    ("EnableHttps", "false"),
                    ("RequireHttps", "false"),
                    ("CertificatePath", ""),
                    ("CertificatePassword", ""),
                    ("InternalHttpPort", str(desired_port)),
                    ("InternalHttpsPort", "8920"),
                    ("PublicHttpPort", str(desired_port)),
                    ("PublicHttpsPort", "8920"),
                    ("AutoDiscovery", "true"),
                    ("EnableUPnP", "false"),
                    ("EnableIPv4", "true"),
                    ("EnableIPv6", "false"),
                    ("EnableRemoteAccess", "true"),
                    ("LocalNetworkSubnets", ""),
                    ("LocalNetworkAddresses", ""),
                    ("KnownProxies", ""),
                    ("IgnoreVirtualInterfaces", "true"),
                    ("VirtualInterfaceNames", None),
                    ("EnablePublishedServerUriByRequest", "false"),
                    ("PublishedServerUriBySubnet", ""),
                    ("RemoteIPFilter", ""),
                    ("IsRemoteIPFilterBlacklist", "false"),
                ):
                    elem = ET.SubElement(root, tag)
                    if tag == "VirtualInterfaceNames":
                        ET.SubElement(elem, "string").text = "veth"
                    else:
                        elem.text = value
                ET.ElementTree(root).write(
                    path, encoding="utf-8", xml_declaration=True
                )
            prefer_network_tags = os.path.basename(path).lower() == "network.xml"
            updated = _patch_xml_port(path, desired_port, prefer_network_tags)
            if updated:
                any_updated = True
                updated_paths.append(path)

        if any_updated:
            logger.info(
                "Jellyfin port set to %s in: %s",
                desired_port,
                ", ".join(updated_paths),
            )
        else:
            logger.debug("Jellyfin port already matches desired value.")
        return any_updated, None
    except ET.ParseError as exc:
        logger.error(f"Error parsing Jellyfin config XML: {exc}")
        return False, str(exc)
    except Exception as exc:
        logger.error(f"Error patching Jellyfin config XML: {exc}")
        return False, str(exc)
