"""Standard release controls, with read compatibility for the retired v2 key.

The major importer owns its fixed artifact internally. These settings select
the active service's normal releases and never authorize a major migration.
"""

import copy


def selected_release(service):
    """Return the effective native selector without resolving a moving tag."""
    if "v2_release_version" in service:
        # Before conversion this was the only effective native selector. The
        # standard fields still described v1 and must not override that choice.
        selected = service["v2_release_version"]
    else:
        enabled = service.get("release_version_enabled", False)
        if type(enabled) is not bool:
            raise ValueError("Release version enabled must be true or false.")
        if not enabled:
            return "latest"
        selected = service.get("release_version", "latest")
    if selected is None or selected == "":
        return "latest"
    if not isinstance(selected, str):
        raise ValueError("Choose latest or an exact official InfiniDysk 2 release.")
    return selected.strip() or "latest"


def set_release_selection(service, selector, *, enabled=None):
    """Publish one selection using the ordinary service configuration fields."""
    if not isinstance(selector, str):
        raise ValueError("Choose latest or an exact official InfiniDysk 2 release.")
    selector = selector.strip() or "latest"
    if enabled is None:
        enabled = selector != "latest"
    if type(enabled) is not bool:
        raise ValueError("Release version enabled must be true or false.")
    service["release_version"] = selector
    service["release_version_enabled"] = enabled
    service.pop("v2_release_version", None)
    return service


def normalized_service(service):
    """Convert the old public key without changing v1's release preferences."""
    normalized = copy.deepcopy(service)
    if "v2_release_version" not in normalized:
        return normalized
    if str(normalized.get("runtime_generation") or "1") == "2":
        set_release_selection(normalized, selected_release(normalized))
    else:
        normalized.pop("v2_release_version")
    return normalized


def normalize_config(config):
    """Normalize only InfiniDysk service entries, not saved recovery payloads."""
    for key in ("infinidysk", "nzbdav"):
        service = config.get(key)
        if isinstance(service, dict) and "v2_release_version" in service:
            normalized = normalized_service(service)
            service.clear()
            service.update(normalized)
    return config
