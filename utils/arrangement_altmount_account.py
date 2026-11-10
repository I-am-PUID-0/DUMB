"""Fresh AltMount administrator and explicitly selected WebDAV credentials.

Official v0.3.2 (14ad0f52) auth_handlers/config_handlers, WebDAV config change
handler and rclonecli mount creation. Native account registration is permitted
only while its public first-user guard is demonstrably open. A running internal
rclone mount must be rebuilt during an explicitly authorized controlled restart.
"""

from __future__ import annotations

import base64
import copy
from io import StringIO
from pathlib import Path

import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException

from utils.arrangement_adoption import preview
from utils.arrangement_http import ApiClient, ApiError
from utils.arrangement_providers import (
    _client,
    _read_config,
    _request,
    _SETTINGS_HANDOFF,
    ProviderInputError,
    UnsupportedProviderVersion,
)
from utils.integration_ownership import fingerprint, read_ownership, write_ownership
from utils.private_files import atomic_write_private_text
from utils.zurg_config import _load_document


def _state(execution, reason, *, verification="not_checked", evidence=None, **extra):
    return {
        "execution": execution,
        "verification": verification,
        "reason": reason,
        "retryable": False,
        "evidence": evidence or {},
        **extra,
    }


def schema_fields(resource, config, payload=None):
    del config, payload
    if resource.get("service_key") != "altmount":
        return []
    source = resource["id"]
    rows = (
        (
            "create_admin",
            "Create the first AltMount administrator",
            "boolean",
            "Only a demonstrably fresh server with zero users can be initialized; an existing account is never recreated.",
        ),
        (
            "webdav_username",
            "AltMount WebDAV username",
            "string",
            "Leave both fields blank to reuse existing non-default native credentials. Changing credentials requires a fresh preview.",
        ),
        (
            "webdav_password",
            "AltMount WebDAV password",
            "password",
            "A new password must contain at least 12 characters. Retained only in protected setup inputs for selected consumers.",
        ),
        (
            "adopt_existing",
            "Accept the exact WebDAV credential changes",
            "boolean",
            "Accept only the current field preview; existing custom credentials are preserved until accepted.",
        ),
        (
            "adoption_preview_id",
            "Accepted WebDAV credential preview",
            "string",
            "Opaque identifier for the exact current credential proposal.",
        ),
        (
            "restart_after_config",
            "Allow the AltMount-only restart for its internal mount",
            "boolean",
            "Credential changes on an internal rclone mount require a controlled AltMount restart that can interrupt active playback.",
        ),
        (
            "acknowledge_external_consumers",
            "I have coordinated existing external WebDAV consumers",
            "boolean",
            "Changing WebDAV credentials also affects existing clients outside this accepted arrangement. Update their credentials deliberately; DUMB cannot rewrite unknown external clients.",
        ),
    )
    return [
        {
            "key": source + "::" + name,
            "resource_id": source,
            "service_key": "altmount",
            "label": label,
            "type": kind,
            "description": description,
            "required": False,
            **({"default": False} if kind == "boolean" else {}),
            **({"secret": True} if kind == "password" else {}),
        }
        for name, label, kind, description in rows
    ]


def native_config_path(action, config):
    del action
    cfg = config.get("altmount") or {}
    return Path(
        cfg.get("config_file")
        or str(Path(cfg.get("config_dir") or "/altmount") / "config.yaml")
    )


def _values(payload):
    return (payload.get("arrangement") or {}).get("values") or {}


def _origin(cfg):
    return f"http://127.0.0.1:{int(cfg.get('port') or 8088)}"


def _status(factory, cfg):
    document = _request(
        factory(_origin(cfg), managed=True, timeout=8),
        "GET",
        "/api/auth/registration-status",
    )
    if isinstance(document, dict) and isinstance(document.get("data"), dict):
        document = document["data"]
    count = document.get("user_count") if isinstance(document, dict) else None
    enabled = (
        document.get("registration_enabled") if isinstance(document, dict) else None
    )
    if (
        type(count) is not int
        or count < 0
        or type(enabled) is not bool
        or enabled != (count == 0)
    ):
        raise UnsupportedProviderVersion(
            "AltMount does not expose the recognized guarded first-user registration contract."
        )
    return count


def _credentials(username, password, *, creating=False, account=False):
    if (
        not isinstance(username, str)
        or not (3 if account else 1) <= len(username) <= 200
        or any(ord(ch) < 32 or ord(ch) == 127 or ch == ":" for ch in username)
        or not isinstance(password, str)
        or not 1 <= len(password) <= 4096
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in password)
    ):
        raise ProviderInputError(
            "Provide a valid username and password without control characters."
        )
    if creating and len(password) < 12:
        raise ProviderInputError("Use a password containing at least 12 characters.")


def _authorized(cfg, payload, factory, apply):
    client = _client("altmount", cfg, factory, payload=payload, allow_login=apply)
    document, _providers = _read_config(client, "altmount", cfg)
    if not isinstance(document, dict):
        raise UnsupportedProviderVersion(
            "AltMount returned an unsupported administrator settings contract."
        )
    return client, document


def _account(action, config, payload, operation, apply, factory):
    del operation
    cfg, source, values = config["altmount"], action["source"], _values(payload)
    count = _status(factory, cfg)
    username, password = values.get(source + "::admin_username"), values.get(
        source + "::admin_password"
    )
    if count == 0:
        if values.get(source + "::create_admin") is not True:
            return _state(
                "waiting_input",
                "Explicitly authorize creating the first AltMount administrator, or create it in AltMount and recheck.",
                missing_inputs=[source + "::create_admin"],
            )
        _credentials(username, password, creating=True, account=True)
        if not apply:
            return _state(
                "planned",
                "The recognized fresh AltMount server has no users. Create only this explicitly selected first administrator.",
                evidence={"fresh": True, "account_initialized": False},
            )
        # Read the authoritative first-user guard again immediately before POST.
        if _status(factory, cfg) == 0:
            _request(
                factory(_origin(cfg), managed=True, timeout=8),
                "POST",
                "/api/auth/register",
                json={"username": username, "password": password},
            )
        if _status(factory, cfg) < 1:
            return _state(
                "waiting_dependency",
                "AltMount has not confirmed its first account yet. Retry authorization; a created account will be reused.",
                retryable=True,
            )
    if (
        not values.get(source + "::settings_token")
        and username
        and password
        and not apply
    ):
        return _state(
            "planned",
            "Authorize the existing administrator using a temporary login; existing accounts are preserved.",
            evidence={"account_initialized": True},
        )
    _authorized(cfg, payload, factory, apply)
    return _state(
        "applied" if apply and count == 0 else "already_present",
        "AltMount accepted the administrator settings session. Existing accounts and login requirements were preserved.",
        verification="passed",
        evidence={"account_initialized": True, "administrator_authorized": True},
    )


def _native(action, config):
    path = native_config_path(action, config)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ProviderInputError(
            "Wait for a regular bounded AltMount native configuration."
        )
    parser, document = _load_document(path.read_text())
    section = document.get("webdav")
    if not isinstance(section, dict):
        raise UnsupportedProviderVersion(
            "AltMount's native WebDAV credential schema is unsupported."
        )
    current = {"username": section.get("user"), "password": section.get("password")}
    _credentials(current["username"], current["password"])
    return path, parser, document, current


def _webdav_auth(factory, cfg, credentials):
    encoded = base64.b64encode(
        (credentials["username"] + ":" + credentials["password"]).encode()
    ).decode()
    client = factory(
        _origin(cfg),
        managed=True,
        timeout=8,
        headers={"Authorization": "Basic " + encoded},
    )
    response = client.request(
        "PROPFIND",
        "/webdav/",
        headers={"Depth": "0", "Content-Type": "application/xml"},
        data=b'<?xml version="1.0"?><propfind xmlns="DAV:"><prop><resourcetype/></prop></propfind>',
    )
    response.raise_for_status()
    try:
        xml = ET.fromstring(response.content)
    except (ET.ParseError, DefusedXmlException, ValueError):
        raise UnsupportedProviderVersion(
            "AltMount did not return the recognized bounded WebDAV metadata response."
        ) from None
    if (
        response.status_code != 207
        or xml.tag != "{DAV:}multistatus"
        or len(xml.findall("{DAV:}response")) != 1
    ):
        raise UnsupportedProviderVersion(
            "AltMount did not return the recognized depth-zero WebDAV metadata response."
        )
    anonymous = factory(_origin(cfg), managed=True, timeout=8).request(
        "PROPFIND", "/webdav/", headers={"Depth": "0"}
    )
    if anonymous.status_code not in {401, 403}:
        raise ProviderInputError(
            "WebDAV does not require authentication on this endpoint. Review its access policy before treating stored credentials as verified."
        )


def _webdav(action, config, payload, operation, apply, factory):
    source, cfg, values = action["source"], config["altmount"], _values(payload)
    path, parser, document, current = _native(action, config)
    stopped = payload.get("_service_stopped") is True
    client = None
    if not stopped:
        client, active = _authorized(cfg, payload, factory, apply)
        visible = active.get("webdav")
        if not isinstance(visible, dict) or visible.get("user") != current["username"]:
            return _state(
                "waiting_dependency",
                "AltMount's active and saved WebDAV credentials differ. Wait for its current settings to settle before replacing them.",
                retryable=True,
            )
    supplied = [
        values.get(source + "::webdav_username"),
        values.get(source + "::webdav_password"),
    ]
    if any(supplied) and not all(supplied):
        raise ProviderInputError(
            "Provide both WebDAV username and password, or leave both blank to reuse existing non-default credentials."
        )
    desired = (
        {"username": supplied[0], "password": supplied[1]} if all(supplied) else current
    )
    _credentials(desired["username"], desired["password"], creating=desired != current)
    if current == desired and current == {"username": "usenet", "password": "usenet"}:
        return _state(
            "waiting_input",
            "Replace AltMount's default WebDAV account with explicitly chosen credentials.",
            missing_inputs=[source + "::webdav_username", source + "::webdav_password"],
        )
    ledger = path.parent / ".dumb-altmount-webdav-ownership.json"
    scope = fingerprint(
        {
            "path": str(path),
            "origin": _origin(cfg),
            "auth": (cfg.get("env") or {}).get("ALTMOUNT_API_KEY"),
        }
    )
    owned = read_ownership(ledger)
    managed = owned.get("scope") == scope and owned.get("credentials") == fingerprint(
        current
    )
    proposal = preview(
        path.parent / ".dumb-altmount-webdav-preview.json",
        source + "::webdav::" + scope,
        current,
        desired,
        managed=managed,
        operation=operation,
        accepted_id=values.get(source + "::adoption_preview_id"),
        consent=values.get(source + "::adopt_existing") is True,
    )
    changed = current != desired
    if changed and proposal["requires_consent"] and not proposal["approved"]:
        return _state(
            "waiting_input",
            "Review and accept this exact native WebDAV credential preview before replacing existing credentials.",
            evidence=proposal,
            missing_inputs=[
                source + "::adopt_existing",
                source + "::adoption_preview_id",
            ],
        )
    if changed and values.get(source + "::acknowledge_external_consumers") is not True:
        return _state(
            "waiting_input",
            "Coordinate existing WebDAV clients before changing this shared account; selected DUMB consumers must be reapplied with the same credentials.",
            evidence=proposal,
            missing_inputs=[source + "::acknowledge_external_consumers"],
        )
    internal_mount = str(cfg.get("mount_type") or "rclone").lower() == "rclone"
    if changed and not apply:
        return _state(
            "planned",
            (
                "Apply only the accepted WebDAV credentials. The internal rclone mount requires a controlled AltMount-only restart."
                if internal_mount
                else "Apply and authenticate only the accepted WebDAV credentials through AltMount's native settings API."
            ),
            evidence={**proposal, "controlled_restart_required": internal_mount},
        )
    if changed:
        if internal_mount:
            if not stopped:
                return _state(
                    "waiting_input",
                    "Authorize a controlled AltMount-only restart before changing credentials used by its internal rclone mount.",
                    evidence={**proposal, "controlled_restart_required": True},
                    missing_inputs=[source + "::restart_after_config"],
                )
            native = copy.deepcopy(document)
            native["webdav"]["user"], native["webdav"]["password"] = (
                desired["username"],
                desired["password"],
            )
            output = StringIO()
            parser.dump(native, output)
            atomic_write_private_text(path, output.getvalue())
            _path, _parser, _document, saved = _native(action, config)
            if saved != desired:
                raise ProviderInputError(
                    "The stopped native WebDAV configuration did not retain its selected credentials."
                )
        else:
            _request(
                client,
                "PATCH",
                "/api/config/webdav",
                json={
                    "webdav": {
                        "user": desired["username"],
                        "password": desired["password"],
                    }
                },
            )
            _path, _parser, _document, saved = _native(action, config)
            if saved != desired:
                raise ProviderInputError(
                    "AltMount did not retain the selected WebDAV credentials in its native configuration."
                )
        write_ownership(ledger, {"scope": scope, "credentials": fingerprint(desired)})
    if stopped:
        return _state(
            "applied",
            "The accepted private credentials were written for the controlled restart. Authenticate WebDAV after AltMount restarts.",
            evidence={"restart_required": True, "configuration": "passed"},
        )
    _webdav_auth(factory, cfg, desired)
    result = _state(
        "applied" if changed else "already_present",
        "AltMount's saved credentials and authenticated depth-zero WebDAV request were verified; no content was read.",
        verification="passed",
        evidence={"authenticated_webdav": "passed", "content_read": False},
    )
    if apply:
        result["_private_values"] = {
            source + "::webdav_username": desired["username"],
            source + "::webdav_password": desired["password"],
        }
    return result


def verified_webdav_credentials(config, *, client_factory=ApiClient):
    """Private defaults from active authenticated native credentials, never a proposal.

    A caller may fill only absent selected consumer fields with this result.
    A pending rotation, a stopped service, or an inaccessible/mismatched endpoint
    cannot introduce unverified credentials into another application's settings.
    """
    try:
        cfg = config.get("altmount") or {}
        if not cfg.get("enabled"):
            return None
        _path, _parser, _document, credentials = _native(
            {"source": "altmount", "service_key": "altmount"}, config
        )
        if credentials == {"username": "usenet", "password": "usenet"}:
            return None
        _webdav_auth(client_factory, cfg, credentials)
        return credentials
    except Exception:
        return None


def _dispatch(action, config, payload, operation, apply, factory):
    if action.get("service_key") != "altmount" or action.get("action") not in {
        "account",
        "webdav",
    }:
        return None
    handoff = {}
    context = _SETTINGS_HANDOFF.set(handoff)
    try:
        result = (_account if action["action"] == "account" else _webdav)(
            action, config, payload, operation, apply, factory
        )
    except (ProviderInputError, UnsupportedProviderVersion) as exc:
        result = _state("waiting_input", str(exc))
    except ApiError as exc:
        result = _state(
            (
                "waiting_input"
                if exc.status_code in {400, 401, 403, 409, 422}
                else "waiting_dependency"
            ),
            (
                "AltMount rejected this authorization or changed its first-user state. Recheck and provide the current account credentials."
                if exc.status_code in {400, 401, 403, 409, 422}
                else "AltMount's native account or WebDAV endpoint is not ready. Retry its bounded readiness check."
            ),
            retryable=exc.status_code not in {400, 401, 403, 409, 422},
        )
    except (OSError, ValueError, TypeError, KeyError):
        result = _state(
            "waiting_input",
            "Check AltMount's regular private configuration, selected input values and ownership-file access.",
        )
    finally:
        _SETTINGS_HANDOFF.reset(context)
    unverified = handoff.pop("_altmount_session_unverified", False)
    if handoff and not unverified:
        result.setdefault("_private_values", {}).update(handoff)
        result["_clear_inputs"] = [action["source"] + "::admin_password"]
    return result


def inspect_altmount_account(
    action, config, payload, operation=None, *, client_factory=ApiClient
):
    return _dispatch(
        action, config, payload, operation or "inspect", False, client_factory
    )


def apply_altmount_account(
    action, config, payload, operation=None, *, client_factory=ApiClient
):
    return _dispatch(
        action, config, payload, operation or "apply", True, client_factory
    )
