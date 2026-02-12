"""
Shared conditional dependency map builder.

Used by:
  - main.py                -- startup ordering
  - api/routers/process.py -- /dependency-graph endpoint
"""

from __future__ import annotations

from typing import Callable

from utils.core_services import has_core_service


def _service_has_enabled_instance(config_obj: dict) -> bool:
    """Return True if the service config has at least one enabled instance."""
    if not isinstance(config_obj, dict):
        return False
    if "instances" in config_obj and isinstance(config_obj["instances"], dict):
        return any(
            isinstance(inst, dict) and inst.get("enabled")
            for inst in config_obj["instances"].values()
        )
    return bool(config_obj.get("enabled"))


def _service_has_huntarr_instance(config_obj: dict) -> bool:
    """Return True if the service config has at least one instance with use_huntarr enabled."""
    if not isinstance(config_obj, dict):
        return False
    if "instances" not in config_obj or not isinstance(config_obj["instances"], dict):
        return False
    return any(
        isinstance(inst, dict) and inst.get("enabled") and inst.get("use_huntarr")
        for inst in config_obj["instances"].values()
    )


def build_conditional_dependency_map(
    config_getter: Callable[[str], dict],
) -> dict[str, set[str]]:
    """
    Build a config-aware conditional dependency map.

    Args:
        config_getter: Callable that accepts a service key and returns its
                       config dict.  For main.py this is
                       ``lambda key: config_manager.get(key, {})``.

    Returns:
        Dict mapping ``service_key -> set[dependency_key]``.
        Only includes conditional entries when the upstream service has at
        least one enabled instance.
    """
    deps: dict[str, set[str]] = {
        "riven_backend": {"postgres"},
        "riven_frontend": {"riven_backend"},
        "zilean": {"postgres"},
        "pgadmin": {"postgres"},
    }

    # -- Media-server-conditional deps --
    if _service_has_enabled_instance(config_getter("plex")):
        deps["tautulli"] = {"plex"}
        deps.setdefault("seerr", set()).add("plex")
    if _service_has_enabled_instance(config_getter("jellyfin")):
        deps.setdefault("seerr", set()).add("jellyfin")
    if _service_has_enabled_instance(config_getter("emby")):
        deps.setdefault("seerr", set()).add("emby")

    # -- Prowlarr -> enabled arr services --
    prowlarr_deps: set[str] = set()
    for arr_key in ("sonarr", "radarr", "lidarr", "whisparr"):
        if _service_has_enabled_instance(config_getter(arr_key)):
            prowlarr_deps.add(arr_key)
    if prowlarr_deps:
        deps["prowlarr"] = prowlarr_deps

    # -- Profilarr -> enabled arr services --
    profilarr_deps: set[str] = set()
    for arr_key in ("sonarr", "radarr", "lidarr", "whisparr"):
        if _service_has_enabled_instance(config_getter(arr_key)):
            profilarr_deps.add(arr_key)
    if profilarr_deps:
        deps["profilarr"] = profilarr_deps

    # -- Huntarr -> arr services with use_huntarr flag --
    huntarr_deps: set[str] = set()
    for arr_key in ("sonarr", "radarr", "lidarr", "whisparr"):
        if _service_has_huntarr_instance(config_getter(arr_key)):
            huntarr_deps.add(arr_key)
    if huntarr_deps:
        deps["huntarr"] = huntarr_deps

    # -- Rclone -> provider services based on instance flags --
    rclone_deps: set[str] = set()
    rclone_config = config_getter("rclone")
    rclone_instances = (
        rclone_config.get("instances", {}) or {}
        if isinstance(rclone_config, dict)
        else {}
    )
    for instance in rclone_instances.values():
        if not isinstance(instance, dict) or not instance.get("enabled"):
            continue
        if instance.get("zurg_enabled"):
            rclone_deps.add("zurg")
        if instance.get("decypharr_enabled"):
            rclone_deps.add("decypharr")
        key_type = (instance.get("key_type") or "").lower()
        if key_type == "nzbdav" or has_core_service(instance, "nzbdav"):
            rclone_deps.add("nzbdav")
    if rclone_deps:
        deps["rclone"] = rclone_deps

    return deps


def _get_rclone_instance_deps(instance_config: dict) -> set[str]:
    """Return the conditional provider deps for a single rclone instance config."""
    deps: set[str] = set()
    if not isinstance(instance_config, dict):
        return deps
    if instance_config.get("zurg_enabled"):
        deps.add("zurg")
    if instance_config.get("decypharr_enabled"):
        deps.add("decypharr")
    key_type = (instance_config.get("key_type") or "").lower()
    if key_type == "nzbdav" or has_core_service(instance_config, "nzbdav"):
        deps.add("nzbdav")
    return deps


def filter_conditional_deps_for_instance(
    aggregated_deps: dict[str, set[str]],
    instance_key: str,
    instance_config: dict,
) -> dict[str, set[str]]:
    """
    Filter an aggregated conditional dependency map to a single instance.

    For instance-scoped services (currently rclone), replaces the
    aggregated union of deps with only the deps relevant to the
    specific instance_config.  All other entries pass through unchanged.
    """
    if instance_key != "rclone":
        return aggregated_deps

    filtered = dict(aggregated_deps)
    instance_deps = _get_rclone_instance_deps(instance_config)
    if instance_deps:
        filtered["rclone"] = instance_deps
    else:
        filtered.pop("rclone", None)
    return filtered
