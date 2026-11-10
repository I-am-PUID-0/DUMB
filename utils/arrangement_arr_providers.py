"""Connect an existing Arr to explicitly selected DUMB download providers.

External filesystems are inspected only through the Arr API. Paths are never
created on the controller, and native settings outside the chosen client/root/
mapping are preserved. Provider-side configuration has its own scoped adapter.
"""

from __future__ import annotations

import copy
import posixpath
from urllib.parse import urlsplit

from utils.arrangement_http import ApiClient, ApiError, validate_endpoint
from utils.arrangement_external import resolve_target, target_family
from utils.arrangement_connections import (
    _scope,
    _values,
    _field,
    _request,
    _list,
    _ledger_path,
    _failure,
    InputNeeded,
)
from utils.arrangement_adoption import preview
from utils.integration_ownership import fingerprint, read_ownership, write_ownership

PROVIDERS = ("decypharr", "infinidysk", "altmount")


def schema_fields(resource, config, payload):
    source, _, _ = _scope(resource)
    if not source.startswith("external_arr.") or not target_family(payload, source):
        return []
    enabled = [key for key in PROVIDERS if (config.get(key) or {}).get("enabled")]
    values = _values(payload)
    fields = [
        _field(
            source,
            "providers",
            "Connect DUMB download providers",
            "multiselect",
            options=[{"value": key, "label": key.capitalize()} for key in enabled],
            refresh_on_change=True,
        ),
        _field(
            source,
            "root_folder",
            "Existing external library root",
            "select",
            options=[],
        ),
        _field(
            source,
            "new_root_folder",
            "Create an additional library root in the external Arr namespace",
        ),
        _field(
            source,
            "adopt_existing",
            "Accept the exact existing provider connection changes",
            "boolean",
            default=False,
        ),
        _field(source, "adoption_preview_id", "Accepted adoption preview"),
    ]
    for provider in values.get(source + "::providers") or []:
        if provider not in enabled:
            continue
        prefix = provider + "::"
        fields.extend(
            [
                _field(
                    source,
                    prefix + "client_url",
                    provider + ": API URL reachable from this external Arr",
                    description="Use the provider's direct API base URL, as reachable by the external server. A controller loopback address is usually not reachable from another server.",
                ),
                _field(
                    source,
                    prefix + "allow_private_client",
                    provider + ": allow a private provider endpoint",
                    "boolean",
                    default=False,
                ),
                _field(
                    source,
                    prefix + "category",
                    provider + ": dedicated download category",
                    default="dumb-" + source.partition(".")[2] + "-" + provider,
                ),
                _field(
                    source,
                    prefix + "remote_path",
                    provider + ": completed path reported by the provider",
                ),
                _field(
                    source,
                    prefix + "local_path",
                    provider + ": same completed path as seen by this external Arr",
                ),
            ]
        )
        if provider == "decypharr":
            fields.append(
                _field(
                    source,
                    prefix + "restart_after_config",
                    "Authorize a Decypharr-only restart if its SAB category cache needs reloading",
                    "boolean",
                    default=False,
                )
            )
        if provider == "altmount":
            fields.append(
                _field(
                    source,
                    prefix + "enable_api",
                    "Enable AltMount's deliberately disabled SAB API",
                    "boolean",
                    default=False,
                )
            )
            fields.append(
                _field(
                    source,
                    prefix + "enable_callbacks",
                    "Enable AltMount's native Arr callbacks (including existing enabled registrations)",
                    "boolean",
                    default=False,
                )
            )
    return fields


def discover_fields(resource, config, payload, *, client_factory=ApiClient):
    fields = schema_fields(resource, config, payload)
    if not fields:
        return []
    source, _, _ = _scope(resource)
    try:
        target = resolve_target(payload, source, client_factory=client_factory)
        roots = _list(
            _request(target["client"], "GET", target["api_prefix"] + "/rootfolder")
        )
        fields[1]["options"] = [
            {"value": row["path"], "label": row["path"]}
            for row in roots
            if isinstance(row.get("path"), str)
        ]
    except (ValueError, ApiError, OSError):
        pass
    return fields


def _path(value):
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or len(value) > 4096
        or any(c in value for c in "\0\r\n\\")
    ):
        raise InputNeeded("Use an absolute Linux path in the external Arr namespace.")
    if any(part == ".." for part in value.split("/")):
        raise InputNeeded(
            "Parent-directory traversal is not supported in external path mappings."
        )
    return posixpath.normpath(value)


def _fields(row):
    return {
        field["name"]: field.get("value")
        for field in row.get("fields") or []
        if "name" in field
    }


def _compare(row):
    return {
        **{
            key: row.get(key)
            for key in (
                "name",
                "enable",
                "implementation",
                "removeCompletedDownloads",
                "removeFailedDownloads",
                "tags",
            )
        },
        **{"setting." + key: value for key, value in _fields(row).items()},
    }


def _provider(
    config,
    payload,
    provider,
    target,
    category,
    operation,
    factory,
    directory,
    *,
    approved_provider_preview=None,
):
    from utils.arrangement_external_provider import (
        external_arr_provider_credentials_and_category,
    )

    result = external_arr_provider_credentials_and_category(
        config,
        payload,
        provider,
        target,
        category,
        operation,
        client_factory=factory,
        ownership_directory=directory,
        approved_provider_preview=approved_provider_preview,
    )
    return result


def _retire(
    action, payload, provider, operation, factory, directory, protocol_suffix=""
):
    source = action["source"]
    target = resolve_target(payload, source, client_factory=factory)
    client, api = target["client"], target["api_prefix"]
    path = _ledger_path(
        directory,
        source
        + "."
        + provider
        + ("." + protocol_suffix if protocol_suffix else "")
        + ".client",
    )
    record = read_ownership(path).get(provider) or {}
    if record.get("endpoint_scope") != target["ownership_scope"] or not isinstance(
        record.get("id"), int
    ):
        raise InputNeeded(
            "The removed external provider client has no protected ownership. Review it in the external Arr."
        )
    rows = _list(_request(client, "GET", api + "/downloadclient"))
    owned = next((row for row in rows if row.get("id") == record["id"]), None)
    if owned:
        identity = fingerprint(
            {
                key: _fields(owned).get(key)
                for key in ("host", "port", "urlBase", "category")
            }
        )
        if identity != record.get("identity"):
            raise InputNeeded(
                "The removed provider client changed outside DUMB. Review its native identity before retirement."
            )
        if owned.get("enable", True):
            if operation in {"inspect", "preview", "recheck", "verify"}:
                return {
                    "execution": "waiting_input",
                    "verification": "not_checked",
                    "retryable": False,
                    "reason": "Apply retirement to disable only the owned external provider client. Roots and path mappings are preserved.",
                }
            resolve_target(payload, source, client_factory=factory, require_write=True)
            _request(
                client,
                "PUT",
                api + "/downloadclient/" + str(owned["id"]),
                body={**owned, "enable": False},
            )
            rows = _list(_request(client, "GET", api + "/downloadclient"))
            if any(
                row.get("id") == owned["id"] and row.get("enable", True) for row in rows
            ):
                raise ApiError("The owned external provider client remains enabled.")
    return {
        "execution": "applied",
        "verification": "passed",
        "retryable": False,
        "reason": "The removed owned external provider client was read back as disabled or absent. Library roots and mappings were preserved.",
        "evidence": {
            "level": "configuration_readback",
            "retired": True,
            "provider": provider,
        },
    }


def _apply_single(
    action,
    config,
    payload,
    operation="apply",
    *,
    client_factory=ApiClient,
    ownership_directory="/config/.dumb-setup/connections",
    provider_adapter=None,
    protocol_suffix="",
):
    source, _, _ = _scope(action)
    provider = str(action.get("action") or "").removeprefix("provider.")
    if (
        not source.startswith("external_arr.")
        or not str(action.get("action", "")).startswith("provider.")
        or provider not in PROVIDERS
    ):
        return None
    handoff = {}
    try:
        values = _values(payload)
        if provider not in (values.get(source + "::providers") or []):
            if action.get("retirement_pending") or action.get("retirement_required"):
                return _retire(
                    action,
                    payload,
                    provider,
                    operation,
                    client_factory,
                    ownership_directory,
                    protocol_suffix,
                )
            return {
                "execution": "declined",
                "verification": "unavailable",
                "retryable": False,
                "reason": "This external Arr/provider connection is not selected. Existing native settings are preserved.",
            }
        if not (config.get(provider) or {}).get("enabled"):
            raise InputNeeded("Enable the selected managed download provider first.")
        target = resolve_target(payload, source, client_factory=client_factory)
        client, api = target["client"], target["api_prefix"]
        prefix = source + "::" + provider + "::"
        url = values.get(prefix + "client_url")
        validate_endpoint(
            url, allow_private=values.get(prefix + "allow_private_client", False)
        )
        parsed = urlsplit(url)
        category = (
            values.get(prefix + "category")
            or "dumb-" + source.partition(".")[2] + "-" + provider
        )
        if (
            not isinstance(category, str)
            or not category
            or len(category) > 120
            or any(c in category for c in "/\\\r\n")
        ):
            raise InputNeeded(
                "Use a dedicated provider category without path separators."
            )
        provider_callback = provider_adapter or _provider
        credentials = provider_callback(
            config,
            payload,
            provider,
            target,
            category,
            "inspect",
            client_factory,
            ownership_directory,
        )
        if credentials.get("execution"):
            return {
                key: value
                for key, value in credentials.items()
                if key
                not in {
                    "fields",
                    "implementation",
                    "_provider_adoption",
                    "additional_clients",
                }
            }
        implementation = credentials.get("implementation")
        if implementation not in {"QBittorrent", "Sabnzbd"} or not isinstance(
            credentials.get("fields"), dict
        ):
            raise InputNeeded(
                "The selected provider version does not expose a supported native client contract."
            )
        schemas = _list(_request(client, "GET", api + "/downloadclient/schema"))
        schema = next(
            (row for row in schemas if row.get("implementation") == implementation),
            None,
        )
        if schema is None:
            raise InputNeeded(
                "The external Arr version has no schema for this provider's protocol."
            )
        rows = _list(_request(client, "GET", api + "/downloadclient"))
        ledger_path = _ledger_path(
            ownership_directory,
            source
            + "."
            + provider
            + ("." + protocol_suffix if protocol_suffix else "")
            + ".client",
        )
        ledger = read_ownership(ledger_path)
        saved_record = ledger.get(provider) or {}
        record = (
            saved_record
            if saved_record.get("endpoint_scope") == target["ownership_scope"]
            else {}
        )
        match = next((row for row in rows if row.get("id") == record.get("id")), None)
        native_name = (
            "DUMB "
            + provider
            + " "
            + source.partition(".")[2]
            + (" " + protocol_suffix if protocol_suffix else "")
        )
        candidates = [row for row in rows if row.get("name") == native_name]
        if match is None and len(candidates) == 1:
            match = candidates[0]
        if len(candidates) > 1:
            raise InputNeeded(
                "Several native download clients match this external provider assignment."
            )
        owned = bool(
            match
            and match.get("id") == record.get("id")
            and record.get("identity")
            == fingerprint(
                {
                    key: _fields(match).get(key)
                    for key in ("host", "port", "urlBase", "category")
                }
            )
        )
        desired = copy.deepcopy(match or schema)
        if not match:
            desired.pop("id", None)
            desired.update(
                name=native_name,
                enable=True,
                priority=1,
                removeCompletedDownloads=False,
                removeFailedDownloads=False,
                tags=[],
            )
        overrides = {
            "host": parsed.hostname,
            "port": parsed.port or (443 if parsed.scheme == "https" else 80),
            "useSsl": parsed.scheme == "https",
            "urlBase": parsed.path,
            **credentials["fields"],
        }
        native_base = str(credentials["fields"].get("urlBase") or "").strip("/")
        public_base = parsed.path.rstrip("/")
        # The supplied URL already names the application's reachable base.
        # Native Decypharr fields include its own application prefix; append
        # only the protocol mount, never repeat that prefix through a proxy.
        protocol_mount = (
            "sabnzbd"
            if implementation == "Sabnzbd" and provider in {"decypharr", "altmount"}
            else ""
        )
        if protocol_mount and not public_base.endswith("/" + protocol_mount):
            public_base += "/" + protocol_mount
        elif (
            provider not in {"decypharr", "altmount"}
            and native_base
            and not public_base.endswith("/" + native_base)
        ):
            public_base += "/" + native_base
        overrides["urlBase"] = public_base
        category_keys = {
            "category",
            "torrentcategory",
            "moviecategory",
            "tvcategory",
            "musiccategory",
            "nzbcategory",
        }
        found_category = False
        for field in desired.get("fields") or []:
            name = field.get("name") or ""
            if name.lower() in category_keys:
                field["value"] = category
                found_category = True
            elif name.lower() in {"usecategory", "usetags"}:
                field["value"] = True
            elif name in overrides:
                field["value"] = overrides[name]
        if not found_category:
            raise InputNeeded(
                "The native client schema does not expose an isolated download category."
            )
        roots = _list(_request(client, "GET", api + "/rootfolder"))
        new_root = values.get(source + "::new_root_folder")
        root_path = _path(new_root or values.get(source + "::root_folder"))
        root = next(
            (
                row
                for row in roots
                if posixpath.normpath(row.get("path") or "") == root_path
            ),
            None,
        )
        if root and root.get("accessible") is False:
            raise InputNeeded(
                "The external Arr reports that the chosen library root is inaccessible. Correct its mount or permissions."
            )
        if not root and not new_root:
            raise InputNeeded(
                "Choose an existing root or explicitly request an additional native root."
            )
        root_payload = {"path": root_path}
        if not root and target["family"] == "lidarr":
            for kind, native in (
                ("qualityprofile", "defaultQualityProfileId"),
                ("metadataprofile", "defaultMetadataProfileId"),
            ):
                choices = _list(_request(client, "GET", api + "/" + kind))
                field = (
                    "quality_profile"
                    if kind == "qualityprofile"
                    else "metadata_profile"
                )
                chosen = next(
                    (
                        row["id"]
                        for row in choices
                        if str(row.get("id")) == str(values.get(source + "::" + field))
                    ),
                    None,
                )
                if chosen is None:
                    raise InputNeeded(
                        "Choose Lidarr quality and metadata defaults before creating its root.",
                        [source + "::" + field],
                    )
                root_payload[native] = chosen
            root_payload.update(
                name="DUMB " + source.partition(".")[2],
                defaultMonitorOption="none",
                defaultNewItemMonitorOption="none",
                defaultTags=[],
            )
        remote, local = values.get(prefix + "remote_path"), values.get(
            prefix + "local_path"
        )
        mappings, mapping, mapping_payload = [], None, None
        if remote or local:
            remote, local = _path(remote), _path(local)
            mappings = _list(_request(client, "GET", api + "/remotepathmapping"))
            matches = [
                row
                for row in mappings
                if row.get("host") == parsed.hostname
                and row.get("remotePath") == remote
            ]
            if len(matches) > 1:
                raise InputNeeded(
                    "Several native remote path mappings match this provider."
                )
            mapping = matches[0] if matches else None
            mapping_payload = {
                **(mapping or {}),
                "host": parsed.hostname,
                "remotePath": remote,
                "localPath": local,
            }
        before, after = _compare(match or {}), _compare(desired)
        auth_digest = fingerprint(credentials["fields"])
        if owned and record.get("auth_digest") == auth_digest:
            for key in before:
                if before[key] == "********" and key in after:
                    before[key] = after[key]
        if not protocol_suffix:
            before["root_folder"] = root_path if root else None
            after["root_folder"] = root_path
        if mapping_payload and not protocol_suffix:
            before["path_mapping"] = mapping
            after["path_mapping"] = mapping_payload
        provider_adoption = credentials.get("_provider_adoption") or {}
        native_adoption_required = bool(provider_adoption.get("requires_consent"))
        for side, destination in (("before", before), ("after", after)):
            native_fields = provider_adoption.get(side) or {}
            if not isinstance(native_fields, dict):
                raise InputNeeded(
                    "The provider returned an unsupported registration preview."
                )
            destination.update(
                {
                    "provider_native." + key: value
                    for key, value in native_fields.items()
                }
            )
        proposal = preview(
            _ledger_path(
                ownership_directory,
                source
                + "."
                + provider
                + ("." + protocol_suffix if protocol_suffix else "")
                + ".preview",
                write=operation == "preview",
            ),
            source
            + "::provider."
            + provider
            + "::"
            + target["ownership_scope"]
            + "::"
            + protocol_suffix,
            before,
            after,
            managed=owned or not match,
            operation=operation,
            accepted_id=values.get(source + "::adoption_preview_id"),
            consent=values.get(source + "::adopt_existing", False),
        )
        different = before != after
        mapping_changes_unowned = bool(
            mapping
            and mapping != mapping_payload
            and record.get("mapping_id") != mapping.get("id")
        )
        if native_adoption_required:
            proposal["requires_consent"] = True
            for change in proposal["changes"]:
                if change["field"].startswith("provider_native."):
                    change.update(managed=False, requires_consent=True)
        if mapping_changes_unowned:
            proposal["requires_consent"] = True
            for change in proposal["changes"]:
                if change["field"] == "path_mapping":
                    change.update(managed=False, requires_consent=True)
        if operation in {"inspect", "preview"}:
            return {
                "execution": "waiting_input" if different else "already_present",
                "verification": "not_checked",
                "reason": "External provider client, root and path mapping fields were inspected without writes.",
                "retryable": False,
                "evidence": proposal,
            }
        if operation in {"verify", "recheck"} and different:
            raise InputNeeded(
                "Apply the reviewed external provider configuration before verification."
            )
        if (
            different
            and (
                (match and not owned)
                or mapping_changes_unowned
                or native_adoption_required
            )
            and not proposal["approved"]
        ):
            return {
                "execution": "waiting_input",
                "verification": "not_checked",
                "retryable": False,
                "reason": "Accept the fresh field-by-field preview before changing this existing external configuration.",
                "evidence": proposal,
                "missing_inputs": [
                    source + "::adopt_existing",
                    source + "::adoption_preview_id",
                ],
            }
        if operation in {"apply", "retry", "start"}:
            resolve_target(
                payload, source, client_factory=client_factory, require_write=True
            )
            provisioned = provider_callback(
                config,
                payload,
                provider,
                target,
                category,
                "apply",
                client_factory,
                ownership_directory,
                approved_provider_preview=(
                    provider_adoption.get("fingerprint")
                    if proposal["approved"]
                    else None
                ),
            )
            if provisioned.get("execution"):
                return {
                    key: value
                    for key, value in provisioned.items()
                    if key
                    not in {
                        "fields",
                        "implementation",
                        "_provider_adoption",
                        "additional_clients",
                    }
                }
            handoff = {
                key: provisioned[key]
                for key in ("_private_values", "_clear_inputs", "_clear_on_handoff")
                if key in provisioned
            }
            if provisioned.get("_provider_pending"):
                return {
                    "execution": "waiting_dependency",
                    "verification": "not_checked",
                    "retryable": True,
                    "reason": "Waiting for the provider's registered callback and category to become available before saving the external Arr client.",
                    "evidence": provisioned.get("evidence") or {},
                    **handoff,
                }
        if different:
            # Native connection tests may authenticate and create the explicitly
            # isolated category; they never enqueue content or issue searches.
            findings = _request(
                client, "POST", api + "/downloadclient/test", body=desired
            )
            if findings not in (None, [], {}, "", "{}"):
                raise InputNeeded(
                    "The external Arr could not authenticate its selected provider connection."
                )
            if _compare(match or {}) != _compare(desired):
                saved = _request(
                    client,
                    "PUT" if match else "POST",
                    api + "/downloadclient" + ("/" + str(match["id"]) if match else ""),
                    body=desired,
                )
                saved_id = match["id"] if match else (saved or {}).get("id")
                if not isinstance(saved_id, int):
                    raise ApiError(
                        "Native provider configuration did not return a saved identity."
                    )
                record["id"] = saved_id
                record["endpoint_scope"] = target["ownership_scope"]
                record["auth_digest"] = auth_digest
                record["identity"] = fingerprint(
                    {
                        key: _fields(desired).get(key)
                        for key in ("host", "port", "urlBase", "category")
                    }
                )
                ledger[provider] = record
                write_ownership(
                    _ledger_path(
                        ownership_directory,
                        source
                        + "."
                        + provider
                        + ("." + protocol_suffix if protocol_suffix else "")
                        + ".client",
                        write=True,
                    ),
                    ledger,
                )
            if not root:
                _request(client, "POST", api + "/rootfolder", body=root_payload)
            if mapping_payload and mapping != mapping_payload:
                saved = _request(
                    client,
                    "PUT" if mapping else "POST",
                    api
                    + "/remotepathmapping"
                    + ("/" + str(mapping["id"]) if mapping else ""),
                    body=mapping_payload,
                )
                record["mapping_id"] = (
                    mapping["id"] if mapping else (saved or {}).get("id")
                )
                ledger[provider] = record
                write_ownership(
                    _ledger_path(
                        ownership_directory,
                        source
                        + "."
                        + provider
                        + ("." + protocol_suffix if protocol_suffix else "")
                        + ".client",
                        write=True,
                    ),
                    ledger,
                )
        saved = next(
            (
                row
                for row in _list(_request(client, "GET", api + "/downloadclient"))
                if row.get("id") == record.get("id")
            ),
            match,
        )
        if not saved:
            raise ApiError("External provider client could not be read back.")
        actual = _compare(saved)
        wanted = _compare(desired)
        for key in actual:
            if actual[key] == "********" and key in wanted:
                actual[key] = wanted[key]
        if actual != wanted:
            raise ApiError("External provider fields did not match native readback.")
        findings = _request(client, "POST", api + "/downloadclient/test", body=saved)
        if findings not in (None, [], {}, "", "{}"):
            raise InputNeeded(
                "The external Arr's saved provider connection test reported findings."
            )
        read_roots = _list(_request(client, "GET", api + "/rootfolder"))
        if not any(
            posixpath.normpath(row.get("path") or "") == root_path
            and row.get("accessible", True)
            for row in read_roots
        ):
            raise ApiError("The external Arr did not confirm its selected root path.")
        if mapping_payload and not any(
            all(
                row.get(key) == value
                for key, value in mapping_payload.items()
                if key != "id"
            )
            for row in _list(_request(client, "GET", api + "/remotepathmapping"))
        ):
            raise ApiError("The external Arr did not return the selected path mapping.")
        return {
            "execution": "applied" if different else "already_present",
            "verification": "passed",
            "retryable": False,
            "reason": "The external Arr's provider connection was saved, authenticated and read back with its selected root and applicable path mapping. Download/import remain separate workflow checks.",
            **handoff,
            "evidence": {
                "level": "connection_test",
                "provider": provider,
                "root_readback": True,
                "remote_path_mapping_readback": bool(mapping_payload),
                "downloads_started": False,
            },
        }
    except (
        InputNeeded,
        ApiError,
        ValueError,
        OSError,
        TypeError,
        KeyError,
        ImportError,
    ) as exc:
        return {**_failure(exc), **handoff}


def apply_arr_provider(
    action,
    config,
    payload,
    operation="apply",
    *,
    client_factory=ApiClient,
    ownership_directory="/config/.dumb-setup/connections",
    provider_adapter=None,
):
    if action.get("action") == "provider.decypharr" and str(
        action.get("source", "")
    ).startswith("external_arr."):
        from utils.arrangement_arr_provider_group import apply_dual_provider

        return apply_dual_provider(
            action,
            config,
            payload,
            operation,
            single=_apply_single,
            provider_adapter=provider_adapter or _provider,
            client_factory=client_factory,
            ownership_directory=ownership_directory,
        )
    return _apply_single(
        action,
        config,
        payload,
        operation,
        client_factory=client_factory,
        ownership_directory=ownership_directory,
        provider_adapter=provider_adapter,
    )


def inspect_arr_provider(action, config, payload, operation="inspect", **kwargs):
    return apply_arr_provider(
        action,
        config,
        payload,
        operation if operation in {"preview", "verify", "recheck"} else "inspect",
        **kwargs,
    )
