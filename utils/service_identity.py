"""Canonical managed-service identities and backward-compatible aliases."""

INFINIDYSK_KEY = "infinidysk"
INFINIDYSK_LEGACY_KEY = "nzbdav"


def canonical_service_key(value):
    """Return the canonical key for a persisted or API-supplied service token."""
    if not isinstance(value, str):
        return value
    normalized = value.strip().lower()
    if normalized == INFINIDYSK_LEGACY_KEY:
        return INFINIDYSK_KEY
    return normalized


def is_infinidysk_key(value):
    return canonical_service_key(value) == INFINIDYSK_KEY
