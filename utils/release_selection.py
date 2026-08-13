def normalize_release_selectors(value):
    """Normalize enabled blank release selectors to the moving latest channel."""
    if isinstance(value, dict):
        if (
            value.get("release_version_enabled") is True
            and not str(value.get("release_version") or "").strip()
        ):
            value["release_version"] = "latest"
        for child in value.values():
            normalize_release_selectors(child)
    elif isinstance(value, list):
        for child in value:
            normalize_release_selectors(child)
    return value
