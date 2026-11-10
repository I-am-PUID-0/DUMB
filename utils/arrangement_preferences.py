"""Explicit Lidarr root defaults and Bazarr subtitle-provider configuration."""

from __future__ import annotations

from utils.arrangement_http import ApiClient
from utils.arrangement_connections import (
    _scope,
    _values,
    _field,
    _native,
    _client,
    _request,
    _list,
    _ledger_path,
    _failure,
    InputNeeded,
    ApiError,
    UnsupportedContract,
)
from utils.arrangement_external import resolve_target, target_family
from utils.arrangement_adoption import preview


def schema_fields(resource, config, payload):
    source, family, _ = _scope(resource)
    if source.startswith("external_arr."):
        family = target_family(payload, source)
    common = [
        _field(
            source,
            "adopt_existing",
            "Accept reviewed native preference changes",
            "boolean",
            default=False,
        ),
        _field(source, "adoption_preview_id", "Accepted adoption preview"),
    ]
    if family == "lidarr":
        return [
            _field(
                source,
                "root_folder",
                "Music root folder to configure",
                "select",
                options=[],
            ),
            _field(
                source,
                "quality_profile",
                "Default music quality profile",
                "select",
                options=[],
            ),
            _field(
                source,
                "metadata_profile",
                "Default music metadata profile",
                "select",
                options=[],
            ),
            *common,
        ]
    if family == "bazarr":
        return [
            _field(
                source,
                "providers",
                "Subtitle providers",
                "list",
                default=[],
                max_items=32,
                refresh_on_change=True,
                item_label="Subtitle provider",
                add_label="Add subtitle provider",
                item_fields=[
                    {
                        "key": "id",
                        "label": "Provider",
                        "type": "select",
                        "options": [],
                        "required": True,
                    },
                    {
                        "key": "enable",
                        "label": "Enable this provider",
                        "type": "boolean",
                        "default": False,
                    },
                    {
                        "key": "authorize_changes",
                        "label": "Authorize saving provider credentials and activation",
                        "type": "boolean",
                        "default": False,
                    },
                ],
            ),
            *common,
        ]
    return []


def _lidarr(resource, config, payload, factory, *, write=False):
    source, _, _ = _scope(resource)
    if source.startswith("external_arr."):
        return resolve_target(
            payload, source, client_factory=factory, require_write=write
        )["client"]
    _, host, key = _native(config, source)
    if not key:
        raise InputNeeded("Waiting for Lidarr's generated API key.")
    client = factory(host, headers={"X-Api-Key": key}, managed=True)
    from utils.integration_ownership import fingerprint

    client._setup_ownership_scope = fingerprint(
        {"family": "lidarr", "url": host, "key": key}
    )
    return client


# Provider identifiers from Bazarr v1.6.2's official settings UI. /api/providers
# reports throttled/history providers, so an empty result is normal on first run.
# Only identifiers with native settings are added by this fallback; unrelated
# auth, media-server and notification sections never become subtitle providers.
_BAZARR_SETTINGS_PROVIDERS = frozenset("""
addic7ed animekalesi animetosho animetosho_xyz tsukihime animesubinfo avistaz
bayflix assrt betaseries bsplayer cinemaz embeddedsubtitles gestdown greeksubs
greeksubtitles hdbits jimaku hosszupuska karagarga ktuvit pipocas legendasdivx
legendasnet napiprojekt napisy24 nekur opensubtitlescom prijevodionline regielive
soustitreseu subclub subdl subf2m subsource subsarr subscenter subsdump subssabbz
subs4free subs4series subsro subsunacs subsynchro subtis subtitrarinoi subtitriid
subtitulamostv subx subtitlecat supersubtitles titlovi titrari titulky
turkcealtyaziorg tvsubtitles whisperai wizdom xsubs yavkanet yifysubtitles
vladoonmooo zimuku
""".split())


def _provider_inventory(client):
    document = _request(client, "GET", "/api/providers")
    if not isinstance(document, dict):
        raise UnsupportedContract(
            "This Bazarr version has no supported provider inventory."
        )
    rows = _list(document.get("data"))
    settings = _request(client, "GET", "/api/system/settings")
    if not isinstance(settings, dict):
        raise UnsupportedContract(
            "This Bazarr version has no supported provider settings."
        )
    settings = {key.lower(): value for key, value in settings.items()}
    existing = {row.get("name") for row in rows}
    rows.extend(
        {"name": name}
        for name in sorted(_BAZARR_SETTINGS_PROVIDERS - existing)
        if isinstance(settings.get(name), dict)
    )
    return rows, settings


def discover_fields(resource, config, payload, *, client_factory=ApiClient):
    fields = schema_fields(resource, config, payload)
    if not fields:
        return fields
    source, family, _ = _scope(resource)
    if source.startswith("external_arr."):
        family = target_family(payload, source)
    try:
        if family == "lidarr":
            client = _lidarr(resource, config, payload, client_factory)
            for field, route, id_key, label_key in zip(
                fields[:3],
                ("rootfolder", "qualityprofile", "metadataprofile"),
                ("path", "id", "id"),
                ("path", "name", "name"),
            ):
                rows = _list(_request(client, "GET", "/api/v1/" + route))
                field["options"] = [
                    {
                        "value": row[id_key],
                        "label": str(row.get(label_key) or row[id_key]),
                    }
                    for row in rows
                    if id_key in row
                ]
        else:
            client, _ = _client(config, source, _values(payload), client_factory)
            providers, settings = _provider_inventory(client)
            ids = {
                row.get("name") for row in providers if isinstance(row.get("name"), str)
            }
            fields[0]["item_fields"][0]["options"] = [
                {"value": name, "label": name} for name in sorted(ids)
            ]
            for selected in _values(payload).get(source + "::providers") or []:
                provider = selected.get("id")
                if provider not in ids or not isinstance(settings.get(provider), dict):
                    continue
                for key, value in settings[provider].items():
                    if (
                        not isinstance(value, (str, int, float, bool))
                        and value is not None
                    ):
                        continue
                    secret = any(
                        word in key.lower()
                        for word in (
                            "password",
                            "token",
                            "key",
                            "cookie",
                            "secret",
                            "username",
                            "login",
                            "email",
                        )
                    )
                    fields.append(
                        _field(
                            source,
                            "provider::" + provider + "::" + key,
                            provider + ": " + key.replace("_", " "),
                            (
                                "password"
                                if secret
                                else (
                                    "boolean"
                                    if isinstance(value, bool)
                                    else (
                                        "number"
                                        if isinstance(value, (int, float))
                                        else "string"
                                    )
                                )
                            ),
                            secret=secret,
                            **({} if secret else {"default": value}),
                        )
                    )
    except (InputNeeded, ApiError, ValueError, OSError, TypeError, KeyError):
        pass
    return fields


def apply_preferences(
    action,
    config,
    payload,
    operation="apply",
    *,
    client_factory=ApiClient,
    ownership_directory="/config/.dumb-setup/connections",
):
    if action.get("action") not in {"arr_preferences", "subtitle_providers"}:
        return None
    source, family, _ = _scope(action)
    if source.startswith("external_arr."):
        family = target_family(payload, source)
    values = _values(payload)
    try:
        if family == "lidarr":
            client = _lidarr(action, config, payload, client_factory)
            roots = _list(_request(client, "GET", "/api/v1/rootfolder"))
            root = next(
                (
                    row
                    for row in roots
                    if row.get("path") == values.get(source + "::root_folder")
                ),
                None,
            )
            if root is None:
                raise InputNeeded(
                    "Choose an existing music root folder.", [source + "::root_folder"]
                )
            qualities = _list(_request(client, "GET", "/api/v1/qualityprofile"))
            metadata = _list(_request(client, "GET", "/api/v1/metadataprofile"))
            quality = next(
                (
                    r["id"]
                    for r in qualities
                    if str(r.get("id")) == str(values.get(source + "::quality_profile"))
                ),
                None,
            )
            meta = next(
                (
                    r["id"]
                    for r in metadata
                    if str(r.get("id"))
                    == str(values.get(source + "::metadata_profile"))
                ),
                None,
            )
            if quality is None or meta is None:
                raise InputNeeded(
                    "Choose native Lidarr quality and metadata profiles.",
                    [source + "::quality_profile", source + "::metadata_profile"],
                )
            before = {
                key: root.get(key)
                for key in ("defaultQualityProfileId", "defaultMetadataProfileId")
            }
            after = {
                "defaultQualityProfileId": quality,
                "defaultMetadataProfileId": meta,
            }
            desired = {**root, **after}
        elif family == "bazarr":
            client, _ = _client(config, source, values, client_factory)
            providers, settings = _provider_inventory(client)
            ids = {row.get("name") for row in providers}
            selected = values.get(source + "::providers") or []
            if not isinstance(selected, list) or len(selected) > 32:
                raise InputNeeded("Choose a bounded list of subtitle providers.")
            enabled = set(
                (settings.get("general") or {}).get("enabled_providers") or []
            )
            before, after, form = {}, {}, {}
            for entry in selected:
                provider = entry.get("id")
                if provider not in ids or not isinstance(settings.get(provider), dict):
                    raise InputNeeded(
                        "Choose providers advertised by the installed Bazarr version."
                    )
                for key, current in settings[provider].items():
                    input_key = source + "::provider::" + provider + "::" + key
                    if input_key not in values or values[input_key] in ("", None):
                        continue
                    value = values[input_key]
                    if value == "[configured]":
                        raise InputNeeded(
                            "Provide actual provider credentials rather than a redaction marker."
                        )
                    if current != value:
                        before[provider + "." + key], after[provider + "." + key] = (
                            current,
                            value,
                        )
                        form["settings-" + provider + "-" + key] = (
                            str(value).lower()
                            if isinstance(value, bool)
                            else str(value)
                        )
                # Existing providers stay enabled unless disabled in their native UI.
                # Enabling a newly chosen provider is an explicit per-entry choice.
                if entry.get("enable", False):
                    enabled.add(provider)
                if any(k.startswith(provider + ".") for k in after) or entry.get(
                    "enable", False
                ):
                    if not entry.get("authorize_changes", False) and operation not in {
                        "inspect",
                        "preview",
                    }:
                        raise InputNeeded(
                            "Authorize saving the selected subtitle provider settings.",
                            [source + "::providers"],
                        )
            old_enabled = sorted(
                (settings.get("general") or {}).get("enabled_providers") or []
            )
            if sorted(enabled) != old_enabled:
                before["enabled_providers"], after["enabled_providers"] = (
                    old_enabled,
                    sorted(enabled),
                )
                form["settings-general-enabled_providers"] = sorted(enabled)
        else:
            return None
        proposal = preview(
            _ledger_path(
                ownership_directory,
                source + ".preferences-preview",
                write=operation == "preview",
            ),
            source + "::" + action["action"] + "::" + client._setup_ownership_scope,
            before,
            after,
            managed=False,
            operation=operation,
            accepted_id=values.get(source + "::adoption_preview_id"),
            consent=values.get(source + "::adopt_existing", False),
        )
        if operation in {"inspect", "preview"}:
            return {
                "execution": (
                    "waiting_input" if proposal["changes"] else "already_present"
                ),
                "verification": "not_checked",
                "reason": "Native preference changes were inspected without saving.",
                "retryable": False,
                "evidence": proposal,
            }
        if before != after:
            if operation in {"verify", "recheck"}:
                raise InputNeeded(
                    "Apply the accepted native preference proposal before verification."
                )
            if not proposal["approved"]:
                return {
                    "execution": "waiting_input",
                    "verification": "not_checked",
                    "retryable": False,
                    "reason": "Preview and accept these exact native preference changes before saving.",
                    "evidence": proposal,
                    "missing_inputs": [
                        source + "::adopt_existing",
                        source + "::adoption_preview_id",
                    ],
                }
            if family == "lidarr":
                client = _lidarr(action, config, payload, client_factory, write=True)
                _request(
                    client, "PUT", "/api/v1/rootfolder/" + str(root["id"]), body=desired
                )
            else:
                _request(client, "POST", "/api/system/settings", form=form)
        if family == "lidarr":
            saved = next(
                (
                    r
                    for r in _list(_request(client, "GET", "/api/v1/rootfolder"))
                    if r.get("id") == root["id"]
                ),
                {},
            )
            passed = all(saved.get(key) == value for key, value in after.items())
        else:
            _, saved = _provider_inventory(client)
            passed = all(
                (
                    ((saved.get("general") or {}).get("enabled_providers") or [])
                    == value
                    if key == "enabled_providers"
                    else (saved.get(key.split(".", 1)[0]) or {}).get(
                        key.split(".", 1)[1]
                    )
                    == value
                )
                for key, value in after.items()
            )
        if not passed:
            raise ApiError(
                "Native preference readback did not match the reviewed values."
            )
        return {
            "execution": "applied" if before != after else "already_present",
            "verification": "passed" if family == "lidarr" else "unavailable",
            "retryable": False,
            "reason": (
                "Lidarr root-folder profile defaults were read back; existing artists, monitors and tags were preserved."
                if family == "lidarr"
                else "Subtitle provider settings were read back. Third-party provider login and subtitle availability require a native search using your account."
            ),
            "evidence": (
                {"level": "configuration_readback", "provider_login_verified": False}
                if family == "bazarr"
                else {"level": "configuration_readback"}
            ),
        }
    except (
        InputNeeded,
        UnsupportedContract,
        ApiError,
        ValueError,
        OSError,
        TypeError,
        KeyError,
    ) as exc:
        return _failure(exc)


def inspect_preferences(action, config, payload, operation="inspect", **kwargs):
    return apply_preferences(
        action,
        config,
        payload,
        operation if operation in {"preview", "verify", "recheck"} else "inspect",
        **kwargs,
    )
