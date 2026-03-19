"""
cacharr_settings.py — Auto-configure Cacharr from DUMB's service registry.

Called by auto_update.py after Cacharr starts.  If PROWLARR_KEY is not
already set in the Cacharr env config, this function reads the API key
and base URL from Prowlarr's config.xml / instance port, then returns True
so the caller can restart Cacharr with the correct credentials.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import os

from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER


def _discover_prowlarr() -> tuple[str, str]:
    """Return (api_key, base_url) from the first enabled Prowlarr instance.

    Reads the API key from the instance's config.xml and constructs the
    base URL from the configured port (default 9696).  Returns ("", "")
    when no enabled instance with a readable key is found.
    """
    prowlarr_cfg = CONFIG_MANAGER.get("prowlarr") or {}
    instances = prowlarr_cfg.get("instances") or {}
    for inst_key, inst in instances.items():
        if not isinstance(inst, dict) or not inst.get("enabled"):
            continue
        config_file = (inst.get("config_file") or "").strip()
        api_key = ""
        if config_file and os.path.isfile(config_file):
            try:
                tree = ET.parse(config_file)
                node = tree.getroot().find(".//ApiKey")
                if node is not None and (node.text or "").strip():
                    api_key = node.text.strip()
            except Exception as exc:
                logger.warning(
                    "Cacharr: failed reading Prowlarr ApiKey from %s: %s",
                    config_file, exc,
                )
        if not api_key:
            continue
        port = inst.get("port") or 9696
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 9696
        base_url = f"http://127.0.0.1:{port}"
        return api_key, base_url
    return "", ""


def patch_cacharr_config() -> tuple[bool, str | None]:
    """Inject Prowlarr API key and URL into Cacharr's env config when not already set.

    Returns:
        (patched, error) — patched is True if the config was changed and
        Cacharr should be restarted; error is a non-empty string on failure.
    """
    cacharr_cfg = CONFIG_MANAGER.get("cacharr") or {}
    if not isinstance(cacharr_cfg, dict) or not cacharr_cfg.get("enabled"):
        return False, None

    env = cacharr_cfg.get("env") or {}
    existing_key = (env.get("PROWLARR_KEY") or "").strip()
    if existing_key:
        logger.debug("Cacharr: PROWLARR_KEY already set; skipping auto-inject.")
        return False, None

    api_key, base_url = _discover_prowlarr()
    if not api_key:
        logger.warning("Cacharr: Prowlarr API key not found; PROWLARR_KEY will remain empty.")
        return False, None

    updates: dict = {"PROWLARR_KEY": api_key}
    if base_url and not (env.get("PROWLARR_URL") or "").strip():
        updates["PROWLARR_URL"] = base_url

    try:
        CONFIG_MANAGER.update("cacharr", {"env": {**env, **updates}})
        logger.info(
            "Cacharr: injected Prowlarr credentials (url=%s).", base_url or "unchanged"
        )
        return True, None
    except Exception as exc:
        return False, f"Cacharr config update failed: {exc}"
