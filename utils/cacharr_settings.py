"""
cacharr_settings.py — Auto-configure Cacharr from DUMB's service registry.

Called by auto_update.py after Cacharr starts.  If PROWLARR_KEY is not
already set in the Cacharr env config, this function reads the API key
from Prowlarr's config.xml and injects it, then returns True so the
caller can restart Cacharr with the correct key.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import os

from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER


def _read_prowlarr_api_key() -> str:
    """Return the Prowlarr API key from the first enabled Prowlarr instance config.xml."""
    prowlarr_cfg = CONFIG_MANAGER.get("prowlarr") or {}
    instances = prowlarr_cfg.get("instances") or {}
    for inst_key, inst in instances.items():
        if not isinstance(inst, dict) or not inst.get("enabled"):
            continue
        config_file = (inst.get("config_file") or "").strip()
        if not config_file or not os.path.isfile(config_file):
            continue
        try:
            tree = ET.parse(config_file)
            node = tree.getroot().find(".//ApiKey")
            if node is not None and (node.text or "").strip():
                return node.text.strip()
        except Exception as exc:
            logger.warning("Cacharr: failed reading Prowlarr ApiKey from %s: %s", config_file, exc)
    return ""


def patch_cacharr_config() -> tuple[bool, str | None]:
    """
    Inject Prowlarr API key into Cacharr's env config when not already set.

    Returns:
        (patched, error) — patched is True if the config was changed and
        Cacharr should be restarted; error is a string on failure.
    """
    cacharr_cfg = CONFIG_MANAGER.get("cacharr") or {}
    if not isinstance(cacharr_cfg, dict) or not cacharr_cfg.get("enabled"):
        return False, None

    env = cacharr_cfg.get("env") or {}
    existing_key = (env.get("PROWLARR_KEY") or "").strip()
    if existing_key:
        logger.debug("Cacharr: PROWLARR_KEY already set; skipping auto-inject.")
        return False, None

    api_key = _read_prowlarr_api_key()
    if not api_key:
        logger.warning("Cacharr: Prowlarr API key not found; PROWLARR_KEY will remain empty.")
        return False, None

    try:
        CONFIG_MANAGER.update("cacharr", {"env": {**env, "PROWLARR_KEY": api_key}})
        logger.info("Cacharr: injected Prowlarr API key into env config.")
        return True, None
    except Exception as exc:
        return False, f"Cacharr config update failed: {exc}"
