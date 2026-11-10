"""Selected request/maintenance application connections to media servers.

Official contracts inspected 2026-10-14:
seerr-team/seerr develop 978f437dc06af77459c8887781a32d2879f1b69d:
  server/routes/settings/index.ts, server/routes/auth.ts, server/lib/settings.
Maintainerr/Maintainerr development c970773da12f79a746edd9b80e24abb26066d50e:
  settings.controller/settings-operations.service, media-server.controller,
  packages/contracts/src/settings/settings.ts.

These actions only configure/test connections. They never import users, enable
libraries, run scans/rules, activate deletion/retention policies or migrate an
established server's collections.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from utils.arrangement_http import ApiClient, ApiError
from utils.arrangement_media import (
    AUTHORIZATION,
    UnsupportedMediaVersion,
    _dto_value,
    _client as media_client,
    _managed_plex,
    _request as media_request,
    _resource,
    _state,
    _values,
)
from utils.integration_ownership import fingerprint, read_ownership, write_ownership


class AppMediaInputError(ValueError):
    pass


def schema_fields(resource, config, payload=None):
    if resource["service_key"] not in {"seerr", "maintainerr"}:
        return []
    rid = resource["id"]
    fields = [
        {
            "key": f"{rid}::api_key",
            "resource_id": rid,
            "service_key": resource["service_key"],
            "label": "Application API key",
            "type": "password",
            "required": False,
            "description": "Authorize this application's settings API. Seerr can reuse its generated key after its first administrator has signed in.",
        }
    ]
    if resource["service_key"] == "maintainerr":
        fields.append(
            {
                "key": f"{rid}::media_user_id",
                "resource_id": rid,
                "service_key": "maintainerr",
                "label": "Media administrator user ID",
                "type": "string",
                "required": False,
                "description": "Optional exact Jellyfin/Emby administrator ID. The native application validates it; an ambiguous administrator choice remains manual.",
            }
        )
    from utils.arrangement_app_companions import schema_fields as companion_fields

    return fields + companion_fields(resource, config, payload)


def _source(action, config, payload, factory):
    source, service = action["source"], action["service_key"]
    cfg, values = _resource(config, source), _values(payload)
    token = values.get(f"{source}::api_key") or cfg.get("api_key") or ""
    if not token and service == "seerr":
        path = cfg.get("config_file") or str(
            Path(cfg.get("config_dir") or "/seerr") / "settings.json"
        )
        if Path(path).is_symlink():
            raise AppMediaInputError(
                "The generated Seerr configuration must be a regular file."
            )
        try:
            stored = json.loads(Path(path).read_text())
            token = (stored.get("main") or {}).get("apiKey") or ""
        except FileNotFoundError:
            pass
    if service == "seerr" and not token:
        raise AppMediaInputError(
            "Finish Seerr's first administrator sign-in, then provide or reuse its generated API key."
        )
    base = str(
        cfg.get("base_path")
        or cfg.get("url_base")
        or (cfg.get("env") or {}).get("BASE_PATH")
        or ""
    ).rstrip("/")
    if base and (
        not base.startswith("/")
        or base.startswith("//")
        or any(ch in base for ch in "?#\\\r\n")
    ):
        raise AppMediaInputError("Use a valid application URL base path.")
    port = int(cfg.get("port") or (5055 if service == "seerr" else 6246))
    return (
        factory(
            f"http://127.0.0.1:{port}{base}",
            headers={"X-Api-Key": token} if token else {},
            managed=True,
            timeout=10,
        ),
        cfg,
    )


def _request(client, method, path, **kwargs):
    value = media_request(client, method, path, **kwargs)
    if isinstance(value, dict) and (
        value.get("status") == "NOK"
        or value.get("code") == 0
        or value.get("success") is False
    ):
        raise AppMediaInputError(
            "The application rejected its connection test. Check authorization and endpoint settings in its UI."
        )
    return value


def _target(action, config, payload, factory):
    source, values = action["source"], _values(payload)
    selected = values.get(f"{source}::media_server")
    if selected == "none":
        return None
    if not selected:
        raise AppMediaInputError("Choose the media server this application should use.")
    if selected == "external":
        info = values.get(f"{source}::external_media") or {}
        if not isinstance(info, dict) or info.get(
            "service_key", info.get("type")
        ) not in {"plex", "jellyfin", "emby"}:
            raise AppMediaInputError(
                "Choose Plex, Jellyfin or Emby for the external media connection."
            )
        service, url, token = (
            info.get("service_key", info.get("type")),
            info.get("url"),
            info.get("api_key"),
        )
        if not isinstance(url, str) or not isinstance(token, str) or not token:
            raise AppMediaInputError(
                "Provide the external media URL and application key."
            )
        if any(
            ord(ch) < 32 or ord(ch) == 127 or ch in (chr(34), chr(92)) for ch in token
        ):
            raise AppMediaInputError(
                "The media API key contains an unsupported character."
            )
        headers = (
            {"X-Plex-Token": token}
            if service == "plex"
            else {
                "Authorization": AUTHORIZATION + f', Token="{token}"',
                "X-Emby-Token": token,
            }
        )
        client = factory(
            url,
            headers=headers,
            managed=False,
            allow_private=info.get("allow_private") is True,
            verify_tls=info.get("verify_tls") is not False,
            timeout=10,
        )
        return {
            "service": service,
            "url": url.rstrip("/"),
            "key": token,
            "client": client,
            "managed": False,
        }
    service = str(selected).split(".", 1)[0]
    if service not in {"plex", "jellyfin", "emby"}:
        raise AppMediaInputError(
            "This application supports automatic Plex/Jellyfin/Emby linking only."
        )
    cfg = _resource(config, selected)
    if not cfg.get("enabled"):
        raise AppMediaInputError(
            "The selected managed media server is disabled or missing."
        )
    if service == "plex":
        url, token, managed = _managed_plex(
            config, values, selected, prefer_managed=True
        )
    else:
        token = values.get(f"{selected}::api_key") or cfg.get("api_key") or ""
        for item in ((config.get("dumb") or {}).get("media_protection") or {}).get(
            "services", []
        ):
            if not token and item.get("process_name") == cfg.get("process_name"):
                token = item.get("api_key") or ""
        base = str(cfg.get("url_base") or cfg.get("base_url") or "").rstrip("/")
        url, managed = f"http://127.0.0.1:{int(cfg.get('port') or 8096)}{base}", True
    if not token:
        raise AppMediaInputError(
            "Wait for the selected media server's application key or finish account authorization."
        )
    return {
        "service": service,
        "url": url,
        "key": token,
        "client": media_client(service, selected, config, payload, factory),
        "managed": managed,
    }


def _identity(target):
    value = _request(
        target["client"], "GET", "/" if target["service"] == "plex" else "/System/Info"
    )
    if not isinstance(value, dict):
        raise AppMediaInputError(
            "The selected media server did not return a supported authenticated identity."
        )
    if target["service"] == "plex":
        value = value.get("MediaContainer") or {}
        identifier, name = value.get("machineIdentifier"), value.get("friendlyName")
    else:
        identifier, name = _dto_value(value, "Id", "id"), _dto_value(
            value, "ServerName", "serverName"
        )
    if not identifier:
        raise AppMediaInputError(
            "The selected media server did not return an authenticated server ID."
        )
    return str(identifier), str(name or "Media server")


def _ledger(cfg, source, directory):
    parent = Path(directory or cfg.get("config_dir") or "")
    if not str(parent) or str(parent) == ".":
        raise AppMediaInputError(
            "The application's configuration directory is not ready."
        )
    return parent / (fingerprint(source) + ".media-ownership.json")


def _ownership(ledger, current, desired, identity, *, identity_field=None):
    records = read_ownership(ledger)
    if (
        identity_field
        and current.get(identity_field)
        and str(current[identity_field]) != identity
    ):
        raise AppMediaInputError(
            "This application already tracks another media server. Use its explicit migration workflow; existing collections and request history were preserved."
        )
    populated = any(
        current.get(key) not in (None, "", 0, False)
        for key in desired
        if key not in {"port", "useSsl", "plex_port", "plex_ssl", "plex_manual_mode"}
    )
    changed = any(current.get(key) != value for key, value in desired.items())
    snapshot = {
        key: current.get(key)
        for key in desired
        if "key" not in key.lower() and "token" not in key.lower()
    }
    credentials = {
        key: current.get(key)
        for key in desired
        if "key" in key.lower() or "token" in key.lower()
    }
    owned = (
        records.get("fingerprint") == fingerprint(snapshot)
        and records.get("server_id") == identity
        and records.get("credential_fingerprint") == fingerprint(credentials)
    )
    if changed and populated and not owned:
        raise AppMediaInputError(
            "An existing connection is user-managed or was edited outside DUMB. It was preserved; confirm the intended endpoint in the application's UI."
        )
    return records, changed


def _save_ownership(path, desired, identity):
    snapshot = {
        key: value
        for key, value in desired.items()
        if "key" not in key.lower() and "token" not in key.lower()
    }
    credentials = {
        key: value
        for key, value in desired.items()
        if "key" in key.lower() or "token" in key.lower()
    }
    write_ownership(
        path,
        {
            "fingerprint": fingerprint(snapshot),
            "server_id": identity,
            "credential_fingerprint": fingerprint(credentials),
        },
    )


def _seerr(action, config, payload, target, *, apply, factory, ownership_directory):
    client, cfg = _source(action, config, payload, factory)
    source = action["source"]
    service = target["service"]
    info = _request(client, "GET", "/api/v1/settings/main")
    expected_type = {"plex": 1, "jellyfin": 2, "emby": 3}[service]
    if not isinstance(info, dict) or "mediaServerType" not in info:
        raise AppMediaInputError(
            "This Seerr version does not expose the supported media-server settings schema."
        )
    if info["mediaServerType"] not in (expected_type, 4, None):
        raise AppMediaInputError(
            "Seerr is configured for another media-server type. Use its explicit migration flow; accounts and requests were preserved."
        )
    # An authenticated account is a prerequisite; DUMB never fabricates a
    # Seerr administrator or imports account identity from a local database.
    user = _request(client, "GET", "/api/v1/auth/me")
    if not isinstance(user, dict) or not user.get("id"):
        raise AppMediaInputError(
            "Finish Seerr's first administrator sign-in, then retry the connection."
        )
    if service == "plex" and not user.get("plexId"):
        raise AppMediaInputError(
            "Sign in to Seerr with the Plex owner account before configuring this Plex endpoint."
        )
    identity, _name = _identity(target)
    parts = urlsplit(target["url"])
    endpoint = "plex" if service == "plex" else "jellyfin"
    path = f"/api/v1/settings/{endpoint}"
    current = _request(client, "GET", path)
    if not isinstance(current, dict) or "libraries" not in current:
        raise AppMediaInputError(
            "This Seerr version does not expose the supported connection settings."
        )
    if service == "plex" and parts.path not in ("", "/"):
        raise AppMediaInputError(
            "Seerr's Plex connector does not support a URL base path. Use a direct server endpoint."
        )
    desired = {
        "ip": parts.hostname,
        "port": parts.port or (443 if parts.scheme == "https" else 80),
        "useSsl": parts.scheme == "https",
    }
    if service != "plex":
        desired.update(urlBase=parts.path.rstrip("/"), apiKey=target["key"])
    ledger = _ledger(cfg, source, ownership_directory)
    _records, changed = _ownership(
        ledger,
        current,
        desired,
        identity,
        identity_field="machineId" if service == "plex" else "serverId",
    )
    if changed and not apply:
        return _state(
            "planned",
            "The authorized Seerr media connection is ready to configure; library choices and scanning remain unchanged.",
        )
    if changed:
        _request(client, "POST", path, json={**current, **desired})
        _save_ownership(ledger, desired, identity)
    saved = _request(client, "GET", path)
    if (
        not isinstance(saved, dict)
        or any(saved.get(key) != value for key, value in desired.items())
        or str(saved.get("machineId" if service == "plex" else "serverId") or "")
        != identity
    ):
        return _state(
            "failed",
            "Seerr's authenticated connection did not match its settings readback.",
            verification="failed",
            retryable=True,
        )
    return _state(
        "applied" if changed else "already_present",
        "Seerr's selected endpoint and authenticated server ID were verified. Library sync, library enablement and scans were left unchanged.",
        verification="passed",
        evidence={"connection": "passed", "library_sync_started": False},
    )


def _maintainerr(
    action, config, payload, target, *, apply, factory, ownership_directory
):
    client, cfg = _source(action, config, payload, factory)
    source, service = action["source"], target["service"]
    current = _request(client, "GET", "/api/settings")
    if not isinstance(current, dict) or "media_server_type" not in current:
        raise AppMediaInputError(
            "This Maintainerr version does not expose the supported selected-server settings schema."
        )
    if current.get("media_server_type") not in (None, "", service):
        raise AppMediaInputError(
            "Maintainerr already tracks a different server type. Use its explicit migration workflow; collections and policies were preserved."
        )
    identity, name = _identity(target)
    parts = urlsplit(target["url"])
    if service == "plex":
        if parts.path not in ("", "/"):
            raise AppMediaInputError(
                "Maintainerr's Plex connector requires a direct server URL without a base path."
            )
        desired = {
            "plex_hostname": parts.hostname,
            "plex_port": parts.port or (443 if parts.scheme == "https" else 80),
            "plex_ssl": int(parts.scheme == "https"),
            "plex_name": name,
            "plex_machine_id": identity,
            "plex_manual_mode": 1,
        }
        specific, save_path, test_path = (
            current,
            "/api/settings",
            "/api/settings/test/plex",
        )
    else:
        specific = _request(client, "GET", f"/api/settings/{service}")
        if not isinstance(specific, dict):
            raise AppMediaInputError(
                "The selected-server settings are not supported by this Maintainerr version."
            )
        desired = {f"{service}_url": target["url"], f"{service}_api_key": target["key"]}
        user_id = _values(payload).get(f"{source}::media_user_id") or specific.get(
            f"{service}_user_id"
        )
        if user_id:
            desired[f"{service}_user_id"] = str(user_id)
        save_path, test_path = (
            f"/api/settings/{service}",
            f"/api/settings/{service}/test",
        )
    ledger = _ledger(cfg, source, ownership_directory)
    _records, changed = _ownership(
        ledger,
        specific,
        desired,
        identity,
        identity_field="plex_machine_id" if service == "plex" else None,
    )
    if changed and not apply:
        return _state(
            "planned",
            "The Maintainerr media connection is ready to configure. Retention rules, collections and deletion policies remain unchanged.",
        )
    if changed:
        if service == "plex":
            # This endpoint stores only the selected Plex token. Settings PATCH
            # is used only on versions advertising the explicit server schema.
            _request(
                client,
                "POST",
                "/api/settings/plex/token",
                json={"plex_auth_token": target["key"]},
            )
            _request(
                client,
                "PATCH",
                save_path,
                json={"media_server_type": "plex", **desired},
            )
        else:
            test = _request(client, "POST", test_path, json=desired)
            if (
                not isinstance(test, dict)
                or test.get("status") != "OK"
                or test.get("code") != 1
            ):
                raise AppMediaInputError(
                    "Maintainerr could not authenticate the selected media server."
                )
            _request(client, "POST", save_path, json=desired)
        _save_ownership(ledger, desired, identity)
    saved = _request(client, "GET", "/api/settings" if service == "plex" else save_path)
    # Secrets may be masked by newer GET handlers; server identity is verified
    # independently through the active connector below.
    visible = {key: value for key, value in desired.items() if "api_key" not in key}
    if not isinstance(saved, dict) or any(
        saved.get(key) != value for key, value in visible.items()
    ):
        return _state(
            "failed",
            "Maintainerr settings did not match their readback.",
            verification="failed",
            retryable=True,
        )
    status = _request(client, "GET", "/api/media-server")
    actual_id = (
        (status or {}).get("id")
        or (status or {}).get("machineIdentifier")
        or (status or {}).get("machineId")
    )
    if not isinstance(status, dict) or str(actual_id or "") != identity:
        return _state(
            "waiting_dependency",
            "Maintainerr has not returned the selected server's authenticated identity yet. Recheck after its connector initializes.",
            retryable=True,
        )
    return _state(
        "applied" if changed else "already_present",
        "Maintainerr's selected server and connection settings were verified. No rule, collection or deletion policy was activated.",
        verification="passed",
        evidence={"connection": "passed", "policies_activated": False},
    )


def _dispatch(
    action, config, payload, operation, *, apply, client_factory, ownership_directory
):
    del operation
    supported = action.get("action") == "media_connection" and action.get(
        "service_key"
    ) in {"seerr", "maintainerr", "pulsarr", "aiostreams"}
    supported |= action.get("service_key") == "maintainerr" and action.get(
        "action"
    ) in {"seerr_connection", "tautulli_connection"}
    if not supported:
        return None
    if action["service_key"] == "pulsarr":
        return _state(
            "waiting_input",
            "Authorize the Plex account and watchlist owner in Pulsarr's UI. A managed server token does not establish the required personal account/watchlist consent.",
            verification="unavailable",
        )
    if action["service_key"] == "aiostreams":
        return _state(
            "waiting_input",
            "Choose and authorize the desired streaming-addon presets in AIOStreams. Account ownership, provider policy and client compatibility require explicit choices.",
            verification="unavailable",
        )
    try:
        if action["action"] in {"seerr_connection", "tautulli_connection"}:
            from utils.arrangement_app_companions import configure

            return configure(
                action,
                config,
                payload,
                apply=apply,
                factory=client_factory,
                ownership_directory=ownership_directory,
            )
        target = _target(action, config, payload, client_factory)
        if target is None:
            return _state(
                "waiting_input",
                "No media server was selected for this application. Choose a server or explicitly waive this connection task.",
            )
        handler = _seerr if action["service_key"] == "seerr" else _maintainerr
        return handler(
            action,
            config,
            payload,
            target,
            apply=apply,
            factory=client_factory,
            ownership_directory=ownership_directory,
        )
    except UnsupportedMediaVersion:
        return _state(
            "unsupported",
            "The selected media server returned an unsupported or ambiguous identity response.",
            verification="unavailable",
        )
    except AppMediaInputError as exc:
        return _state("waiting_input", str(exc))
    except ApiError as exc:
        if exc.status_code in (401, 403):
            return _state(
                "waiting_input",
                "The application rejected authorization. Finish its first administrator sign-in or provide its API key, then retry.",
                verification="failed",
            )
        if exc.status_code in (404, 405):
            return _state(
                "unsupported",
                "The installed application version does not expose the required selected-server API. Complete this connection in its UI.",
                verification="unavailable",
            )
        return _state(
            "waiting_dependency",
            "The application or selected server is not ready for the connection check.",
            retryable=True,
        )
    except (OSError, ValueError, TypeError, KeyError):
        return _state(
            "waiting_dependency",
            "Generated settings or the connection API are not ready. Recheck after startup.",
            retryable=True,
        )


def apply_app_media(
    action,
    config,
    payload,
    operation=None,
    *,
    client_factory=ApiClient,
    ownership_directory=None,
):
    return _dispatch(
        action,
        config,
        payload,
        operation,
        apply=True,
        client_factory=client_factory,
        ownership_directory=ownership_directory,
    )


def inspect_app_media(
    action,
    config,
    payload,
    operation=None,
    *,
    client_factory=ApiClient,
    ownership_directory=None,
):
    return _dispatch(
        action,
        config,
        payload,
        operation,
        apply=False,
        client_factory=client_factory,
        ownership_directory=ownership_directory,
    )
