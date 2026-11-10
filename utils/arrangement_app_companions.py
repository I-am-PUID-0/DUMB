"""Explicit Maintainerr companion connections; no policy activation."""

from __future__ import annotations

import configparser
import json
from pathlib import Path

from utils.arrangement_media import _resource, _state, _values


def schema_fields(resource, config, payload=None):
    if resource["service_key"] != "maintainerr":
        return []
    rid = resource["id"]
    options = [
        {"value": "", "label": "Choose later"},
        {"value": "none", "label": "No Seerr connection"},
    ]
    candidates = {
        f"seerr.{key}"
        for key, instance in (
            (config.get("seerr") or {}).get("instances") or {}
        ).items()
        if isinstance(instance, dict) and instance.get("enabled")
    }
    cores = (payload or {}).get("core_services") or []
    cores = [cores] if isinstance(cores, dict) else cores
    for entry in cores:
        if (
            isinstance(entry, dict)
            and entry.get("name") == "seerr"
            and entry.get("instance_name")
        ):
            candidates.add("seerr." + entry["instance_name"])
    options += [
        {"value": key, "label": key.replace("seerr.", "Seerr — ", 1)}
        for key in sorted(candidates, key=str.lower)
    ]
    return [
        {
            "key": f"{rid}::seerr_instance",
            "resource_id": rid,
            "service_key": "maintainerr",
            "label": "Maintainerr request server",
            "type": "connection",
            "required": False,
            "description": "Choose the exact Seerr instance to connect. No request/deletion policy is enabled.",
            "options": options,
        },
        {
            "key": f"{rid}::connect_tautulli",
            "resource_id": rid,
            "service_key": "maintainerr",
            "label": "Connect Tautulli to Maintainerr",
            "type": "boolean",
            "required": False,
            "description": "Explicitly connect the configured Tautulli instance. Its Plex/account authorization must already be complete.",
        },
    ]


def configure(action, config, payload, *, apply, factory, ownership_directory):
    from utils import arrangement_app_media as app

    source, kind, values = action["source"], action["action"], _values(payload)
    if kind == "seerr_connection":
        target = values.get(f"{source}::seerr_instance")
        if not target or target == "none":
            return _state(
                "waiting_input",
                "Choose the exact Seerr instance or explicitly waive this optional connection.",
                missing_inputs=[f"{source}::seerr_instance"],
            )
        cfg = _resource(config, str(target))
        if not cfg.get("enabled") or str(target).split(".", 1)[0] != "seerr":
            raise app.AppMediaInputError("Choose an enabled Seerr instance.")
        token = values.get(f"{target}::api_key") or cfg.get("api_key") or ""
        if not token:
            path = Path(
                cfg.get("config_file")
                or str(Path(cfg.get("config_dir") or "/seerr") / "settings.json")
            )
            if path.is_symlink():
                raise app.AppMediaInputError(
                    "Generated Seerr credentials must be in a regular file."
                )
            token = (json.loads(path.read_text()).get("main") or {}).get("apiKey") or ""
        base = str(cfg.get("base_path") or cfg.get("url_base") or "").rstrip("/")
        family, port = "seerr", int(cfg.get("port") or 5055)
    else:
        if values.get(f"{source}::connect_tautulli") is not True:
            return _state(
                "waiting_input",
                "Choose whether to connect Tautulli before applying this optional connection.",
                missing_inputs=[f"{source}::connect_tautulli"],
            )
        cfg = config.get("tautulli") or {}
        if not cfg.get("enabled"):
            raise app.AppMediaInputError(
                "The selected Tautulli instance is disabled or missing."
            )
        path = Path(cfg.get("config_file") or "/tautulli/data/config.ini")
        if path.is_symlink():
            raise app.AppMediaInputError(
                "Generated Tautulli credentials must be in a regular file."
            )
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(path.read_text())
        token = values.get("tautulli::api_key") or parser.get(
            "General", "api_key", fallback=""
        ).strip(chr(34) + chr(39) + " ")
        base = (
            parser.get("General", "http_root", fallback="")
            .strip(chr(34) + chr(39) + " ")
            .rstrip("/")
        )
        family, port = "tautulli", int(cfg.get("port") or 8181)
    if not isinstance(token, str) or not token:
        raise app.AppMediaInputError(
            "The selected companion's application API key is not ready."
        )
    if base and (
        not base.startswith("/")
        or base.startswith("//")
        or any(ord(ch) < 32 or ch in "?#" for ch in base)
    ):
        raise app.AppMediaInputError("The companion URL base must be a path.")
    url = f"http://127.0.0.1:{port}{base}"
    client, source_cfg = app._source(action, config, payload, factory)
    save_path, test_path = f"/api/settings/{family}", f"/api/settings/test/{family}"
    current = app._request(client, "GET", save_path)
    if not isinstance(current, dict) or "url" not in current:
        raise app.AppMediaInputError(
            "This Maintainerr version does not expose supported companion settings."
        )
    desired = {"url": url, "api_key": token}
    ledger = app._ledger(source_cfg, source + "." + family, ownership_directory)
    from utils.integration_ownership import fingerprint

    _records, changed = app._ownership(ledger, current, desired, fingerprint(url))
    if changed and not apply:
        return _state(
            "planned",
            "The selected companion is ready to configure; workflow policies remain unchanged.",
        )
    tested = app._request(client, "POST", test_path, json=desired)
    if (
        not isinstance(tested, dict)
        or tested.get("status") != "OK"
        or tested.get("code") != 1
    ):
        raise app.AppMediaInputError(
            "Maintainerr could not authenticate the selected companion."
        )
    if changed:
        app._request(client, "POST", save_path, json=desired)
        app._save_ownership(ledger, desired, fingerprint(url))
    saved = app._request(client, "GET", save_path)
    if not isinstance(saved, dict) or any(
        saved.get(key) != value for key, value in desired.items()
    ):
        return _state(
            "failed",
            "The companion connection did not match its authenticated readback.",
            verification="failed",
            retryable=True,
        )
    return _state(
        "applied" if changed else "already_present",
        "The selected companion passed authentication and settings readback. No retention or deletion policy was enabled.",
        verification="passed",
        evidence={"connection": "passed", "policies_activated": False},
    )
