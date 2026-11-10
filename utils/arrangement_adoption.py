"""Private immutable adoption previews, bound to native before/after state."""

from __future__ import annotations

import hashlib
import json
import secrets
from urllib.parse import urlsplit

from utils.integration_ownership import read_ownership, write_ownership

_SECRET_PARTS = (
    "password",
    "apikey",
    "api_key",
    "token",
    "secret",
    "cookie",
    "passkey",
    "username",
    "login",
    "email",
)


def _contains_secret(value, name=""):
    if any(word in name.lower() for word in _SECRET_PARTS):
        return True
    if isinstance(value, dict):
        return any(_contains_secret(child, str(key)) for key, child in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(child) for child in value)
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        try:
            parsed = urlsplit(value)
            return bool(
                parsed.username or parsed.password or parsed.query or parsed.fragment
            )
        except ValueError:
            return True
    return False


def preview(
    path,
    identity,
    before,
    after,
    *,
    secret_fields=(),
    managed=False,
    operation="preview",
    accepted_id=None,
    consent=False,
):
    """Keep private comparisons on disk; public evidence never contains secrets.

    An opaque random ID is reused only for the exact identity and comparison.
    A fresh native read invalidates consent if either side changed.
    """
    private = {"identity": identity, "before": before, "after": after}
    signature = hashlib.sha256(
        json.dumps(private, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    records = read_ownership(path)
    record = records.get(identity) or {}
    matched = record.get("signature") == signature
    if operation == "preview":
        if not matched:
            record = {"signature": signature, "id": secrets.token_urlsafe(32)}
            records[identity] = record
            write_ownership(path, records)
    identifier = record.get("id") if (matched or operation == "preview") else None
    secret_fields = {str(field).lower() for field in secret_fields}
    changes = []
    for name in sorted(set(before or {}) | set(after or {})):
        old, new = (before or {}).get(name), (after or {}).get(name)
        if old == new:
            continue
        secret = (
            name.lower() in secret_fields
            or _contains_secret(old, name)
            or _contains_secret(new, name)
        )
        changes.append(
            {
                "field": name,
                "before": (
                    "[configured]" if secret and old else (None if secret else old)
                ),
                "after": (
                    "[configured]" if secret and new else (None if secret else new)
                ),
                "secret": secret,
                "managed": managed,
                "requires_consent": not managed,
            }
        )
    approved = bool(consent and identifier and accepted_id == identifier and matched)
    return {
        "preview_id": identifier,
        "requires_consent": not managed and bool(changes),
        "changes": changes,
        "approved": approved,
    }
