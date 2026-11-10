"""Private native provider credentials and scoped external Arr registration.

InfiniDysk settings API (arr.instances/api.categories); AltMount v0.3.2
14ad0f52 config/:section and SAB /sabnzbd; Decypharr v2.7 ea020e6d partial
config update/Arr sync and qBittorrent category API. Credentials are returned
ONLY to the internal Arr adapter, never as public execution evidence.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
from pathlib import Path
import re
import threading

from utils.arrangement_http import ApiClient, ApiError
from utils.arrangement_providers import (
    _client,
    _read_config,
    _request,
    _SETTINGS_HANDOFF,
    ProviderInputError,
    UnsupportedProviderVersion,
)
from utils.zurg_config import _load_document
from utils.integration_ownership import fingerprint, read_ownership, write_ownership

_LOCK = threading.Lock()


class ProviderRegistrationError(ValueError):
    pass


def _state(reason, *, dependency=False):
    return {
        "execution": "waiting_dependency" if dependency else "waiting_input",
        "verification": "not_checked",
        "reason": reason,
        "retryable": dependency,
        "evidence": {},
    }


def _items(client, keys):
    response = _request(client, "POST", "/api/get-config", data={"config-keys": keys})
    rows = response.get("configItems", response.get("ConfigItems"))
    if not isinstance(rows, list):
        raise UnsupportedProviderVersion(
            "InfiniDysk does not expose its supported native settings API."
        )
    values = {key: "" for key in keys}
    for row in rows:
        if isinstance(row, dict):
            key = row.get("configName", row.get("ConfigName"))
            value = row.get("configValue", row.get("ConfigValue"))
            if key in values and isinstance(value, str):
                values[key] = value
    return values


_MASK_PREFIX = "__NZBDAV_SECRET_MASK_V1__:"


def _mask_matches(mask, secret, signing_key):
    if (
        not all(isinstance(value, str) for value in (mask, secret, signing_key))
        or not signing_key
    ):
        return False
    if not mask.startswith(_MASK_PREFIX):
        return hmac.compare_digest(mask, secret)
    try:
        nonce, supplied = mask[len(_MASK_PREFIX) :].split(".")
        decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        if len(decode(nonce)) != 16 or len(decode(supplied)) != 32:
            return False
        message = chr(0).join(("arr.instances", "ApiKey", nonce, secret)).encode()
        signature = hmac.new(signing_key.encode(), message, hashlib.sha256).digest()
        return hmac.compare_digest(signature, decode(supplied))
    except (ValueError, TypeError):
        return False


def _private_infini_registration(cfg):
    # The versioned native API masks secrets using random nonces. This scoped,
    # bounded read resolves only an unknown matching callback credential; all
    # writes still use the supported authenticated native API.
    from utils.nzbdav_db import get_active_config_value

    try:
        value = get_active_config_value("arr.instances", cfg, max_bytes=2 * 1024 * 1024)
        document = json.loads(value or "{}")
    except Exception:
        raise ProviderRegistrationError(
            "InfiniDysk's selected private native settings store is unavailable or exceeds its read limit. Restore its configured database connection and refresh this preview."
        ) from None
    if not isinstance(document, dict):
        raise UnsupportedProviderVersion(
            "The selected InfiniDysk native settings store uses an unsupported schema."
        )
    return document


def _normalise_infini(document, section, desired, cfg):
    result = copy.deepcopy(document)
    rows = result.get(section, [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise UnsupportedProviderVersion(
            "InfiniDysk native Arr rows use an unsupported schema."
        )
    signing_key = (cfg.get("env") or {}).get("FRONTEND_BACKEND_API_KEY") or ""
    private = None
    for row in rows:
        if not any(
            row.get(key) == desired.get(key) for key in ("Host", "Category", "Instance")
        ):
            continue
        mask = row.get("ApiKey")
        if not isinstance(mask, str) or not mask.startswith(_MASK_PREFIX):
            continue
        if _mask_matches(mask, desired["ApiKey"], signing_key):
            row["ApiKey"] = desired["ApiKey"]
            continue
        if private is None:
            private = _private_infini_registration(cfg)
        candidates = [
            candidate
            for candidate in private.get(section, [])
            if isinstance(candidate, dict)
            and all(
                candidate.get(key) == row.get(key)
                for key in ("Host", "Category", "Instance")
            )
        ]
        if len(candidates) != 1 or not _mask_matches(
            mask, candidates[0].get("ApiKey"), signing_key
        ):
            raise ProviderRegistrationError(
                "InfiniDysk's masked callback credential does not match its selected private native settings store. Refresh the preview after its effective configuration is synchronized."
            )
        row["ApiKey"] = candidates[0]["ApiKey"]
    return result


def _merge(rows, desired, identity, owned):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise UnsupportedProviderVersion(
            "Native Arr registrations use an unsupported schema."
        )
    matches = [row for row in rows if row.get(identity) == desired[identity]]
    if len(matches) > 1:
        raise ProviderRegistrationError(
            "Several native Arr registrations use this identity. Review the duplicate in the provider UI before retrying."
        )
    result = copy.deepcopy(rows)
    if not matches:
        endpoint = desired.get("Host", desired.get("url", desired.get("host")))
        matches = [
            row
            for row in rows
            if row.get("Host", row.get("url", row.get("host"))) == endpoint
        ]
        if len(matches) > 1:
            raise ProviderRegistrationError(
                "Several native registrations use this Arr endpoint. Choose the intended registration in the provider before retrying."
            )
    if not matches and identity == "Host":
        matches = [
            row
            for row in rows
            if (
                row.get("Category") == desired.get("Category")
                or row.get("Instance") == desired.get("Instance")
            )
        ]
        if len(matches) > 1:
            raise ProviderRegistrationError(
                "Several native registrations share the selected category or instance. Choose the intended registration in the provider before retrying."
            )
    conflict = None
    prior = None
    if matches:
        existing = matches[0]
        view = {key: existing.get(key) for key in desired}
        prior = view
        if view == desired:
            return result, False, None, prior
        if owned.get("fingerprint") != fingerprint(view):
            conflict = view
        result[result.index(existing)] = {**existing, **desired}
    else:
        result.append(desired)
    return result, True, conflict, prior


def _scope(cfg, provider, target):
    """A reused resource name grants no authority over another native identity."""
    token = (cfg.get("env") or {}).get(
        "FRONTEND_BACKEND_API_KEY" if provider == "infinidysk" else "ALTMOUNT_API_KEY"
    ) or cfg.get("api_key")
    native_base = ""
    if provider == "decypharr":
        auth = Path(cfg.get("config_dir") or "/decypharr") / "auth.json"
        if auth.is_file() and not auth.is_symlink() and auth.stat().st_size <= 65536:
            token = json.loads(auth.read_text()).get("api_token") or token
        path = Path(cfg.get("config_file") or str(auth.parent / "config.json"))
        if (
            path.is_file()
            and not path.is_symlink()
            and path.stat().st_size <= 2 * 1024 * 1024
        ):
            native_base = json.loads(path.read_text()).get("url_base")
    return fingerprint(
        {
            "provider": provider,
            "config_dir": cfg.get("config_dir"),
            "config_file": cfg.get("config_file"),
            "port": (
                cfg.get("backend_port", 8080)
                if provider == "infinidysk"
                else cfg.get("port", 8088 if provider == "altmount" else 8282)
            ),
            "url_base": native_base,
            "provider_auth": token,
            "target_scope": target.get("ownership_scope"),
            "target": {
                "family": target.get("family", target.get("service_key")),
                "url": target["url"],
                "key": target["key"],
            },
        }
    )


def _adoption(
    scope,
    before,
    after,
    *,
    category,
    category_changed,
    registration_requires_consent=False,
    enabled=None,
    callbacks=None,
    callback_context=None,
):
    old = {"registration." + key: value for key, value in (before or {}).items()}
    new = {"registration." + key: value for key, value in after.items()}
    if category_changed:
        old["category.present"] = False
        new["category.name"] = category
        new["category.present"] = True
    if enabled is False:
        old["api.enabled"] = False
        new["api.enabled"] = True
    if callbacks is False:
        old["callbacks.enabled"] = False
        new["callbacks.enabled"] = True
        old["callbacks.existing_enabled_registrations"] = [
            {"family": family, "name": row.get("name"), "url": row.get("url")}
            for family, rows in (callback_context or {}).items()
            if family.endswith("_instances") and isinstance(rows, list)
            for row in rows
            if isinstance(row, dict) and row.get("enabled") is True
        ]
    callback_snapshot = [
        {
            "family": family,
            **{key: row.get(key) for key in ("name", "url", "api_key", "enabled")},
        }
        for family, rows in sorted((callback_context or {}).items())
        if family.endswith("_instances") and isinstance(rows, list)
        for row in rows
        if isinstance(row, dict)
    ]
    return {
        "before": old,
        "after": new,
        "fingerprint": fingerprint(
            {
                "scope": scope,
                "before": old,
                "after": new,
                "callbacks": callback_snapshot if callbacks is False else None,
            }
        ),
        "requires_consent": registration_requires_consent
        or enabled is False
        or callbacks is False,
    }


def _check_rows(rows, desired, identity):
    matches = [
        row
        for row in rows
        if isinstance(row, dict) and row.get(identity) == desired[identity]
    ]
    return len(matches) == 1 and all(
        matches[0].get(key) == value for key, value in desired.items()
    )


def _protocol_version(factory, cfg, provider, fields):
    token = fields.get("apiKey")
    if not isinstance(token, str) or not token:
        raise ProviderRegistrationError(
            "Wait for the selected provider's private native download API key before adding an external client."
        )
    port = (
        cfg.get("backend_port", 8080)
        if provider == "infinidysk"
        else cfg.get("port", 8088 if provider == "altmount" else 8282)
    )
    protocol = factory(f"http://127.0.0.1:{int(port)}", managed=True, timeout=8)
    response = protocol.request(
        "GET",
        str(fields.get("urlBase") or "") + "/api",
        params={"mode": "version", "output": "json", "apikey": token},
    )
    response.raise_for_status()
    document = response.json()
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("version"), str)
        or document.get("status") is False
    ):
        raise ProviderRegistrationError(
            "The native SAB download API rejected its key or is not enabled. Check the managed provider's native API settings before retrying."
        )


def _sab_category_ready(factory, cfg, fields, category):
    protocol = factory(
        f"http://127.0.0.1:{int(cfg.get('port') or 8282)}", managed=True, timeout=8
    )
    response = protocol.request(
        "GET",
        fields["urlBase"] + "/api",
        params={"mode": "get_config", "output": "json", "apikey": fields["apiKey"]},
    )
    response.raise_for_status()
    config = response.json()
    rows = (
        (config.get("config") or {}).get("categories")
        if isinstance(config, dict)
        else None
    )
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise UnsupportedProviderVersion(
            "Decypharr does not expose the supported native SAB category settings contract."
        )
    return any(row.get("name") == category for row in rows)


def _decypharr_protocol_fields(cfg):
    directory = Path(cfg.get("config_dir") or "/decypharr")
    native = Path(cfg.get("config_file") or str(directory / "config.json"))
    if (
        native.is_symlink()
        or not native.is_file()
        or native.stat().st_size > 2 * 1024 * 1024
    ):
        raise ProviderRegistrationError(
            "Wait for a regular bounded Decypharr native configuration."
        )
    base = str(json.loads(native.read_text()).get("url_base") or "").rstrip("/")
    if base and (
        not base.startswith("/")
        or base.startswith("//")
        or any(ch in base for ch in ("?", "#", chr(92), chr(13), chr(10)))
    ):
        raise ProviderRegistrationError(
            "Decypharr's native URL base must be a plain application path."
        )
    auth = directory / "auth.json"
    token = cfg.get("api_key") or ""
    if auth.exists():
        if auth.is_symlink() or not auth.is_file() or auth.stat().st_size > 65536:
            raise ProviderRegistrationError(
                "Decypharr credentials must be a bounded regular private file."
            )
        token = json.loads(auth.read_text()).get("api_token") or token
    return base, token


def external_arr_provider_credentials_and_category(
    config,
    payload,
    provider,
    target,
    category,
    operation,
    *,
    client_factory=ApiClient,
    ownership_directory="/config/setup-automation/ownership",
    approved_provider_preview=None,
):
    """Inspect returns private fields plus planned readiness without provider writes."""
    if provider not in {"decypharr", "infinidysk", "altmount"}:
        return _state("Select a supported managed download provider.")
    cfg = config.get(provider) or {}
    if provider == "infinidysk" and str(cfg.get("runtime_generation") or "1") == "2":
        return _state(
            "InfiniDysk 2 uses its native Arr/SAB settings and credentials; legacy registration is not applied."
        )
    if not cfg.get("enabled"):
        return _state(
            "Enable the selected managed provider before linking the external Arr."
        )
    family = target.get("family") or target.get("service_key")
    identifier, host, key = target.get("id"), target.get("url"), target.get("key")
    if family not in {"radarr", "sonarr", "lidarr", "whisparr"} or not all(
        isinstance(value, str) and value for value in (identifier, host, key)
    ):
        return _state(
            "Choose an authenticated, explicitly authorized external Arr target."
        )
    if not isinstance(category, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", category
    ):
        return _state(
            "Choose a dedicated provider category using letters, numbers, dash, dot, underscore or colon."
        )
    apply = operation == "apply"
    handoff = {}
    context = _SETTINGS_HANDOFF.set(handoff)
    try:
        with _LOCK:
            result = _reconcile(
                config,
                payload,
                cfg,
                provider,
                target,
                category,
                apply,
                client_factory,
                ownership_directory,
                approved_provider_preview,
            )
    except (
        ProviderRegistrationError,
        ProviderInputError,
        UnsupportedProviderVersion,
    ) as exc:
        result = _state(str(exc))
    except ApiError as exc:
        result = _state(
            (
                "The provider rejected its configured authorization. Reauthorize its settings account/key before retrying."
                if exc.status_code in {401, 403}
                else "The provider's native settings API is not ready. Retry after its bounded readiness checks pass."
            ),
            dependency=exc.status_code not in {400, 401, 403, 422},
        )
    except (OSError, ValueError, TypeError, KeyError):
        result = _state(
            "Check the provider's native configuration, private credentials and ownership-file access."
        )
    finally:
        _SETTINGS_HANDOFF.reset(context)
    unverified = handoff.pop("_altmount_session_unverified", False)
    if handoff and not unverified:
        result["_private_values"] = handoff
        result["_clear_inputs"] = ["altmount::admin_password"]
    return result


def _reconcile(
    config,
    payload,
    cfg,
    provider,
    target,
    category,
    apply,
    factory,
    directory,
    approved_provider_preview,
):
    identifier, family, host, arr_key = (
        target["id"],
        target.get("family", target.get("service_key")),
        target["url"],
        target["key"],
    )
    # Settings credentials and download-client credentials are separate in AltMount.
    values = (payload.get("arrangement") or {}).get("values") or {}
    local_preview = (
        provider == "altmount"
        and not apply
        and not values.get("altmount::settings_token")
    )
    client = (
        None
        if local_preview
        else _client(provider, cfg, factory, payload=payload, allow_login=apply)
    )
    scope = _scope(cfg, provider, target)
    ledger = Path(directory) / ("native-provider-" + scope + ".json")
    ownership = read_ownership(ledger)
    own = (
        (ownership.get("registration") or {}) if ownership.get("scope") == scope else {}
    )
    if provider == "infinidysk":
        token = (cfg.get("env") or {}).get("FRONTEND_BACKEND_API_KEY") or cfg.get(
            "api_key"
        )
        fields, implementation = {"apiKey": token, "urlBase": ""}, "Sabnzbd"
        _protocol_version(factory, cfg, provider, fields)
        settings = _items(client, ["arr.instances", "api.categories"])
        document = json.loads(settings["arr.instances"] or "{}")
        if not isinstance(document, dict):
            raise UnsupportedProviderVersion(
                "InfiniDysk's Arr settings use an unsupported schema."
            )
        section = family.capitalize() + "Instances"
        desired = {
            "Host": host,
            "ApiKey": arr_key,
            "Instance": identifier,
            "Category": category,
        }
        document = _normalise_infini(document, section, desired, cfg)
        rows, changed, conflict, prior = _merge(
            document.get(section, []), desired, "Host", own
        )
        categories = [
            name.strip()
            for name in settings["api.categories"].split(",")
            if name.strip()
        ]
        category_changed = category not in categories
        if category_changed:
            categories.append(category)
        preview = _adoption(
            scope,
            prior,
            desired,
            category=category,
            category_changed=category_changed,
            registration_requires_consent=conflict is not None,
        )
        authorized = (
            not preview["requires_consent"]
            or approved_provider_preview == preview["fingerprint"]
        )
        if apply and authorized and (changed or category_changed):
            updates = {
                "arr.instances": json.dumps(
                    {**document, section: rows}, separators=(",", ":")
                ),
                "api.categories": ",".join(categories),
            }
            _request(client, "POST", "/api/update-config", data=updates)
            saved = _items(client, ["arr.instances", "api.categories"])
            actual = _normalise_infini(
                json.loads(saved["arr.instances"] or "{}"), section, desired, cfg
            )
            if not _check_rows(
                actual.get(section, []), desired, "Host"
            ) or category not in saved["api.categories"].split(","):
                raise ProviderRegistrationError(
                    "InfiniDysk did not retain the selected Arr registration/category. Check its native settings before retrying."
                )
        token = (cfg.get("env") or {}).get("FRONTEND_BACKEND_API_KEY") or cfg.get(
            "api_key"
        )
        fields, implementation = {"apiKey": token, "urlBase": ""}, "Sabnzbd"
    else:
        if local_preview:
            path = Path(
                cfg.get("config_file")
                or str(Path(cfg.get("config_dir") or "/altmount") / "config.yaml")
            )
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > 2 * 1024 * 1024
            ):
                raise ProviderRegistrationError(
                    "Wait for a regular AltMount native configuration and authorize its administrator session to apply."
                )
            _, document = _load_document(path.read_text())
        else:
            document, _ = _read_config(client, provider, cfg)
        if provider == "altmount":
            section = family + "_instances"
            arrs = document.get("arrs")
            sab = document.get("sabnzbd")
            if (
                not isinstance(arrs, dict)
                or not isinstance(sab, dict)
                or not isinstance(sab.get("categories"), list)
            ):
                raise UnsupportedProviderVersion(
                    "AltMount does not expose the supported Arr/SAB settings contract."
                )
            token = (cfg.get("env") or {}).get("ALTMOUNT_API_KEY") or cfg.get("api_key")
            fields, implementation = {"apiKey": token, "urlBase": "/sabnzbd"}, "Sabnzbd"
            if sab.get("enabled") is True:
                _protocol_version(factory, cfg, provider, fields)
            desired = {
                "name": category,
                "url": host,
                "api_key": arr_key,
                "enabled": True,
            }
            rows, changed, conflict, prior = _merge(
                arrs.get(section, []), desired, "name", own
            )
            categories = copy.deepcopy(sab["categories"])
            category_changed = not any(
                isinstance(row, dict) and row.get("name") == category
                for row in categories
            )
            if category_changed:
                categories.append(
                    {
                        "name": category,
                        "order": len(categories),
                        "priority": 0,
                        "dir": category,
                    }
                )
            values = (payload.get("arrangement") or {}).get("values") or {}
            if (
                arrs.get("enabled") is not True
                and values.get(f"{identifier}::{provider}::enable_callbacks")
                is not True
            ):
                raise ProviderRegistrationError(
                    "AltMount's native Arr callback integration is disabled. Explicitly authorize enabling it, review all affected enabled registrations, and accept its fresh preview before linking this target."
                )
            if (
                sab.get("enabled") is not True
                and values.get(f"{identifier}::{provider}::enable_api") is not True
            ):
                raise ProviderRegistrationError(
                    "The selected provider's SAB API is disabled. Explicitly authorize enabling this API before adding the external download client."
                )
            category_changed |= sab.get("enabled") is not True
            preview = _adoption(
                scope,
                prior,
                desired,
                category=category,
                category_changed=category_changed,
                registration_requires_consent=conflict is not None,
                enabled=sab.get("enabled"),
                callbacks=arrs.get("enabled"),
                callback_context=arrs,
            )
            authorized = (
                not preview["requires_consent"]
                or approved_provider_preview == preview["fingerprint"]
            )
            callbacks_changed = arrs.get("enabled") is not True
            changed |= callbacks_changed
            if apply and authorized and changed:
                # Send only the Arr section; native PATCH preserves every other section.
                _request(
                    client,
                    "PATCH",
                    "/api/config/arrs",
                    json={"arrs": {**arrs, section: rows, "enabled": True}},
                )
            if apply and authorized and category_changed:
                # Never send the masked fallback API key returned by GET config.
                _request(
                    client,
                    "PATCH",
                    "/api/config/sabnzbd",
                    json={"sabnzbd": {"categories": categories, "enabled": True}},
                )
            if apply and authorized and (changed or category_changed):
                saved, _ = _read_config(client, provider, cfg)
                if (
                    not _check_rows(
                        (saved.get("arrs") or {}).get(section, []), desired, "name"
                    )
                    or (saved.get("arrs") or {}).get("enabled") is not True
                    or not any(
                        row.get("name") == category
                        for row in (saved.get("sabnzbd") or {}).get("categories", [])
                    )
                ):
                    raise ProviderRegistrationError(
                        "AltMount did not retain the selected Arr registration/category. Check its native settings before retrying."
                    )
            token = (cfg.get("env") or {}).get("ALTMOUNT_API_KEY") or cfg.get("api_key")
            fields, implementation = {"apiKey": token, "urlBase": "/sabnzbd"}, "Sabnzbd"
        else:
            native_base, native_token = _decypharr_protocol_fields(cfg)
            usenet = document.get("usenet") or {}
            if not isinstance(usenet, dict) or not isinstance(
                usenet.get("providers", []), list
            ):
                raise UnsupportedProviderVersion(
                    "Decypharr's native Usenet account list uses an unsupported schema."
                )
            additional_clients = []
            if usenet.get("providers"):
                sab_fields = {
                    "apiKey": native_token,
                    "urlBase": native_base + "/sabnzbd",
                }
                _protocol_version(factory, cfg, provider, sab_fields)
                additional_clients.append(
                    {
                        "implementation": "Sabnzbd",
                        "fields": sab_fields,
                        "name_suffix": "usenet",
                    }
                )
            desired = {"name": category, "host": host, "token": arr_key}
            rows, changed, conflict, prior = _merge(
                document.get("arrs", []), desired, "name", own
            )
            categories = list(document.get("categories") or [])
            if any(not isinstance(item, str) for item in categories):
                raise UnsupportedProviderVersion(
                    "Decypharr categories use an unsupported schema."
                )
            category_changed = category not in categories
            if category_changed:
                categories.append(category)
            preview = _adoption(
                scope,
                prior,
                desired,
                category=category,
                category_changed=category_changed,
                registration_requires_consent=conflict is not None,
            )
            authorized = (
                not preview["requires_consent"]
                or approved_provider_preview == preview["fingerprint"]
            )
            if apply and authorized and (changed or category_changed):
                _request(
                    client,
                    "POST",
                    "/api/config",
                    json={"arrs": rows, "categories": categories},
                )
                saved, _ = _read_config(client, provider, cfg)
                if not _check_rows(
                    saved.get("arrs", []), desired, "name"
                ) or category not in saved.get("categories", []):
                    raise ProviderRegistrationError(
                        "Decypharr did not retain the selected Arr callback/category registration."
                    )
            if apply and authorized:
                # qBittorrent caches categories separately from hot config. Use
                # its supported category API and prove callback-bound login.
                protocol = factory(
                    f"http://127.0.0.1:{int(cfg.get('port') or 8282)}{native_base}",
                    managed=True,
                    timeout=8,
                )
                login = protocol.request(
                    "POST",
                    "/api/v2/auth/login",
                    data={"username": host, "password": arr_key, "category": category},
                )
                login.raise_for_status()
                if login.text.strip() != "Ok.":
                    raise ProviderRegistrationError(
                        "Decypharr rejected the registered Arr's qBittorrent credentials."
                    )
                response = protocol.request("GET", "/api/v2/torrents/categories")
                response.raise_for_status()
                live = response.json()
                if not isinstance(live, dict):
                    raise UnsupportedProviderVersion(
                        "Decypharr returned an unsupported qBittorrent category response."
                    )
                if category not in live:
                    created = protocol.request(
                        "POST",
                        "/api/v2/torrents/createCategory",
                        data={"category": category},
                    )
                    created.raise_for_status()
                    response = protocol.request("GET", "/api/v2/torrents/categories")
                    response.raise_for_status()
                    if category not in response.json():
                        raise ProviderRegistrationError(
                            "Decypharr did not retain the live qBittorrent category."
                        )
            fields, implementation = {
                "username": host,
                "password": arr_key,
                "urlBase": native_base,
            }, "QBittorrent"
    if provider in {"infinidysk", "altmount"} and apply and authorized:
        _protocol_version(factory, cfg, provider, fields)
    if apply and authorized and (changed or category_changed):
        ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_ownership(
            ledger,
            {"scope": scope, "registration": {"fingerprint": fingerprint(desired)}},
        )
    result = {
        "implementation": implementation,
        "fields": fields,
        "_provider_adoption": preview,
        "evidence": {
            "provider_registration": (
                "passed" if (apply and authorized) or not changed else "planned"
            ),
            "provider_category": (
                "passed"
                if (apply and authorized) or not category_changed
                else "planned"
            ),
            "provider": provider,
            "content_tested": False,
        },
    }
    if provider == "decypharr":
        result["additional_clients"] = additional_clients
        if additional_clients:
            ready = _sab_category_ready(
                factory, cfg, additional_clients[0]["fields"], category
            )
            result["evidence"]["provider_sab_category"] = (
                "passed" if ready else "planned"
            )
            if apply and authorized and not ready:
                result.update(
                    {
                        "execution": "waiting_input",
                        "verification": "not_checked",
                        "retryable": False,
                        "reason": "Decypharr retained the category, but its native SAB settings still expose the startup category cache. Authorize a controlled Decypharr-only restart, then retry this exact provider connection.",
                    }
                )
                result["evidence"]["restart_required"] = "decypharr"
    if apply and not authorized:
        result.update(
            _state(
                "The provider registration changed or is user-owned. Review its current field preview and explicitly accept that exact adoption before retrying."
            )
        )
    return result


provider_connection = external_arr_provider_credentials_and_category
