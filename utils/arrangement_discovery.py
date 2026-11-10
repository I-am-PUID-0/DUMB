"""Version-aware discovery, scoped companion settings and public route checks.

Official source audited 2026-10-14: CLI main db84f5e9/dev c971c603;
Zilean 8bfd20d4 (healthchecks/ping and DmmScraping logging); Zurg public
40d4090 (v1.0.0, config.yml/on_library_update and scripts/plex_update.sh).
No provider content, metadata search, DNS modification or cloud account API is
called. Public route probes never carry application credentials.
"""

from __future__ import annotations

import copy
from io import StringIO
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
from urllib.parse import urlsplit

import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException

from utils.arrangement_http import ApiClient, ApiError
from utils.arrangement_media import _managed_plex, _resource, _values
from utils.cli_source_capabilities import cli_source_capability, effective_battery_mode
from utils.integration_ownership import fingerprint, read_ownership, write_ownership
from utils.private_files import atomic_write_private_text
from utils.zurg_config import _load_document, ZurgConfigError


class DiscoveryInput(ValueError):
    pass


def _state(
    execution, reason, verification="not_checked", *, retryable=False, evidence=None
):
    return {
        "execution": execution,
        "reason": reason,
        "verification": verification,
        "retryable": retryable,
        "evidence": evidence or {},
    }


def _field(resource, key, label, kind="string", description="", **extra):
    return {
        "key": resource["id"] + "::" + key,
        "resource_id": resource["id"],
        "service_key": resource["service_key"],
        "label": label,
        "type": kind,
        "required": False,
        "description": description,
        **extra,
    }


def schema_fields(resource, config, payload=None):
    del payload
    service = resource.get("service_key")
    fields = []
    if service in {"cloudflared", "traefik", "traefik_proxy_admin"}:
        targets = []
        for key in (
            "aiostreams",
            "plex",
            "jellyfin",
            "emby",
            "audiobookshelf",
            "seerr",
            "pulsarr",
            "maintainerr",
            "traefik_proxy_admin",
            "dumb_frontend",
        ):
            cfg = config.get(key) or {}
            if cfg.get("enabled"):
                targets.append({"value": key, "label": key})
            for name, instance in (cfg.get("instances") or {}).items():
                if instance.get("enabled"):
                    targets.append(
                        {"value": f"{key}.{name}", "label": f"{key} — {name}"}
                    )
        fields += [
            _field(
                resource,
                "public_url",
                "Public application HTTPS URL",
                description="Final public route, including any application base path. This checks DNS/TLS/HTTP; it does not create DNS or change Cloudflare/Traefik policies.",
            ),
            _field(
                resource,
                "public_target",
                "Managed application behind this route",
                "select",
                "Select the intended managed endpoint. No application credentials are sent to the public URL.",
                options=targets,
            ),
            _field(
                resource,
                "public_allow_private",
                "Allow private-network route check",
                "boolean",
                "Explicitly authorize a private/LAN destination for this HTTPS probe.",
            ),
            _field(
                resource,
                "public_verify_tls",
                "Verify public TLS certificate",
                "boolean",
                "Defaults to true. Disable only for an explicitly trusted private endpoint with a custom certificate.",
            ),
        ]
    if service == "cli_debrid":
        fields += [
            _field(
                resource,
                "adopt_companion_settings",
                "Replace differing companion bindings",
                "boolean",
                "Explicitly replace only Battery URL/Phalanx enablement when the native configuration already contains different values. Other settings are preserved.",
            ),
            _field(
                resource,
                "restart_after_config",
                "Restart CLI Debrid to apply bindings",
                "boolean",
                "Allows a controlled CLI Debrid-only stop/configuration/restart. Existing transfers and other services are not restarted by this action.",
            ),
        ]
    if service == "zurg":
        options = [{"value": "none", "label": "Do not configure a scan hook"}]
        if (config.get("plex") or {}).get("enabled"):
            options.append({"value": "plex", "label": "Managed Plex"})
        fields += [
            _field(
                resource,
                "enable_library_hook",
                "Enable selected Plex library update hook",
                "boolean",
                "Opt in to partial library refreshes when Zurg reports changes. No library scan is run during setup.",
            ),
            _field(
                resource,
                "media_server",
                "Plex server for library updates",
                "select",
                "Use the explicitly selected managed Plex identity.",
                options=options,
            ),
            _field(
                resource,
                "scan_library_ids",
                "Plex library IDs to refresh",
                description="Comma-separated numeric library IDs. Setup validates these against the authenticated selected Plex server and only refreshes paths under their existing roots.",
            ),
            _field(
                resource,
                "scan_mount_path",
                "Mount path visible to Plex",
                description="Absolute path containing this Zurg library in Plex's namespace. It must overlap every explicitly selected library root.",
            ),
            _field(
                resource,
                "adopt_library_hook",
                "Replace an existing custom update hook",
                "boolean",
                "Explicitly replace the current on_library_update command; otherwise user-owned hooks are preserved.",
            ),
            _field(
                resource,
                "restart_after_config",
                "Restart this Zurg instance to enable the hook",
                "boolean",
                "Allows a controlled stop/configuration/restart of this Zurg instance only. Mount access can be interrupted while it restarts.",
            ),
        ]
    return fields


def native_config_path(action, config):
    cfg = _resource(config, action["source"])
    if action.get("service_key") == "cli_debrid":
        return (
            Path((cfg.get("env") or {}).get("USER_CONFIG") or "/user/config")
            / "config.json"
        )
    if action.get("service_key") == "zurg":
        return Path(
            cfg.get("config_file")
            or str(Path(cfg.get("config_dir") or "/zurg") / "config.yml")
        )
    return None


def _read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise DiscoveryInput(
            "Wait for a regular, bounded native configuration file; setup does not follow configuration symlinks."
        )
    return path.read_text()


def _get(client, path, **kwargs):
    response = client.request("GET", path, **kwargs)
    response.raise_for_status()
    return response


def sync_cli_phalanx_environment(cfg, phalanx_cfg, *, adopt=False, apply=True):
    """Synchronize only explicitly selected managed Phalanx runtime bindings."""
    if cfg.get("use_phalanx_db") is not True:
        return {"changed": False, "adoption_required": False}
    port = str(int(phalanx_cfg.get("port") or 8888))
    env = dict(cfg.get("env") or {})
    desired = {"CLI_DEBRID_PHALANX_HOST": "127.0.0.1", "CLI_DEBRID_PHALANX_PORT": port}
    ledger = (
        Path(cfg.get("config_dir") or "/cli_debrid") / ".dumb-phalanx-environment.json"
    )
    records = read_ownership(ledger)
    origin = env.get("CLI_DEBRID_PHALANX_HOST") or env.get("CLI_DEBRID_PHALANX_URL")
    old_port = env.get("CLI_DEBRID_PHALANX_PORT")
    owned = records.get("binding") == fingerprint(
        {key: env.get(key) for key in desired}
    )
    conventional = origin in (
        None,
        "",
        "localhost",
        "127.0.0.1",
        "http://localhost",
        "http://127.0.0.1",
    ) and old_port in (None, "", "8888", port)
    if not owned and not conventional and not adopt:
        return {"changed": False, "adoption_required": True}
    changed = any(env.get(key) != value for key, value in desired.items())
    if changed and apply:
        env.update(desired)
        write_ownership(ledger, {"binding": fingerprint(desired)})
        cfg["env"] = env
    return {
        "changed": changed,
        "adoption_required": False,
        "environment": {**env, **desired},
    }


def _cli(action, config, payload, apply, factory, manager=None):
    source, values = action["source"], _values(payload)
    cfg = _resource(config, source)
    mode, capability = effective_battery_mode(cfg), cli_source_capability(cfg)
    if mode not in {"standalone", "embedded"} or (
        capability["battery_mode"] and capability["battery_mode"] != mode
    ):
        raise DiscoveryInput(
            "Choose the Battery layout supported by the selected immutable CLI source before configuring companions."
        )
    path = native_config_path(action, config)
    document = json.loads(_read(path))
    if not isinstance(document, dict):
        raise DiscoveryInput("The CLI native configuration must be one JSON object.")
    updates, evidence = {}, {
        "battery_mode": mode,
        "phalanx_requested": cfg.get("use_phalanx_db") is True,
    }
    if mode == "standalone":
        battery = config.get("cli_battery") or {}
        if not battery.get("enabled"):
            raise DiscoveryInput(
                "Enable the explicitly selected standalone Battery companion first."
            )
        port = int(battery.get("port") or 5001)
        target = f"http://127.0.0.1:{port}"
        stats = _get(factory(target, managed=True, timeout=8), "/api/stats").json()
        if not isinstance(stats, dict) or not all(
            isinstance(stats.get(key), int)
            and not isinstance(stats.get(key), bool)
            and stats[key] >= 0
            for key in ("total_items", "total_metadata")
        ):
            raise DiscoveryInput(
                "Battery does not expose the audited metadata statistics contract. Check its selected source version."
            )
        evidence["battery_api_ready"] = True
        current = (document.get("Metadata Battery") or {}).get("url")
        if str(current or "").rstrip("/") not in {target, f"http://localhost:{port}"}:
            updates[("Metadata Battery", "url")] = target
        bound = str((cfg.get("env") or {}).get("CLI_DEBRID_BATTERY_PORT") or "")
        if bound != str(port):
            return _state(
                "waiting_dependency",
                "The managed CLI/Battery runtime ports are not yet synchronized. Apply the normal service configuration, then retry.",
                retryable=True,
                evidence=evidence,
            )
    wanted = cfg.get("use_phalanx_db") is True
    native = (document.get("UI Settings") or {}).get("enable_phalanx_db")
    if wanted and not (config.get("phalanx_db") or {}).get("enabled"):
        raise DiscoveryInput(
            "Enable the explicitly selected PhalanxDB companion first."
        )
    environment_changed = False
    if wanted:
        port = int((config.get("phalanx_db") or {}).get("port") or 8888)
        health = _get(
            factory(f"http://127.0.0.1:{port}", managed=True, timeout=8), "/health"
        ).json()
        if (
            not isinstance(health, dict)
            or health.get("status") != "healthy"
            or health.get("version") != "v4"
        ):
            raise DiscoveryInput(
                "The selected Phalanx build does not expose its audited v4 health contract."
            )
        evidence["phalanx_api_ready"] = True
        env_plan = sync_cli_phalanx_environment(
            cfg,
            config.get("phalanx_db") or {},
            adopt=values.get(f"{source}::adopt_companion_settings") is True,
            apply=False,
        )
        if env_plan["adoption_required"]:
            raise DiscoveryInput(
                "The existing Phalanx environment targets a custom endpoint. Explicitly adopt its companion binding before selecting managed Phalanx."
            )
        if env_plan["changed"]:
            if not apply:
                return _state(
                    "planned",
                    "Synchronize the explicitly selected managed Phalanx environment and native companion settings during a controlled CLI restart.",
                )
            if payload.get("_service_stopped") is not True or manager is None:
                raise DiscoveryInput(
                    "Authorize the controlled CLI restart and configuration persistence before replacing its Phalanx binding."
                )
            previous = copy.deepcopy(cfg.get("env"))
            had_env = "env" in cfg
            try:
                cfg["env"] = env_plan["environment"]
                manager.save_config(cfg.get("process_name") or "CLI Debrid")
            except Exception:
                if had_env:
                    cfg["env"] = previous
                else:
                    cfg.pop("env", None)
                return _state(
                    "failed",
                    "The managed Phalanx environment could not be persisted. The previous in-memory binding was restored.",
                    "failed",
                )
            write_ownership(
                Path(cfg.get("config_dir") or "/cli_debrid")
                / ".dumb-phalanx-environment.json",
                {
                    "binding": fingerprint(
                        {
                            key: cfg["env"].get(key)
                            for key in (
                                "CLI_DEBRID_PHALANX_HOST",
                                "CLI_DEBRID_PHALANX_PORT",
                            )
                        }
                    )
                },
            )
            environment_changed = True
        env = cfg.get("env") or {}
        host = env.get("CLI_DEBRID_PHALANX_HOST")
        origin = (
            "http://" + str(host)
            if host
            else str(env.get("CLI_DEBRID_PHALANX_URL") or "http://localhost")
        )
        actual_url = (
            origin.rstrip("/") + ":" + str(env.get("CLI_DEBRID_PHALANX_PORT") or 8888)
        )
        if actual_url not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            return _state(
                "waiting_dependency",
                "The managed Phalanx runtime binding differs from CLI's effective companion environment. Apply its explicit managed binding before retrying.",
                retryable=True,
                evidence=evidence,
            )
    if native is not wanted:
        updates[("UI Settings", "enable_phalanx_db")] = wanted
    if not updates:
        return _state(
            "applied" if environment_changed else "already_present",
            "CLI's native companion bindings match the selected layout and optional Phalanx choice. Provider accounts and Trakt authorization remain separate.",
            "passed",
            evidence=evidence,
        )
    ledger = path.parent / ".dumb-companion-ownership.json"
    records = read_ownership(ledger)
    for (section, key), desired in updates.items():
        existing = (document.get(section) or {}).get(key)
        if (
            existing not in (None, "")
            and records.get(section + "." + key) != fingerprint(existing)
            and values.get(f"{source}::adopt_companion_settings") is not True
        ):
            raise DiscoveryInput(
                "A native companion setting differs from the selected arrangement. Explicitly authorize replacing the companion binding; other user settings will be preserved."
            )
    if not apply:
        return _state(
            "planned",
            "Update only the selected native Battery URL/Phalanx binding during a controlled CLI Debrid restart.",
            evidence=evidence,
        )
    if payload.get("_service_stopped") is not True:
        raise DiscoveryInput(
            "Authorize a controlled CLI Debrid-only restart before writing its native companion configuration."
        )
    desired = copy.deepcopy(document)
    for (section, key), value in updates.items():
        if section in desired and not isinstance(desired[section], dict):
            raise DiscoveryInput(
                "The native companion settings use an unsupported schema."
            )
        desired.setdefault(section, {})[key] = value
        records[section + "." + key] = fingerprint(value)
    # The coordinator snapshots/restores this exact file if restart fails.
    atomic_write_private_text(path, json.dumps(desired, indent=2) + "\n")
    if json.loads(path.read_text()) != desired:
        raise DiscoveryInput(
            "The native CLI companion configuration did not retain its selected fields."
        )
    write_ownership(ledger, records)
    return _state(
        "applied",
        "Wrote and read back the scoped native companion bindings. The controlled restart must complete before live use.",
        "passed",
        evidence={**evidence, "restart_required": True},
    )


def _zurg_version(cfg):
    marker = Path(cfg.get("config_dir") or "/zurg") / "version.txt"
    version = (
        marker.read_text().strip()
        if marker.is_file() and not marker.is_symlink()
        else ""
    )
    if re.fullmatch(r"v?1\.0\.0", version):
        return version, "realdebrid"
    if re.fullmatch(r"v?0\.9\.3(?:-final)?", version):
        return version, "realdebrid"
    return version, "unknown"


def _zurg(action, config):
    cfg = _resource(config, action["source"])
    version, provider = _zurg_version(cfg)
    _, document = _load_document(_read(native_config_path(action, config)))
    evidence = {
        "version": version or "unknown",
        "provider": provider,
        "credential_present": bool(document.get("token")),
        "credential_verified": False,
    }
    if provider == "unknown":
        return _state(
            "unsupported",
            "This Zurg build has no audited provider contract. Sponsor/nightly provider discovery requires its selected version's native settings; setup will not replace provider YAML.",
            "unavailable",
            evidence=evidence,
        )
    if not document.get("token") or document.get("token") == "yourtoken":
        return _state(
            "waiting_input",
            "The selected public Zurg build requires a Real-Debrid token. Configure its provider authorization before mounting the library.",
            evidence=evidence,
        )
    return _state(
        "already_present",
        "Recognized the public Real-Debrid configuration and supplied token. Provider authorization and library visibility are verified separately by the mount/consumer checks.",
        "unavailable",
        evidence=evidence,
    )


def _ingestion(action, config, factory):
    cfg = _resource(config, action["source"])
    port = int(cfg.get("port") or 8181)
    response = _get(
        factory(f"http://127.0.0.1:{port}", managed=True, timeout=8),
        "/healthchecks/ping",
    )
    if not response.text.rstrip().endswith("Pong!"):
        raise DiscoveryInput("Zilean does not expose the audited healthcheck contract.")
    state = "unknown"
    path = Path(
        cfg.get("log_file")
        or str(Path(cfg.get("config_dir") or "/zilean") / "zilean.log")
    )
    if (
        path.is_file()
        and not path.is_symlink()
        and time.time() - path.stat().st_mtime < 24 * 3600
    ):
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 65536))
            tail = handle.read(65536).decode("utf-8", errors="replace")
        markers = [
            (tail.rfind(text), status)
            for text, status in (
                ("Downloading DMM Hashlists", "running"),
                ("DMM files processing started", "running"),
                ("DMM Internal Tasks Completed", "completed"),
                ("All files processed", "completed"),
            )
        ]
        markers.extend(
            (match.start(), "running")
            for match in re.finditer(r"\bFound [0-9]+ files to parse\b", tail)
        )
        position, observed = max(markers)
        if position >= 0:
            state = observed
    evidence = {
        "api_ready": True,
        "ingestion_observed": state,
        "dataset_count_verified": False,
    }
    return _state(
        "already_present",
        "Zilean's API is ready. Initial ingestion is reported separately from API health; allow its first population to finish before expecting indexer results.",
        "passed",
        evidence=evidence,
    )


def _identity(service, response):
    if service == "plex":
        root = ET.fromstring(response.content)
        return root.get("machineIdentifier") or None
    if service in {"jellyfin", "emby"}:
        data = response.json()
        identifier = data.get("Id", data.get("id"))
        if data.get("Id") and data.get("id") and data["Id"] != data["id"]:
            raise DiscoveryInput(
                "The public media identity response contains conflicting IDs."
            )
        return identifier if isinstance(identifier, str) and identifier else None
    return None


def _public_route(action, config, payload, factory):
    source, values = action["source"], _values(payload)
    origin, target = values.get(f"{source}::public_url"), values.get(
        f"{source}::public_target"
    )
    if (
        not isinstance(origin, str)
        or len(origin) > 2048
        or any(ord(ch) <= 32 or ch == "\\" for ch in origin)
    ):
        raise DiscoveryInput("Provide an explicit public HTTPS application URL.")
    parts = urlsplit(origin)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path.startswith("/ui/")
    ):
        raise DiscoveryInput(
            "Use a dedicated HTTPS application route without embedded credentials or an embedded DUMB UI path."
        )
    choices = {
        entry["value"]
        for field in schema_fields(
            {"id": source, "service_key": action["service_key"]}, config
        )
        if field["key"].endswith("::public_target")
        for entry in field["options"]
    }
    if target not in choices:
        raise DiscoveryInput(
            "Select the enabled managed application behind this public route."
        )
    service, cfg = target.split(".", 1)[0], _resource(config, target)
    paths = {
        "plex": "/identity",
        "jellyfin": "/System/Info/Public",
        "emby": "/System/Info/Public",
        "aiostreams": "/api/v1/status",
        "seerr": "/api/v1/status",
        "audiobookshelf": "/status",
    }
    path = paths.get(service, "/")
    default_ports = {
        "plex": 32400,
        "jellyfin": 8096,
        "emby": 8096,
        "aiostreams": 3006,
        "seerr": 5055,
        "audiobookshelf": 13378,
        "pulsarr": 3003,
        "maintainerr": 6246,
        "traefik_proxy_admin": 3004,
        "dumb_frontend": 3000,
    }
    local = factory(
        f"http://127.0.0.1:{int(cfg.get('port') or default_ports.get(service, 3000))}"
        + str(
            cfg.get("base_path")
            or cfg.get("url_base")
            or (cfg.get("env") or {}).get("BASE_PATH")
            or ""
        ).rstrip("/"),
        managed=True,
        timeout=8,
    )
    public = factory(
        origin.rstrip("/"),
        managed=False,
        allow_private=values.get(f"{source}::public_allow_private") is True,
        verify_tls=values.get(f"{source}::public_verify_tls") is not False,
        timeout=8,
    )
    evidence = {
        "dns": "not_checked",
        "tls": "not_checked",
        "http": "not_checked",
        "application_identity": "unknown",
        "dns_ownership": "unknown",
    }
    try:
        remote = public.request("GET", path)
    except ApiError as exc:
        stage = getattr(exc, "stage", None) or "unknown"
        evidence["failed_stage"] = stage
        if stage not in {"dns", "unknown"}:
            evidence["dns"] = "passed"
        if stage == "tls":
            evidence["tls"] = "failed"
        return _state(
            "waiting_dependency",
            "The public route check failed during DNS, TLS or transport setup. Correct the indicated stage before retrying; no cloud/DNS settings were changed.",
            retryable=True,
            evidence=evidence,
        )
    evidence.update(
        {
            "dns": "passed",
            "tls": (
                "passed"
                if values.get(f"{source}::public_verify_tls") is not False
                else "verification_disabled"
            ),
            "http": remote.status_code,
        }
    )
    if 300 <= remote.status_code < 400:
        return _state(
            "waiting_input",
            "The public route redirects. Supply its final application HTTPS URL; setup does not follow redirects or transmit login credentials.",
            evidence=evidence,
        )
    if remote.status_code in {401, 403}:
        return _state(
            "already_present",
            "The HTTPS route is reachable and access-protected. Application identity and DNS account ownership remain unverified; finish authorization in the existing route/protection panel.",
            "unavailable",
            evidence=evidence,
        )
    if not 200 <= remote.status_code < 300:
        return _state(
            "waiting_dependency",
            "The public route returned an unsuccessful HTTP status. Check its selected origin and proxy policy.",
            retryable=True,
            evidence=evidence,
        )
    native = _get(local, path)
    expected, actual = _identity(service, native), _identity(service, remote)
    if expected and actual:
        evidence["application_identity"] = (
            "matched" if expected == actual else "mismatched"
        )
        if expected != actual:
            return _state(
                "failed",
                "The public route belongs to a different media-server identity. Correct the selected route before using it.",
                "failed",
                evidence=evidence,
            )
        return _state(
            "already_present",
            "DNS, HTTPS, HTTP and the selected application's public identity match. DNS account ownership and any explicitly disabled certificate verification remain separate operator decisions.",
            (
                "passed"
                if values.get(f"{source}::public_verify_tls") is not False
                else "unavailable"
            ),
            evidence=evidence,
        )
    return _state(
        "already_present",
        "The selected application endpoint and public HTTPS route respond. This application's anonymous API cannot prove instance identity or DNS ownership; review the route in its existing administration panel.",
        "unavailable",
        evidence=evidence,
    )


def _hook(action, config, payload, apply, factory):
    from utils.zurg_library_hook import validate_hook_configuration

    source, values, cfg = (
        action["source"],
        _values(payload),
        _resource(config, action["source"]),
    )
    if values.get(f"{source}::enable_library_hook") is not True:
        return _state(
            "already_present",
            "Automatic library scans are not selected. Existing user hooks are preserved.",
            "unavailable",
        )
    version, provider = _zurg_version(cfg)
    if provider == "unknown":
        raise DiscoveryInput(
            "Select an audited public Zurg version before adding a library hook; unknown sponsor/nightly contracts are preserved."
        )
    if values.get(f"{source}::media_server") != "plex" or not (
        config.get("plex") or {}
    ).get("enabled"):
        raise DiscoveryInput(
            "Choose the managed Plex server for this optional update hook."
        )
    url, token, _ = _managed_plex(config, values, "plex", prefer_managed=True)
    if not token:
        raise DiscoveryInput(
            "Authorize the selected Plex server before installing its optional update hook."
        )
    raw_ids = values.get(f"{source}::scan_library_ids")
    ids = (
        [item.strip() for item in raw_ids.split(",")]
        if isinstance(raw_ids, str)
        else []
    )
    if (
        not ids
        or len(ids) > 50
        or any(not re.fullmatch(r"[0-9]{1,12}", item) for item in ids)
        or len(set(ids)) != len(ids)
    ):
        raise DiscoveryInput(
            "Choose a bounded comma-separated list of exact Plex library IDs."
        )
    root = values.get(f"{source}::scan_mount_path")
    client = factory(url, managed=True, headers={"X-Plex-Token": token}, timeout=8)
    response = _get(client, "/library/sections")
    sections = ET.fromstring(response.content)
    libraries = {
        row.get("key"): [item.get("path") for item in row.findall("Location")]
        for row in sections.findall("Directory")
    }
    if set(ids) - set(libraries):
        raise DiscoveryInput(
            "A selected library ID does not belong to this Plex server."
        )
    desired = {
        "url": url,
        "api_key": token,
        "mount_path": root,
        "libraries": {key: libraries[key] for key in ids},
    }
    validate_hook_configuration(desired)
    path = native_config_path(action, config)
    parser, document = _load_document(_read(path))
    private = path.parent / ".dumb-zurg-library-hook.json"
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).with_name('zurg_library_hook.py').resolve()))} {shlex.quote(str(private))} \"$@\""
    ledger = path.parent / ".dumb-zurg-hook-ownership.json"
    ownership = read_ownership(ledger)
    current = document.get("on_library_update") or ""
    previous = ownership.get("on_library_update")
    if (
        current
        and current != command
        and previous != fingerprint(current)
        and values.get(f"{source}::adopt_library_hook") is not True
    ):
        raise DiscoveryInput(
            "This Zurg instance has a user-owned update hook. Explicitly authorize replacing it before enabling DUMB's scoped hook."
        )
    stored = json.loads(_read(private)) if private.exists() else None
    if current == command and stored == desired:
        return _state(
            "already_present",
            "The optional selected-library hook is registered and its Plex libraries/credentials are reachable. No scan was triggered during this check.",
            "passed",
            evidence={"library_count": len(ids), "scan_triggered": False},
        )
    if not apply:
        return _state(
            "planned",
            "Register a private hook that refreshes only selected Plex library paths when this Zurg instance reports an update.",
        )
    if payload.get("_service_stopped") is not True:
        raise DiscoveryInput(
            "Authorize the controlled restart of this Zurg instance before changing its native update hook."
        )
    owner = (
        int(config.get("puid", os.geteuid())),
        int(config.get("pgid", os.getegid())),
    )
    atomic_write_private_text(
        private, json.dumps(desired), owner=owner if not private.exists() else None
    )
    document["on_library_update"] = command
    output = StringIO()
    parser.dump(document, output)
    atomic_write_private_text(path, output.getvalue())
    _, check = _load_document(path.read_text())
    if check.get("on_library_update") != command:
        raise DiscoveryInput("Zurg did not retain the selected update-hook command.")
    write_ownership(ledger, {"on_library_update": fingerprint(command)})
    return _state(
        "applied",
        "Registered and read back the private selected-library hook. The controlled Zurg restart must complete before update events use it.",
        "passed",
        evidence={
            "library_count": len(ids),
            "scan_triggered": False,
            "restart_required": True,
            "version": version,
        },
    )


def _dispatch(action, config, payload, *, apply, factory, manager=None):
    service, kind = action.get("service_key"), action.get("action")
    try:
        if kind == "public_route" and service in {
            "cloudflared",
            "traefik",
            "traefik_proxy_admin",
        }:
            return _public_route(action, config, payload, factory)
        if kind == "companion_connection" and service == "cli_debrid":
            return _cli(action, config, payload, apply, factory, manager)
        if kind == "discovery" and service == "zurg":
            return _zurg(action, config)
        if kind in {"ingestion", "discovery"} and service == "zilean":
            return _ingestion(action, config, factory)
        if kind == "library_hook" and service == "zurg":
            return _hook(action, config, payload, apply, factory)
        return None
    except (DiscoveryInput, ZurgConfigError) as exc:
        return _state("waiting_input", str(exc))
    except ApiError as exc:
        if exc.status_code in {401, 403}:
            return _state(
                "waiting_input",
                "The selected application rejected authorization. Check its configured account/key before retrying.",
            )
        return _state(
            "waiting_dependency",
            "The selected native service is not ready for its bounded discovery check.",
            retryable=True,
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        ET.ParseError,
        DefusedXmlException,
    ):
        return _state(
            "waiting_input",
            "Check the selected native version, regular configuration files and explicit integration choices. Sensitive native errors are not exposed.",
        )


def inspect_discovery(
    action, config, payload, operation=None, *, client_factory=ApiClient
):
    del operation
    return _dispatch(action, config, payload, apply=False, factory=client_factory)


def apply_discovery(
    action, config, payload, operation=None, *, client_factory=ApiClient, manager=None
):
    del operation
    return _dispatch(
        action, config, payload, apply=True, factory=client_factory, manager=manager
    )
