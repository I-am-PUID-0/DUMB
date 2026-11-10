"""Retry an explicitly authorized Plex claim through the running local server.

Native contract: python-plexapi's PlexServer.claim uses POST /myplex/claim with
the short-lived claim token. No Preferences.xml write or installer retry is used.
"""

from __future__ import annotations

from xml.etree.ElementTree import Element

from utils.arrangement_http import ApiClient, ApiError
from utils.arrangement_media import _account, _managed_plex, _request


def _state(execution, reason, **extra):
    return {
        "execution": execution,
        "verification": "not_checked",
        "reason": reason,
        "retryable": False,
        **extra,
    }


def _claim(action, config, payload, *, apply, factory):
    source = action["source"]
    values = (payload.get("arrangement") or {}).get("values") or {}
    try:
        url, key, _ = _managed_plex(config, values, source, prefer_managed=True)
        client = factory(
            url, headers={"X-Plex-Token": key} if key else {}, managed=True, timeout=10
        )
        identity = _request(client, "GET", "/identity")
        info = (
            identity.attrib
            if isinstance(identity, Element)
            else (identity or {}).get("MediaContainer", identity or {})
        )
        claimed = info.get("claimed")
        if str(claimed).lower() in {"1", "true"}:
            result = _account(action, config, payload, apply=False, factory=factory)
            if result.get("execution") in {"applied", "already_present"}:
                result["_clear_inputs"] = [f"{source}::claim_token"]
            return result
        if str(claimed).lower() not in {"0", "false"}:
            return _state(
                "unsupported",
                "This server did not explicitly report an unclaimed identity. Claim setup is available in the Plex UI.",
                verification="unavailable",
            )
        claim = values.get(f"{source}::claim_token")
        if (
            not isinstance(claim, str)
            or not claim.startswith("claim-")
            or not 7 <= len(claim) <= 500
            or any(ord(ch) <= 32 for ch in claim)
        ):
            return _state(
                "waiting_input",
                "Obtain a fresh short-lived token at plex.tv/claim after Plex is ready, then apply this account action. Existing servers are never reclaimed.",
                missing_inputs=[f"{source}::claim_token"],
            )
        if not apply:
            return _state(
                "planned",
                "The local Plex server is explicitly unclaimed. Apply this account action now to use the fresh token.",
            )
        response = client.post("/myplex/claim", params={"token": claim})
        if response.status_code in {400, 401, 403, 404, 422}:
            return _state(
                "waiting_input",
                "Plex did not accept this claim. Check its native setup or obtain a fresh claim token, then retry this action without reinstalling.",
            )
        response.raise_for_status()
        latest = _request(client, "GET", "/identity")
        latest_info = (
            latest.attrib
            if isinstance(latest, Element)
            else (latest or {}).get("MediaContainer", latest or {})
        )
        if str(latest_info.get("claimed")).lower() not in {"1", "true"}:
            return _state(
                "waiting_dependency",
                "Plex has not confirmed the claim yet. Recheck its identity before retrying account authorization.",
                retryable=True,
            )
        _, discovered, _ = _managed_plex(config, values, source, prefer_managed=True)
        result = _state(
            "applied",
            "The local server confirmed its claim. Authenticated library access is checked separately; provide its API token if it has not become available.",
            evidence={"level": "claimed_server", "installer_repeated": False},
            _clear_inputs=[f"{source}::claim_token"],
        )
        if discovered:
            result["_private_values"] = {f"{source}::api_key": discovered}
        return result
    except (ApiError, OSError, ValueError, TypeError, AttributeError):
        return _state(
            "waiting_dependency",
            "The local Plex account state could not be checked. No installer or Preferences.xml write was attempted.",
            retryable=True,
        )


def inspect_claim(action, config, payload, *, client_factory=ApiClient):
    return _claim(action, config, payload, apply=False, factory=client_factory)


def apply_claim(action, config, payload, *, client_factory=ApiClient):
    return _claim(action, config, payload, apply=True, factory=client_factory)
