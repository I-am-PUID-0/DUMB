"""Pure arrangement planning and coverage; planning never starts applications."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
from urllib.parse import quote, urlsplit

from utils.arrangement_catalog import (
    ARR_KEYS,
    CORE_KEYS,
    MANUAL_TASKS,
    MEDIA_KEYS,
    OPTIONAL_KEYS,
    PROVIDER_KEYS,
    SERVICE_NAMES,
    docs_url,
    service_roles,
)

PLAN_VERSION = 1
MAX_ACTIONS = 1200
MAX_RESOURCES = 100
SECRET_PATTERN = re.compile(
    r"password|token|secret|api.?key|debrid_key|username|credential", re.I
)


def public_value(value, key=""):
    """Remove credentials recursively, including credentials inside list inputs."""
    if type(value) is bool:
        return value
    if SECRET_PATTERN.search(str(key)):
        return "[configured]" if value else ""
    if isinstance(value, dict):
        return {str(k): public_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [public_value(v) for v in value]
    if isinstance(value, str):
        if "://" in value:
            try:
                parts = urlsplit(value)
            except ValueError:
                return "[configured URL]"
            if parts.username or parts.password or parts.query or parts.fragment:
                return "[configured URL]"
        return value
    return value


def public_values(values, fields):
    """Honor advertised secret fields even when upstream names lack secret words."""

    def redact(value, field, key):
        if field.get("type") == "password" or field.get("secret") is True:
            return "[configured]" if value else ""
        if field.get("type") == "object" and isinstance(value, dict):
            children = {child["key"]: child for child in field.get("fields") or []}
            return {
                name: redact(child, children.get(name, {}), name)
                for name, child in value.items()
            }
        if field.get("type") == "list" and isinstance(value, list):
            children = {child["key"]: child for child in field.get("item_fields") or []}
            return [
                (
                    {
                        name: redact(child, children.get(name, {}), name)
                        for name, child in row.items()
                    }
                    if isinstance(row, dict)
                    else public_value(row)
                )
                for row in value
            ]
        return public_value(value, key)

    by_key = {field["key"]: field for field in fields}
    return {
        key: redact(value, by_key.get(key, {}), key) for key, value in values.items()
    }


def resource_id(key: str, instance: str | None = None) -> str:
    return f"{key}.{instance}" if instance else key


def resource_config(config: dict, identifier: str) -> dict:
    key, _, instance = identifier.partition(".")
    block = config.get(key, {})
    if not isinstance(block, dict):
        return {}
    return (block.get("instances", {}).get(instance, {}) if instance else block) or {}


def clean_instance_name(value):
    name = str(value or "").strip()
    if len(name) > 80 or not re.fullmatch(r"[A-Za-z0-9 _-]+", name):
        raise ValueError(
            "Instance names must contain 1–80 letters, digits, spaces, underscores or hyphens."
        )
    return name


def _core_list(payload):
    cores = payload.get("core_services") or []
    return [cores] if isinstance(cores, dict) else cores


def project_configuration(
    payload: dict, current: dict, defaults: dict
) -> tuple[dict, list[dict]]:
    """Materialize selected instances in a copy, preserving unrelated state."""
    proposed = copy.deepcopy(current)
    resources = []
    seen = set()
    entries = [(entry, "chosen") for entry in _core_list(payload)]
    for key in payload.get("optional_services") or []:
        entries.append(
            (
                {
                    "name": key,
                    "service_options": {
                        key: (payload.get("optional_service_options") or {}).get(
                            key, {}
                        )
                    },
                },
                "chosen",
            )
        )
    if len(entries) > MAX_RESOURCES:
        raise ValueError("Too many selected services.")
    for entry, selection in entries:
        key = str(entry.get("name") or "").strip().lower()
        if key == "nzbdav":
            key = "infinidysk"
        if key not in CORE_KEYS | OPTIONAL_KEYS or key not in defaults:
            raise ValueError("The selected service is not available for onboarding.")
        block = proposed.setdefault(key, copy.deepcopy(defaults[key]))
        options = (entry.get("service_options") or {}).get(key, {})
        if not isinstance(options, dict):
            raise ValueError("Service options must be an object.")
        instance = entry.get("instance_name")
        if "instances" in defaults[key]:
            templates = defaults[key]["instances"]
            instances = block.setdefault("instances", {})
            if instance:
                instance = clean_instance_name(instance)
            else:
                active = [
                    name
                    for name, item in instances.items()
                    if isinstance(item, dict) and item.get("enabled")
                ]
                if len(active) > 1:
                    raise ValueError(
                        "Choose an explicit instance when a service has multiple active instances."
                    )
                instance = active[0] if active else next(iter(templates))
            if instance not in instances:
                candidate = copy.deepcopy(next(iter(templates.values())))
                slug = instance.lower().replace(" ", "_")
                for field in ("config_dir", "config_file", "log_file"):
                    if isinstance(candidate.get(field), str):
                        candidate[field] = candidate[field].replace(
                            f"/{key}/default", f"/{key}/{slug}"
                        )
                candidate["process_name"] = f"{SERVICE_NAMES.get(key, key)} {instance}"
                instances[instance] = candidate
            target = instances[instance]
            options = {
                **options,
                **(entry.get("service_options") or {}).get(instance, {}),
            }
        else:
            if instance:
                raise ValueError("This service does not support named instances.")
            target = block
        target.update(
            {k: copy.deepcopy(v) for k, v in options.items() if v is not None}
        )
        if key == "infinidysk":
            from utils.arrangement_infinidysk import project_choice

            project_choice(
                resource_config(current, "infinidysk"),
                target,
                options,
                (payload.get("arrangement") or {}).get("values") or {},
                current,
            )
        target["enabled"] = True
        rid = resource_id(key, instance)
        values = (payload.get("arrangement") or {}).get("values") or {}
        for field in ("battery_mode", "use_phalanx_db", "arr_instances"):
            if field == "arr_instances" and key not in {
                "prowlarr",
                "neutarr",
                "profilarr",
            }:
                continue
            choice = f"{rid}::{field}"
            if choice in values:
                target[field] = copy.deepcopy(values[choice])
        if rid in seen:
            raise ValueError("A service instance was selected more than once.")
        seen.add(rid)
        resources.append(
            {
                "id": rid,
                "service_key": key,
                "instance_key": instance,
                "name": SERVICE_NAMES.get(key, key)
                + (f" — {instance}" if instance else ""),
                "selection_kind": selection,
                "roles": service_roles(key),
                "process_name": target.get("process_name", ""),
                "already_enabled": bool(resource_config(current, rid).get("enabled")),
            }
        )
    for resource in resources:
        if resource["service_key"] == "infinidysk":
            from utils.arrangement_infinidysk import topology

            cfg = resource_config(proposed, resource["id"])
            resource["runtime_topology"] = topology(
                cfg, _dependencies("infinidysk", cfg)
            )
    return proposed, resources


def _dependencies(key, cfg):
    dependencies = (
        {
            "riven_backend": ["postgres"],
            "riven_frontend": ["riven_backend"],
            "cli_debrid": ["zurg", "rclone"],
            "infinidysk": ["rclone"],
            "zilean": ["postgres"],
            "pgadmin": ["postgres"],
            "mediastorm": ["postgres"],
            "traefik_proxy_admin": ["postgres", "traefik"],
            "authelia": ["postgres", "traefik"],
            "cloudflared": ["traefik"],
        }
        .get(key, [])
        .copy()
    )
    if key == "riven_backend" and not (
        cfg.get("branch_enabled") or cfg.get("commit_sha")
    ):
        dependencies += ["zurg", "rclone"]
    if key == "decypharr" and cfg.get("mount_type") == "external_rclone":
        dependencies.append("rclone")
    if key == "cli_debrid":
        from utils.cli_source_capabilities import effective_battery_mode

        if effective_battery_mode(cfg) == "standalone":
            dependencies.append("cli_battery")
        if cfg.get("use_phalanx_db", False):
            dependencies.append("phalanx_db")
    if key == "infinidysk" and str(cfg.get("runtime_generation") or "1") == "2":
        if cfg.get("mount_backend") == "fuse":
            dependencies = [item for item in dependencies if item != "rclone"]
    elif cfg.get("postgres_enabled"):
        dependencies.append("postgres")
    if key in ARR_KEYS:
        if cfg.get("use_neutarr"):
            dependencies.append("neutarr")
        if cfg.get("use_profilarr"):
            dependencies.append("profilarr")
    return list(dict.fromkeys(dependencies))


def coverage(actions: list[dict]) -> dict:
    required = [
        a
        for a in actions
        if a.get("required", True) and a.get("coverage_counted", True)
    ]
    count = len(required)
    automatic = sum(a.get("automation") == "automatic" for a in required)
    after_input = sum(a.get("automation") == "after_input" for a in required)
    applied = sum(
        a.get("execution") in {"applied", "already_present"} for a in required
    )
    verified = sum(a.get("verification") == "passed" for a in required)
    return {
        "denominator": count,
        "required": count,
        "automatic": automatic,
        "after_input": after_input,
        "manual": sum(a.get("automation") == "manual" for a in required),
        "unknown": sum(a.get("automation") == "unknown" for a in required),
        "unsupported": sum(a.get("automation") == "unsupported" for a in required),
        "applied": applied,
        "verified": verified,
        "waiting_input": sum(a.get("execution") == "waiting_input" for a in required),
        "failed": sum(a.get("execution") == "failed" for a in required),
        "automatic_percent": round(100 * automatic / count, 1) if count else None,
        "supported_percent": (
            round(100 * (automatic + after_input) / count, 1) if count else None
        ),
        "definition": "Unweighted required setup actions for this plan version; applied settings and verified connections are separate. Credentials/account choices remain human actions.",
    }


def action(
    resource,
    key,
    label,
    *,
    automation="manual",
    reason="",
    required=True,
    prerequisites=(),
    target=None,
    fields=(),
):
    return {
        "id": f"{resource['id']}::{key}",
        "service_key": resource["service_key"],
        "instance_key": resource.get("instance_key"),
        "source": resource["id"],
        "target": target,
        "action": key,
        "label": label,
        "automation": automation,
        "required": required,
        "execution": "waiting_input" if automation == "manual" else "planned",
        "verification": "not_checked",
        "reason": reason,
        "instructions": reason,
        "prerequisites": list(prerequisites),
        "missing_inputs": list(fields),
        "input_keys": list(fields),
        "docs_url": docs_url(resource["service_key"]),
        "ui_url": f"/services/{quote(resource.get('process_name') or resource['service_key'], safe='')}",
        "retryable": automation in {"automatic", "after_input"},
        "checked_at": None,
    }


def _field(resource, name, label, kind, description, **extra):
    return {
        "key": f"{resource['id']}::{name}",
        "resource_id": resource["id"],
        "service_key": resource["service_key"],
        "label": label,
        "type": kind,
        "description": description,
        "required": False,
        **extra,
    }


def validate_values(values: dict, fields: list[dict]) -> dict:
    def bounded(value, depth=0):
        if depth > 12:
            raise ValueError("Arrangement input nesting exceeds the limit.")
        if isinstance(value, dict):
            if len(value) > 500 or any(
                not isinstance(k, str) or len(k) > 300 for k in value
            ):
                raise ValueError("Arrangement input objects exceed their limits.")
            for child in value.values():
                bounded(child, depth + 1)
        elif isinstance(value, list):
            if len(value) > 100:
                raise ValueError("Arrangement input lists exceed their limits.")
            for child in value:
                bounded(child, depth + 1)
        elif isinstance(value, str) and (len(value) > 16384 or "\0" in value):
            raise ValueError(
                "Arrangement text exceeds its limit or contains invalid characters."
            )
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Arrangement numbers must be finite.")

    bounded(values)
    if (
        not isinstance(values, dict)
        or len(json.dumps(values, allow_nan=False)) > 262144
    ):
        raise ValueError(
            "Arrangement inputs must be an object within the input size limit."
        )
    allowed = {field["key"]: field for field in fields}
    if set(values) - set(allowed):
        raise ValueError("An arrangement input is not advertised by this plan.")
    cleaned = copy.deepcopy(values)
    for key, value in cleaned.items():
        field = allowed[key]
        kind = field["type"]
        if kind in {"object", "list"}:
            children = field.get("fields" if kind == "object" else "item_fields") or []
            if not isinstance(children, list) or not all(
                isinstance(child, dict) and isinstance(child.get("key"), str)
                for child in children
            ):
                raise ValueError(
                    "The application advertised an invalid structured input."
                )
            rows = [value] if kind == "object" else value
            if not isinstance(rows, list) or len(rows) > min(
                int(field.get("max_items", 50)), 100
            ):
                raise ValueError("Use a bounded list of advertised objects.")
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Use the advertised object fields.")
                validate_values(row, children)
            continue
        if kind == "boolean" and not isinstance(value, bool):
            raise ValueError("Expected a boolean arrangement choice.")
        if kind == "number" and (
            type(value) is not int
            or not field.get("min", 0) <= value <= field.get("max", 65535)
        ):
            raise ValueError("Choose an integer within the advertised limits.")
        if kind in {"string", "password", "select", "connection"} and type(
            value
        ) not in (str, int):
            raise ValueError("Expected a scalar arrangement value.")
        if kind in {"libraries", "nntp_providers", "multiselect"} and (
            not isinstance(value, list) or len(value) > 50
        ):
            raise ValueError("Expected a bounded list of arrangement choices.")
        if kind == "libraries":
            for library in value:
                if (
                    not isinstance(library, dict)
                    or not str(library.get("name") or "").strip()
                    or len(str(library.get("name"))) > 100
                ):
                    raise ValueError("Each library needs a name.")
                for flag in ("create_if_missing", "scan_on_create"):
                    if flag in library and type(library[flag]) is not bool:
                        raise ValueError(
                            "Library creation and initial-scan choices must be booleans."
                        )
                if library.get("scan_on_create") is True and not field.get(
                    "supports_scan_on_create"
                ):
                    raise ValueError(
                        "This server's setup contract does not advertise an initial library scan."
                    )
                if library.get("media_type") not in {
                    "movie",
                    "show",
                    "music",
                    "book",
                    "podcast",
                    "mixed",
                }:
                    raise ValueError("Choose a supported library content type.")
                paths = library.get("paths")
                if not isinstance(paths, list) or not paths or len(paths) > 20:
                    raise ValueError("Each library needs bounded absolute media paths.")
                for path in paths:
                    if (
                        not isinstance(path, str)
                        or not os.path.isabs(path)
                        or ".." in path.split("/")
                        or path == "/"
                        or path.split("/")[1] in {"config", "etc", "proc", "sys", "dev"}
                    ):
                        raise ValueError(
                            "Use absolute media paths, not system or configuration directories."
                        )
        if kind == "nntp_providers":
            total = 0
            for provider in value:
                if (
                    not isinstance(provider, dict)
                    or not str(provider.get("host") or "").strip()
                ):
                    raise ValueError("Each NNTP provider needs a host.")
                host = str(provider["host"])
                if any(ch in host for ch in "/@?#\\\r\n"):
                    raise ValueError(
                        "Use a provider hostname, not a URL or credential string."
                    )
                if (
                    not isinstance(provider.get("port", 563), int)
                    or not 1 <= provider.get("port", 563) <= 65535
                ):
                    raise ValueError("Invalid NNTP provider port.")
                connections = provider.get("connections", 5)
                if type(connections) is not int or not 1 <= connections <= 200:
                    raise ValueError("Choose 1–200 provider connections.")
                limit = provider.get("account_connection_limit")
                if limit not in (None, "") and (
                    type(limit) is not int or not 1 <= limit <= 10000
                ):
                    raise ValueError("Choose a valid declared NNTP account ceiling.")
                total += connections
            if total > 500:
                raise ValueError("Review the shared NNTP connection budget.")
        if kind == "external_service":
            if not isinstance(value, dict) or set(value) - {
                "service_key",
                "url",
                "api_key",
                "allow_private",
                "verify_tls",
            }:
                raise ValueError(
                    "Use the advertised external server connection fields."
                )
            if value.get("service_key") not in MEDIA_KEYS:
                raise ValueError("Choose the external media server type.")
            for flag in ("allow_private", "verify_tls"):
                if flag in value and not isinstance(value[flag], bool):
                    raise ValueError("External connection choices must be booleans.")
            try:
                parts = urlsplit(str(value.get("url") or ""))
                if (
                    parts.scheme not in {"http", "https"}
                    or not parts.hostname
                    or parts.username
                    or parts.password
                    or parts.query
                    or parts.fragment
                    or any(ch in str(value.get("url")) for ch in "\\\r\n\t ")
                ):
                    raise ValueError
                _ = parts.port
            except ValueError as exc:
                raise ValueError(
                    "Use an external HTTP(S) server URL without embedded credentials."
                ) from exc
        options = field.get("options") or []
        if (
            kind in {"select", "connection"}
            and options
            and str(value) not in {str(option["value"]) for option in options}
        ):
            raise ValueError("Choose an advertised arrangement option.")
        if (
            kind == "multiselect"
            and options
            and any(
                str(choice) not in {str(option["value"]) for option in options}
                for choice in value
            )
        ):
            raise ValueError("Choose advertised target instances.")
    return cleaned


def public_schema(fields):
    """Native schemas must never publish password defaults, including nested rows."""

    def clean(value):
        if isinstance(value, list):
            return [clean(item) for item in value]
        if not isinstance(value, dict):
            return value
        secret = value.get("type") == "password" or value.get("secret") is True
        cleaned = {
            key: clean(item)
            for key, item in value.items()
            if not (secret and key in {"default", "value", "current", "example"})
        }
        if str(cleaned.get("key") or "").rsplit("::", 1)[-1] in {
            "adoption_preview_id",
            "workflow_preview_id",
        }:
            cleaned["internal"] = True
        return cleaned

    return clean(fields)


def refresh_external_resources(record, payload):
    from utils.arrangement_external import external_resources

    external = external_resources(payload)
    previous = {r["id"] for r in record.get("services", []) if r.get("external")}
    selected = {r["id"] for r in external}
    record["services"] = [
        r for r in record.get("services", []) if not r.get("external")
    ] + external
    by_id = {item["id"]: item for item in record["actions"]}
    for item in record["actions"]:
        if item.get("source") in previous - selected:
            item.update(
                required=False,
                execution="declined",
                verification="stale",
                evidence=None,
                retryable=False,
                next_retry_at=None,
                reason="This external server was removed from the accepted arrangement.",
            )
    for resource in external:
        for name, label, required in (
            ("external_ready", "Verify the external server and authorization", True),
            ("indexers", "Configure selected indexers", False),
        ):
            identifier = f"{resource['id']}::{name}"
            if identifier not in by_id:
                item = action(
                    resource,
                    name,
                    label,
                    automation="after_input",
                    required=required,
                    reason="Only explicitly authorized existing servers are configured; no installation or restart is performed.",
                )
                item["previewable"] = True
                record["actions"].append(item)
            elif resource["id"] not in previous:
                by_id[identifier].update(
                    required=required,
                    execution="planned",
                    verification="stale",
                    evidence=None,
                )
        _extend_resource(
            record["actions"], [], resource, {}, payload, {}, planning=False
        )


def _extend_resource(actions, fields, resource, config, payload, cfg, *, planning=True):
    """Keep each recommended native setup capability in the retained inventory."""
    from utils.arrangement_preferences import schema_fields as preferences_fields
    from utils.arrangement_streams import schema_fields as streams_fields
    from utils.arrangement_pgadmin import schema_fields as pgadmin_fields
    from utils.arrangement_workflow import schema_fields as workflow_fields
    from utils.arrangement_discovery import schema_fields as discovery_fields
    from utils.arrangement_mediastorm import schema_fields as mediastorm_fields
    from utils.arrangement_arr_providers import schema_fields as arr_provider_fields
    from utils.arrangement_altmount_account import schema_fields as altmount_fields
    from utils.arrangement_proxy_routes import schema_fields as proxy_route_fields
    from utils.arrangement_riven import schema_fields as riven_fields

    key, rid = resource["service_key"], resource["id"]
    if planning:
        for schema in (
            preferences_fields,
            streams_fields,
            pgadmin_fields,
            workflow_fields,
            discovery_fields,
            mediastorm_fields,
            arr_provider_fields,
            altmount_fields,
            proxy_route_fields,
            riven_fields,
        ):
            fields.extend(schema(resource, config, payload))
    specs = []
    if key == "riven_backend":
        specs.extend(
            (
                (
                    "backend_connection",
                    "Verify backend settings and API credentials",
                    True,
                ),
                ("media_connection", "Connect the selected media server", False),
            )
        )
    if key == "traefik_proxy_admin":
        specs.extend(
            (
                ("account", "Authorize the existing proxy administrator", True),
                (
                    "proxy_routes",
                    "Configure selected domains and protected routes",
                    False,
                ),
            )
        )
    if key == "altmount":
        specs.extend(
            (
                ("account", "Authorize or create the first administrator", True),
                ("webdav", "Secure and verify WebDAV access", True),
            )
        )
        actions[:] = [
            item
            for item in actions
            if not (item["source"] == rid and item["action"] == "import")
        ]
    if resource.get("external"):
        chosen = ((payload.get("arrangement") or {}).get("values") or {}).get(
            rid + "::providers"
        ) or []
        for previous in actions:
            if (
                previous.get("source") == rid
                and str(previous.get("action") or "").startswith("provider.")
                and previous.get("target") not in chosen
            ):
                retired = (previous.get("evidence") or {}).get(
                    "retired"
                ) is True and previous.get("verification") == "passed"
                previous.update(
                    selected=False,
                    required=True,
                    retirement_required=True,
                    retirement_pending=not retired,
                    retryable=not retired,
                )
                if not retired and previous.get("execution") != "planned":
                    previous.update(
                        execution="planned",
                        verification="stale",
                        evidence=None,
                        reason="Retire only this removed owned provider client. Existing roots and remote-path mappings are preserved.",
                    )
        for provider in sorted(set(chosen) & PROVIDER_KEYS):
            specs.append(
                (
                    "provider." + provider,
                    "Connect the external Arr to " + SERVICE_NAMES[provider],
                    True,
                )
            )
    if key in ARR_KEYS:
        specs.append(
            (
                "workflow_test",
                "Test a complete download, import and playback workflow",
                False,
            )
        )
    if key == "lidarr":
        specs.append(
            ("arr_preferences", "Choose music root and quality/metadata defaults", True)
        )
    if key == "bazarr":
        specs.append(
            (
                "subtitle_providers",
                "Configure selected subtitle provider credentials",
                False,
            )
        )
    if key == "aiostreams":
        specs.extend(
            (
                ("backend_presets", "Configure the selected addon backends", True),
                ("public_origin", "Check the public addon HTTPS origin", True),
            )
        )
    if key == "pgadmin":
        specs.append(
            ("postgres_connection", "Register the managed PostgreSQL connection", True)
        )
    if key == "mediastorm":
        specs.extend(
            (
                (
                    "account",
                    "Authorize the administrator and secure initial access",
                    True,
                ),
                ("sources", "Configure selected media sources and engines", True),
                ("metadata", "Configure selected metadata credentials", False),
            )
        )
        actions[:] = [
            a
            for a in actions
            if not (a["source"] == rid and a["action"] == "providers")
        ]
    if key == "cli_debrid":
        specs.append(
            (
                "companion_connection",
                "Configure and verify Battery and PhalanxDB bindings",
                True,
            )
        )
    if key == "zurg":
        specs.extend(
            (
                (
                    "discovery",
                    "Check the installed Zurg version and provider contract",
                    False,
                ),
                ("library_hook", "Configure selected Plex library update hooks", False),
            )
        )
        actions[:] = [
            a for a in actions if not (a["source"] == rid and a["action"] == "scan")
        ]
    if key == "zilean":
        specs.append(
            ("ingestion", "Check API readiness and initial ingestion progress", True)
        )
        actions[:] = [
            a
            for a in actions
            if not (a["source"] == rid and a["action"] == "population")
        ]
    if key in {"cloudflared", "traefik", "traefik_proxy_admin"}:
        specs.append(
            (
                "public_route",
                "Verify the selected public application route",
                key == "cloudflared",
            )
        )
    for name, label, required in specs:
        existing = next(
            (a for a in actions if a["source"] == rid and a["action"] == name), None
        )
        if existing is None:
            existing = action(
                resource,
                name,
                label,
                automation="after_input",
                required=required,
                prerequisites=[
                    (
                        f"{rid}::external_ready"
                        if resource.get("external")
                        else f"{rid}::runtime"
                    )
                ],
                reason="Apply only explicitly selected native settings. Preview and authenticated readback preserve existing user choices.",
            )
            actions.append(existing)
        existing.update(automation="after_input", previewable=True)
        if name.startswith("provider."):
            was_removed = (
                existing.get("retirement_required") or existing.get("selected") is False
            )
            existing.update(
                target=name.removeprefix("provider."),
                selected=True,
                retirement_required=False,
                retirement_pending=False,
            )
            if was_removed:
                existing.update(
                    execution="planned", verification="stale", evidence=None
                )
        if name in {"workflow_test", "proxy_routes"}:
            existing.update(
                explicit_only=True,
                reason=(
                    "Optional real content operations require a fresh preview and explicit authorization. Ordinary setup, retry and recheck never enqueue a test."
                    if name == "workflow_test"
                    else "Domain and route changes require a fresh preview and explicit Apply; public exposure and protection choices remain explicit."
                ),
            )


def build_plan(
    payload: dict, current: dict, defaults: dict, *, plan_id: str, discover_fields=None
) -> dict:
    proposed, resources = project_configuration(payload, current, defaults)
    selected_ids = {r["id"] for r in resources}
    selected_keys = {r["service_key"] for r in resources}
    queue = list(resources)
    while queue:
        parent = queue.pop(0)
        if parent["service_key"] == "infinidysk":
            from utils.arrangement_infinidysk import transition

            if transition(
                current.get("infinidysk") or {}, resource_config(proposed, parent["id"])
            ):
                # The native wizard owns creation/activation of its mount.
                # Describe dependencies in runtime_topology without creating
                # ordinary legacy setup resources before that handoff.
                continue
        for key in _dependencies(
            parent["service_key"], resource_config(proposed, parent["id"])
        ):
            if key not in defaults or (
                key in selected_keys and key not in {"rclone", "zurg"}
            ):
                continue
            block = proposed.setdefault(key, copy.deepcopy(defaults[key]))
            instance = (
                next(iter(block["instances"]), None)
                if isinstance(block.get("instances"), dict)
                else None
            )
            if key in {"rclone", "zurg"}:
                display = SERVICE_NAMES.get(
                    parent["service_key"], parent["service_key"]
                )
                if (
                    parent["service_key"] == "infinidysk"
                    and "nzbdav"
                    in str(
                        resource_config(proposed, parent["id"]).get("process_name", "")
                    ).lower()
                ):
                    display = "NzbDAV"
                instance = display
                instances = block.setdefault("instances", {})
                if key == "rclone" and parent["service_key"] == "infinidysk":
                    from utils.infinidysk_v2_topology import associated_rclone

                    owners = [
                        name
                        for name, item in instances.items()
                        if isinstance(item, dict)
                        and item.get("enabled")
                        and associated_rclone(item)
                    ]
                    if len(owners) == 1:
                        instance = owners[0]
                    elif (
                        str(
                            resource_config(proposed, parent["id"]).get(
                                "runtime_generation"
                            )
                            or "1"
                        )
                        == "2"
                    ):
                        raise ValueError(
                            "Preserve exactly one existing managed rclone owner for InfiniDysk 2."
                        )
                if instance not in instances:
                    candidate = copy.deepcopy(
                        next(iter(defaults[key]["instances"].values()))
                    )
                    candidate.update(
                        core_service=parent["service_key"],
                        process_name=f"{'Rclone' if key == 'rclone' else 'Zurg'} w/ {display}",
                    )
                    instances[instance] = candidate
            rid = resource_id(key, instance)
            if rid in selected_ids:
                existing_resource = next(
                    item for item in resources if item["id"] == rid
                )
                if existing_resource.get("selection_kind") == "required" and parent[
                    "id"
                ] not in existing_resource.setdefault("required_by", []):
                    existing_resource["required_by"].append(parent["id"])
                continue
            dependency = {
                "id": rid,
                "service_key": key,
                "instance_key": instance,
                "name": SERVICE_NAMES.get(key, key),
                "selection_kind": "required",
                "required_by": [parent["id"]],
                "roles": service_roles(key),
                "process_name": resource_config(proposed, rid).get("process_name", ""),
                "already_enabled": bool(resource_config(current, rid).get("enabled")),
            }
            resources.append(dependency)
            selected_keys.add(key)
            selected_ids.add(rid)
            queue.append(dependency)
            if len(resources) > MAX_RESOURCES:
                raise ValueError("The selected arrangement has too many resources.")
    from utils.arrangement_external import external_resources, global_fields

    resources.extend(external_resources(payload))
    if len(resources) > MAX_RESOURCES:
        raise ValueError("The arrangement has too many managed and external resources.")
    actions = []
    fields = global_fields(proposed, payload)
    registry = action(
        {"id": "setup", "service_key": "setup", "name": "External connections"},
        "external_registry",
        "Manage external Arr connections",
        automation="after_input",
        required=bool(external_resources(payload)),
        fields=["setup::external_arrs"],
        reason="Keep external credentials protected and choose write authorization separately from discovery.",
    )
    registry["ui_url"] = ""
    registry["docs_url"] = "https://dumbarr.com/features/setup-checklist/"
    registry["previewable"] = True
    arrs = [r for r in resources if r["service_key"] in ARR_KEYS]
    media = [r for r in resources if r["service_key"] in MEDIA_KEYS]
    media_options = [{"value": r["id"], "label": r["name"]} for r in media]
    for resource in resources:
        key, rid = resource["service_key"], resource["id"]
        cfg = resource_config(proposed, rid)
        if resource.get("external"):
            actions.append(
                action(
                    resource,
                    "external_ready",
                    "Verify the external server and authorization",
                    automation="after_input",
                    fields=["setup::external_arrs"],
                    reason="Read-only authenticated readiness; external servers are never installed or restarted.",
                )
            )
            indexers = action(
                resource,
                "indexers",
                "Configure selected indexers",
                automation="after_input",
                required=False,
                reason="Configure only explicitly selected indexers after preview and authorization.",
            )
            indexers["previewable"] = True
            actions.append(indexers)
            _extend_resource(actions, fields, resource, proposed, payload, cfg)
            if discover_fields:
                fields.extend(discover_fields(resource, proposed, payload))
            continue
        if key == "infinidysk" and str(cfg.get("runtime_generation") or "1") == "2":
            from utils.arrangement_infinidysk import (
                choice_fields,
                transition,
                NATIVE_SETTINGS_REASON,
            )

            fields.extend(choice_fields(resource, current.get("infinidysk") or {}, cfg))
            handoff = transition(current.get("infinidysk") or {}, cfg)
            if handoff:
                install = action(
                    resource,
                    "native_transition",
                    "Open the guarded InfiniDysk 2 wizard",
                    reason="Complete the guarded native installation or migration, including its separate acknowledgements, then create a fresh setup plan.",
                )
                install.update(kind="native_transition", native_transition=handoff)
            else:
                install = action(
                    resource,
                    "runtime",
                    "Start the existing InfiniDysk 2 runtime",
                    automation="automatic",
                )
            actions.append(install)
            actions.append(
                action(
                    resource,
                    "native_settings",
                    "Complete and verify InfiniDysk 2 native settings",
                    reason=NATIVE_SETTINGS_REASON,
                    prerequisites=[install["id"]],
                )
            )
            if discover_fields:
                fields.extend(discover_fields(resource, proposed, payload))
            continue
        if key == "infinidysk":
            from utils.arrangement_infinidysk import choice_fields

            fields.extend(choice_fields(resource, current.get("infinidysk") or {}, cfg))
        install = action(
            resource,
            "runtime",
            f"Install and start {resource['name']}",
            automation="automatic",
            reason="Application readiness is checked separately from its connections and initial account setup.",
        )
        actions.append(install)
        for task, label, instructions, required in MANUAL_TASKS.get(key, ()):
            automation = (
                "after_input"
                if task == "account" and key in {"jellyfin", "audiobookshelf"}
                else "manual"
            )
            required_fields = (
                [f"{rid}::admin_username", f"{rid}::admin_password"]
                if automation == "after_input"
                else []
            )
            actions.append(
                action(
                    resource,
                    task,
                    label,
                    automation=automation,
                    reason=instructions,
                    required=required,
                    prerequisites=[install["id"]],
                    fields=required_fields,
                )
            )
        if key in ARR_KEYS:
            actions.append(
                action(
                    resource,
                    "policy",
                    "Choose library and quality policy",
                    reason="Select an actual quality profile and the desired import/language/upgrade policy. Naming an instance 1080p or 4K does not configure its profile.",
                    prerequisites=[install["id"]],
                )
            )
            providers = cfg.get("core_services", cfg.get("core_service", ""))
            if isinstance(providers, str):
                providers = [p.strip() for p in providers.split(",") if p.strip()]
            for provider in providers or []:
                if provider in PROVIDER_KEYS:
                    actions.append(
                        action(
                            resource,
                            f"provider.{provider}",
                            f"Connect {resource['name']} to {SERVICE_NAMES[provider]}",
                            automation="automatic",
                            target=provider,
                            reason="Configure the selected client and category. Decypharr/InfiniDysk use per-instance symlink roots and Arr import modes 777 for folders and 666 for files where supported; review these managed settings before applying to an existing app. AltMount requires an explicit import/root choice. Readback and connection tests do not download content.",
                            prerequisites=[install["id"]],
                        )
                    )
        if key in ARR_KEYS | {"prowlarr"}:
            prior = next(
                (
                    a
                    for a in actions
                    if a["source"] == rid and a["action"] == "indexers"
                ),
                None,
            )
            item = action(
                resource,
                "indexers",
                "Configure selected indexers",
                automation="after_input",
                required=False,
                prerequisites=[install["id"]],
                reason="Use this application's advertised definitions and explicit credentials; new indexers stay disabled until chosen.",
            )
            item["previewable"] = True
            if prior is None:
                actions.append(item)
            else:
                prior.update(
                    automation="after_input",
                    previewable=True,
                    reason=item["reason"],
                    prerequisites=item["prerequisites"],
                )
        if key == "profilarr":
            item = next(
                (
                    item
                    for item in actions
                    if item["source"] == rid and item["action"] == "profiles"
                ),
                None,
            )
            if item is not None:
                item.update(
                    automation="after_input",
                    previewable=True,
                    reason="Choose a database and exact profiles, preview managed changes, then explicitly authorize synchronization.",
                )
        if cfg.get("process_name"):
            from utils.arrangement_consumer import schema_fields as consumer_fields

            fields.extend(consumer_fields(resource, proposed, payload))
            probe = action(
                resource,
                "consumer_paths",
                "Verify paths as the running service",
                automation="after_input",
                required=False,
                fields=[f"{rid}::consumer_paths"],
                reason="Optional bounded reads in the running service's actual namespace and identity. No scans or writes.",
            )
            probe.update(explicit_only=True, previewable=True)
            actions.append(probe)
        if key in {"infinidysk", "altmount", "decypharr"}:
            fields.append(
                _field(
                    resource,
                    "nntp_source",
                    "Usenet account source",
                    "select",
                    "Enter credentials once and allocate separate connections to each client.",
                    default="own",
                    options=[
                        {"value": "own", "label": "Enter accounts for this service"},
                        *[
                            {
                                "value": r["id"],
                                "label": f"Reuse accounts from {r['name']}",
                            }
                            for r in resources
                            if r["service_key"] in PROVIDER_KEYS and r["id"] != rid
                        ],
                    ],
                )
            )
            fields.append(
                _field(
                    resource,
                    "nntp",
                    "Usenet provider accounts",
                    "nntp_providers",
                    "Use only accounts you own. Divide the account connection limit across every selected client; provider setup does not add indexer accounts.",
                    visible_when={
                        "key": f"{rid}::nntp_source",
                        "equals": "own",
                        "default": "own",
                    },
                )
            )
            fields.append(
                _field(
                    resource,
                    "nntp_connections",
                    "Connections per shared account",
                    "number",
                    "Allocation for this application, counted together with the source and other selected clients.",
                    default=5,
                    min=1,
                    max=200,
                    visible_when={
                        "key": f"{rid}::nntp_source",
                        "not_equals": "own",
                        "default": "own",
                    },
                )
            )
            intent = (payload.get("arrangement") or {}).get("intent") or {}
            actions.append(
                action(
                    resource,
                    "nntp",
                    "Configure Usenet provider accounts",
                    automation="after_input",
                    reason="Supply provider accounts for Usenet. Decypharr can need a controlled application restart after provider changes; no downloads run as a connection test.",
                    required=key != "decypharr"
                    or intent.get("stack") in {"usenet", "both"},
                    fields=[f"{rid}::nntp"],
                )
            )
            if key == "altmount":
                actions.append(
                    action(
                        resource,
                        "import_layout",
                        "Configure AltMount import layout",
                        automation="after_input",
                        fields=[f"{rid}::import_strategy", f"{rid}::import_dir"],
                        reason="Choose import strategy and directory explicitly; this action does not move content or create a missing external mount.",
                    )
                )
        if key in MEDIA_KEYS:
            library_types = (
                ("book", "podcast")
                if key == "audiobookshelf"
                else (
                    ("movie", "show", "music")
                    if key == "plex"
                    else ("movie", "show", "music", "book", "mixed")
                )
            )
            fields.append(
                _field(
                    resource,
                    "libraries",
                    "Media libraries",
                    "libraries",
                    "Select names, content types and paths. Existing matching libraries are reused. New library creation is explicit and never removes existing paths.",
                    media_types=[
                        {
                            "value": kind,
                            "label": {
                                "movie": "Movies",
                                "show": "TV shows",
                                "music": "Music",
                                "book": "Audiobooks",
                                "podcast": "Podcasts",
                                "mixed": "Mixed media",
                            }[kind],
                        }
                        for kind in library_types
                    ],
                    default_media_type=library_types[0],
                    supports_language=key != "audiobookshelf",
                    supports_scan_on_create=key in {"jellyfin", "emby"},
                )
            )
            actions.append(
                action(
                    resource,
                    "libraries",
                    "Create or reuse selected media libraries",
                    automation="after_input",
                    fields=[f"{rid}::libraries"],
                    reason="Choose the actual libraries/paths and authorize the media server. A ready process does not establish a usable library.",
                )
            )
            if key in {"jellyfin", "audiobookshelf"}:
                fields.append(
                    _field(
                        resource,
                        "admin_username",
                        "First administrator username",
                        "string",
                        "Used only for an explicitly authorized, demonstrably fresh supported server.",
                    )
                )
                fields.append(
                    _field(
                        resource,
                        "admin_password",
                        "First administrator password",
                        "password",
                        "Never used to reinitialize an existing server; retained only in protected setup inputs when needed.",
                    )
                )
            fields.append(
                _field(
                    resource,
                    "api_key",
                    "Dedicated integration API key",
                    "password",
                    "Use a dedicated key where the selected application supports it.",
                )
            )
            if key == "plex":
                fields.append(
                    _field(
                        resource,
                        "claim_token",
                        "Fresh Plex claim token",
                        "password",
                        "Obtain a short-lived token at https://plex.tv/claim/ after Plex is ready. Retry only account authorization; a claimed server is never initialized again.",
                    )
                )
            if key in {"jellyfin", "emby"}:
                fields.append(
                    _field(
                        resource,
                        "save_protection_key",
                        "Save this API key for Library Protection",
                        "boolean",
                        "Save the verified dedicated key in the existing DUMB protection policy. Existing enablement, mounts and outage settings are preserved.",
                    )
                )
        if key in {"seerr", "pulsarr", "tautulli", "maintainerr", "aiostreams"}:
            fields.append(
                _field(
                    resource,
                    "media_server",
                    "Media server connection",
                    "connection",
                    "Choose the intended server explicitly; accounts and permissions stay separate.",
                    options=[
                        {"value": "", "label": "Choose later"},
                        *media_options,
                        {"value": "external", "label": "Connect an external server"},
                        {"value": "none", "label": "No media server"},
                    ],
                )
            )
            fields.append(
                _field(
                    resource,
                    "external_media",
                    "External media server",
                    "external_service",
                    "Use a server you are authorized to configure. Credentials remain on the backend.",
                    visible_when={"key": f"{rid}::media_server", "equals": "external"},
                )
            )
            if key in {"seerr", "pulsarr", "maintainerr"}:
                actions.append(
                    action(
                        resource,
                        "media_connection",
                        "Connect the selected media server",
                        automation="after_input",
                        fields=[f"{rid}::media_server"],
                        reason="Authorize the application account first. Apply only the chosen connection; preserve libraries, watchlists, rules and existing user policy.",
                    )
                )
        if key == "maintainerr":
            for task, label, fields_for_task in (
                (
                    "seerr_connection",
                    "Connect the selected Seerr instance",
                    [f"{rid}::seerr_instance"],
                ),
                (
                    "tautulli_connection",
                    "Connect Tautulli monitoring",
                    [f"{rid}::connect_tautulli"],
                ),
            ):
                actions.append(
                    action(
                        resource,
                        task,
                        label,
                        automation="after_input",
                        required=False,
                        fields=fields_for_task,
                        reason="An optional, explicitly selected companion connection. Collection and retention rules remain unchanged.",
                    )
                )
        if key in {
            "prowlarr",
            "seerr",
            "pulsarr",
            "bazarr",
            "maintainerr",
            "neutarr",
            "profilarr",
        }:
            options = [
                {"value": r["id"], "label": r["name"]}
                for r in arrs
                if key not in {"bazarr", "profilarr", "seerr", "pulsarr"}
                or r["service_key"] in {"radarr", "sonarr"}
            ]
            fields.append(
                _field(
                    resource,
                    "arr_instances",
                    "Connect these Arr instances",
                    "multiselect",
                    "Select exact instances. An empty explicit selection means none, not all instances.",
                    options=options,
                )
            )
            actions.append(
                action(
                    resource,
                    "arr_connections",
                    "Configure and verify selected Arr connections",
                    automation="after_input",
                    reason="Choose target instances, authorize this application, and select profile/root/default routing where applicable.",
                    fields=[f"{rid}::arr_instances"],
                )
            )
        if key == "cli_debrid":
            from utils.cli_source_capabilities import (
                cli_source_capability,
                effective_battery_mode,
            )

            source_capability = cli_source_capability(cfg)
            fields.append(
                _field(
                    resource,
                    "battery_mode",
                    "CLI Battery topology",
                    "select",
                    "Choose the layout supported by the selected source. "
                    + source_capability["evidence"]
                    + ". Standalone and integrated Battery are distinct; do not enable both by accident.",
                    required=effective_battery_mode(cfg) is None,
                    default=effective_battery_mode(cfg),
                    options=[
                        {"value": "standalone", "label": "Standalone CLI Battery"},
                        {
                            "value": "embedded",
                            "label": "Battery integrated in CLI Debrid",
                        },
                    ],
                )
            )
            fields.append(
                _field(
                    resource,
                    "use_phalanx_db",
                    "Use the PhalanxDB companion",
                    "boolean",
                    "Optional unless your audited selected CLI source requires it.",
                )
            )
        if key == "pulsarr":
            fields.append(
                _field(
                    resource,
                    "tmdb_token",
                    "TMDB API Read Access Token",
                    "password",
                    "Metadata-feature credential for versions exposing tmdbApiKey; not a startup API key.",
                )
            )
        if key in {"riven_frontend", "tautulli"}:
            actions.append(
                action(
                    resource,
                    "backend_connection",
                    "Verify backend connection",
                    automation="after_input",
                    reason="Wait for generated configuration and authorized credentials, then read back and test the connection.",
                )
            )
        if discover_fields:
            fields.extend(discover_fields(resource, proposed, payload))
        _extend_resource(actions, fields, resource, proposed, payload, cfg)
    actions.append(registry)
    fields = public_schema(fields)
    fields_by_key = {field["key"]: field for field in fields}
    fields = list(fields_by_key.values())
    values = validate_values(
        (payload.get("arrangement") or {}).get("values") or {}, fields
    )
    from utils.arrangement_inputs import resolve_provider_inputs

    effective_values = resolve_provider_inputs(payload)["arrangement"]["values"]
    for item in actions:
        item["input_fields"] = [
            f["key"] for f in fields if f.get("resource_id") == item["source"]
        ]
        item["missing_inputs"] = [
            k for k in item["missing_inputs"] if not effective_values.get(k)
        ]
        if item["automation"] == "after_input" and item["missing_inputs"]:
            item["execution"] = "waiting_input"
        if item.get("action") in {
            "arr_connection",
            "arr_connections",
            "indexers",
            "profiles",
            "consumer_paths",
            "backend_presets",
            "public_origin",
            "postgres_connection",
        }:
            item["previewable"] = True
        versioned = (
            item["action"].startswith("provider.")
            or item["action"]
            in {
                "nntp",
                "import_layout",
                "libraries",
                "arr_connections",
                "backend_connection",
                "media_connection",
                "seerr_connection",
                "tautulli_connection",
                "indexers",
                "profiles",
                "arr_preferences",
                "subtitle_providers",
                "backend_presets",
                "sources",
                "webdav",
                "proxy_routes",
            }
            or (
                item["action"] == "account"
                and item["service_key"]
                in {
                    "jellyfin",
                    "audiobookshelf",
                    "altmount",
                    "mediastorm",
                    "traefik_proxy_admin",
                }
            )
        )
        if versioned and item["automation"] in {"automatic", "after_input"}:
            item["declared_automation"] = item["automation"]
            item["automation"] = "unknown"
            item["capability_reason"] = (
                "The selected application's API contract must be recognized before this action counts as supported automation."
            )
    if len({item["id"] for item in actions}) != len(actions):
        raise ValueError("Setup action identities must be unique.")
    if len(actions) > MAX_ACTIONS:
        raise ValueError("This arrangement has too many setup actions.")
    from utils.arrangement_requirements import refresh_requirements

    refresh_requirements({"actions": actions}, payload)
    fingerprint_payload = {
        "resources": [
            {"id": r["id"], "selection_kind": r["selection_kind"]} for r in resources
        ],
        "version": PLAN_VERSION,
        "choices": public_values(values, fields),
        "action_ids": [a["id"] for a in actions],
    }
    record = {
        "plan_id": plan_id,
        "version": PLAN_VERSION,
        "services": resources,
        "actions": actions,
        "coverage": coverage(actions),
        "checks": [
            {
                "key": "plan",
                "label": "Arrangement plan validated",
                "ok": True,
                "critical": True,
                "detail": "No application settings have been applied.",
            }
        ],
        "arrangement_schema": {"version": 1, "fields": list(fields_by_key.values())},
        "features": {
            "action_previews": True,
            "structured_inputs": True,
            "external_arrs": True,
            "consumer_checks": True,
        },
        "values": public_values(values, fields),
        "fingerprint": hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True).encode()
        ).hexdigest(),
    }
    from utils.arrangement_link_actions import refresh_link_actions

    refresh_link_actions(record, payload, proposed)
    from utils.arrangement_infinidysk import adapt_plan

    adapt_plan(record, current, proposed, payload)
    from utils.arrangement_action_fields import bind_action_fields

    bind_action_fields(record)
    record["coverage"] = coverage(record["actions"])
    fingerprint_payload["action_ids"] = [item["id"] for item in record["actions"]]
    record["fingerprint"] = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True).encode()
    ).hexdigest()
    return record
