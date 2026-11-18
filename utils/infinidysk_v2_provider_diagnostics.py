"""Bounded, credential-free NNTP observations retained with a migration job."""

MAX_PROVIDERS = 64
MAX_COUNTER = 2**53 - 1
PROVIDER_FAILURE_CATEGORIES = frozenset(
    {
        "dns",
        "connect",
        "tls",
        "authentication",
        "connection_limit",
        "timeout",
        "connection_closed",
        "protocol",
        "response_limit",
        "provider_reply",
        "article_missing",
        "bridge_capacity",
        "bridge_handshake",
    }
)


def _counter(value):
    return type(value) is int and 0 <= value <= MAX_COUNTER


def _failures(value):
    if not isinstance(value, dict):
        return None
    selected = {}
    for key in sorted(PROVIDER_FAILURE_CATEGORIES):
        if key in value:
            count = value[key]
            if not _counter(count):
                return None
            if count:
                selected[key] = count
    return selected


def sanitize_provider_diagnostics(value):
    """Copy only the protocol's fixed keys and enums; never relay exception text."""
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or not isinstance(value.get("providers"), list)
        or len(value["providers"]) > MAX_PROVIDERS
    ):
        return None
    providers, seen = [], set()
    for row in value["providers"]:
        if not isinstance(row, dict):
            return None
        ordinal = row.get("provider_ordinal")
        if (
            type(ordinal) is not int
            or not 1 <= ordinal <= MAX_PROVIDERS
            or ordinal in seen
            or any(
                not _counter(row.get(key))
                for key in ("requests", "bytes_received", "retries")
            )
        ):
            return None
        failures = _failures(row.get("failures"))
        if failures is None:
            return None
        seen.add(ordinal)
        providers.append(
            {
                "provider_ordinal": ordinal,
                "requests": row["requests"],
                "bytes_received": row["bytes_received"],
                "retries": row["retries"],
                "failures": failures,
            }
        )
    unbound = _failures(value.get("unbound_failures", {}))
    if unbound is None:
        return None
    safe = {
        "schema_version": 1,
        "providers": sorted(providers, key=lambda row: row["provider_ordinal"]),
        "unbound_failures": unbound,
    }
    for key in ("recorded_at", "final"):
        if key in value:
            if not (
                (key == "recorded_at" and _counter(value[key]))
                or (key == "final" and type(value[key]) is bool)
            ):
                return None
            safe[key] = value[key]
    return safe
