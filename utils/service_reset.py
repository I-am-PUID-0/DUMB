"""Guarded service/instance reset and removal planning.

The reset workflow deliberately owns only DUMB configuration and paths that can
be attributed to one configured service.  Media mounts, symlink libraries,
shared caches, and PostgreSQL databases are outside this boundary.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


class ServiceResetError(ValueError):
    """Raised when a requested reset cannot be performed safely."""


PROTECTED_SERVICE_KEYS = {"dumb", "dumb_api_service", "dumb_frontend"}
PROTECTED_PATHS = {
    "/",
    "/config",
    "/data",
    "/log",
    "/cache",
    "/mnt",
    "/mnt/debrid",
    "/dumb",
    "/utils",
    "/usr",
    "/opt",
    "/etc",
    "/var",
}
DIRECTORY_FIELDS = ("config_dir", "install_dir")
FILE_FIELDS = ("config_file", "log_file", "access_log_file")
IDENTITY_FIELDS = {
    "process_name",
    "port",
    "frontend_port",
    "backend_port",
    "config_dir",
    "install_dir",
    "config_file",
    "log_file",
    "access_log_file",
    "mount_dir",
    "mount_name",
    "cache_dir",
    "zurg_config_file",
    "key_type",
}

# Persistent locations created by utils.user_management.migrate_symlinks().
# These are data/config/runtime roots, not media-library or mount roots.
PERSISTENT_DATA_NAMES = {
    "riven_backend": "riven",
    "postgres": "postgres",
    "pgadmin": "pgadmin",
    "zilean": "zilean",
    "cli_debrid": "cli_debrid",
    "cli_battery": "cli_debrid",
    "phalanx_db": "phalanx_db",
    "decypharr": "decypharr",
    "infinidysk": "infinidysk",
    "plex": "plex",
    "tautulli": "tautulli",
    "bazarr": "bazarr",
    "seerr": "seerr",
    "jellyfin": "jellyfin",
    "emby": "emby",
    "sonarr": "sonarr",
    "radarr": "radarr",
    "lidarr": "lidarr",
    "prowlarr": "prowlarr",
    "whisparr": "whisparr",
    "traefik": "traefik",
    "traefik_proxy_admin": "traefik_proxy_admin",
    "cloudflared": "cloudflared",
    "neutarr": "neutarr",
    "profilarr": "profilarr",
    "pulsarr": "pulsarr",
    "maintainerr": "maintainerr",
    "mediastorm": "mediastorm",
    "altmount": "altmount",
}


def _normalized_absolute(path: str) -> str:
    value = os.path.normpath(str(path or "").strip())
    if not value.startswith("/"):
        raise ServiceResetError("Managed cleanup paths must be absolute.")
    return value


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _path_exists(path: str) -> bool:
    return os.path.lexists(path) or os.path.exists(os.path.realpath(path))


def _safe_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")


def _load_defaults(config_manager) -> dict[str, Any]:
    try:
        with open(config_manager.default_config_path, "r", encoding="utf-8") as handle:
            defaults = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ServiceResetError(
            "DUMB's default configuration is unavailable."
        ) from error
    if not isinstance(defaults, dict):
        raise ServiceResetError("DUMB's default configuration is invalid.")
    return defaults


def _resolve_service(config_manager, process_name: str) -> dict[str, Any]:
    requested = str(process_name or "").strip()
    if not requested:
        raise ServiceResetError("process_name is required.")
    service_key, instance_name = config_manager.find_key_for_process(requested)
    if not service_key or service_key in PROTECTED_SERVICE_KEYS:
        raise ServiceResetError("This DUMB control-plane service cannot be reset here.")

    section = config_manager.config.get(service_key)
    if not isinstance(section, dict):
        raise ServiceResetError("Service configuration was not found.")
    if instance_name is not None:
        instances = section.get("instances")
        current = instances.get(instance_name) if isinstance(instances, dict) else None
    else:
        current = section
    if not isinstance(current, dict) or current.get("process_name") != requested:
        raise ServiceResetError("Service configuration changed; refresh and try again.")
    return {
        "process_name": requested,
        "service_key": service_key,
        "instance_name": instance_name,
        "section": section,
        "current": current,
    }


def _default_template(
    defaults: dict[str, Any], resolved: dict[str, Any]
) -> tuple[str | None, dict[str, Any]]:
    service_key = resolved["service_key"]
    default_section = defaults.get(service_key)
    if not isinstance(default_section, dict):
        raise ServiceResetError("No reset template is available for this service.")
    if resolved["instance_name"] is None:
        return None, copy.deepcopy(default_section)

    default_instances = default_section.get("instances")
    if not isinstance(default_instances, dict) or not default_instances:
        raise ServiceResetError(
            "No instance reset template is available for this service."
        )
    template_name, template = next(iter(default_instances.items()))
    if not isinstance(template, dict):
        raise ServiceResetError("The instance reset template is invalid.")
    return template_name, copy.deepcopy(template)


def _replace_template_values(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        updated = value
        for old, new in sorted(
            replacements.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if old:
                updated = updated.replace(old, new)
        return updated
    if isinstance(value, list):
        return [_replace_template_values(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_template_values(item, replacements)
            for key, item in value.items()
        }
    return value


def _reset_instance_config(
    template: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    replacements: dict[str, str] = {}
    for field in IDENTITY_FIELDS:
        old = template.get(field)
        new = current.get(field)
        if isinstance(old, str) and isinstance(new, str) and old != new:
            replacements[old] = new
    reset = _replace_template_values(template, replacements)
    for field in IDENTITY_FIELDS:
        if field in current:
            reset[field] = copy.deepcopy(current[field])
    reset["enabled"] = False
    return reset


def _configured_entries(config: dict[str, Any]):
    for service_key, section in config.items():
        if not isinstance(section, dict):
            continue
        instances = section.get("instances")
        if isinstance(instances, dict):
            for instance_name, instance in instances.items():
                if isinstance(instance, dict):
                    yield service_key, instance_name, instance
        elif section.get("process_name"):
            yield service_key, None, section


def _default_owned_roots(defaults: dict[str, Any], service_key: str) -> list[str]:
    section = defaults.get(service_key)
    if not isinstance(section, dict):
        return []
    instances = section.get("instances")
    template = (
        next(iter(instances.values()), {})
        if isinstance(instances, dict) and instances
        else section
    )
    roots = []
    for field in DIRECTORY_FIELDS:
        value = template.get(field) if isinstance(template, dict) else None
        if not isinstance(value, str) or not value.startswith("/"):
            continue
        normalized = _normalized_absolute(value)
        if isinstance(instances, dict):
            normalized = os.path.dirname(normalized)
        roots.append(normalized)
    return roots


def _persistent_data_path(
    config: dict[str, Any],
    service_key: str,
    instance_name: str | None,
    current: dict[str, Any],
) -> str | None:
    data_root = str(config.get("data_root") or "/data").strip() or "/data"
    if not data_root.startswith("/"):
        return None
    if service_key == "zurg":
        config_dir = str(current.get("config_dir") or "").strip()
        leaf = os.path.basename(config_dir.rstrip("/"))
        return os.path.join(data_root, f"zurg_{leaf}") if leaf else None
    if service_key == "infinidysk":
        config_dir = str(current.get("config_dir") or "").strip()
        leaf = os.path.basename(config_dir.rstrip("/"))
        return os.path.join(data_root, leaf or "infinidysk")
    name = PERSISTENT_DATA_NAMES.get(service_key)
    if not name:
        return None
    base = os.path.join(data_root, name)
    if instance_name is None:
        return base
    config_dir = str(current.get("config_dir") or "").strip()
    leaf = os.path.basename(config_dir.rstrip("/"))
    return os.path.join(base, leaf) if leaf else None


def _other_owned_roots(
    config: dict[str, Any], selected_process: str
) -> list[tuple[str, str]]:
    roots: list[tuple[str, str]] = []
    for service_key, instance_name, entry in _configured_entries(config):
        process_name = str(entry.get("process_name") or "").strip()
        if not process_name or process_name == selected_process:
            continue
        for field in DIRECTORY_FIELDS:
            path = entry.get(field)
            if isinstance(path, str) and path.startswith("/"):
                roots.append(
                    (process_name, os.path.realpath(_normalized_absolute(path)))
                )
        persistent = _persistent_data_path(config, service_key, instance_name, entry)
        if persistent:
            roots.append(
                (process_name, os.path.realpath(_normalized_absolute(persistent)))
            )
    return roots


def _is_shared_directory(path: str, other_roots: list[tuple[str, str]]) -> list[str]:
    resolved = os.path.realpath(path)
    owners = []
    for process_name, other in other_roots:
        if resolved == other or _is_within(other, resolved):
            owners.append(process_name)
    return sorted(set(owners))


def _directory_allowed(path: str, allowed_roots: list[str]) -> bool:
    normalized = _normalized_absolute(path)
    if normalized in PROTECTED_PATHS:
        return False
    if not any(_is_within(normalized, root) for root in allowed_roots):
        return False
    resolved = os.path.realpath(normalized)
    canonical_roots = [root for root in allowed_roots if os.path.realpath(root) == root]
    return any(_is_within(resolved, root) for root in canonical_roots)


def _candidate_targets(
    config_manager, defaults: dict[str, Any], resolved: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    service_key = resolved["service_key"]
    current = resolved["current"]
    process_name = resolved["process_name"]
    instance_name = resolved["instance_name"]
    warnings: list[str] = []
    candidates: list[tuple[str, str, str]] = []
    allowed_roots = _default_owned_roots(defaults, service_key)

    persistent = _persistent_data_path(
        config_manager.config, service_key, instance_name, current
    )
    if persistent:
        allowed_roots.append(_normalized_absolute(persistent))

    for field in DIRECTORY_FIELDS:
        value = current.get(field)
        if isinstance(value, str) and value.startswith("/"):
            candidates.append((value, "directory", field))
    if persistent:
        candidates.append((persistent, "directory", "persistent_data"))

    other_roots = _other_owned_roots(config_manager.config, process_name)
    targets: list[dict[str, Any]] = []
    seen_resolved: set[str] = set()
    accepted_directories: list[str] = []
    for raw_path, kind, source in candidates:
        path = _normalized_absolute(raw_path)
        if not _directory_allowed(path, allowed_roots):
            warnings.append(
                f"Retained {path}: it is outside this service's DUMB-managed root."
            )
            continue
        shared_with = _is_shared_directory(path, other_roots)
        if shared_with:
            warnings.append(
                f"Retained shared path {path}; it is also used by {', '.join(shared_with)}."
            )
            continue
        resolved_path = os.path.realpath(path)
        if resolved_path in seen_resolved:
            continue
        seen_resolved.add(resolved_path)
        accepted_directories.append(path)
        targets.append(
            {
                "path": path,
                "resolved_path": resolved_path,
                "kind": kind,
                "source": source,
                "operation": "clear_directory",
                "exists": _path_exists(path),
            }
        )

    _, default_template = _default_template(defaults, resolved)
    rclone_shared_config = service_key == "rclone"
    for field in FILE_FIELDS:
        value = current.get(field)
        default_value = default_template.get(field)
        if not isinstance(value, str) or not value.startswith("/"):
            continue
        path = _normalized_absolute(value)
        if any(_is_within(path, directory) for directory in accepted_directories):
            continue
        if rclone_shared_config and field == "config_file":
            warnings.append(f"Retained shared rclone configuration file {path}.")
            continue
        if not isinstance(default_value, str) or path != os.path.normpath(
            default_value
        ):
            warnings.append(
                f"Retained custom {field} path {path}; only template-owned files are removed."
            )
            continue
        resolved_path = os.path.realpath(path)
        if resolved_path in seen_resolved or path in PROTECTED_PATHS:
            continue
        seen_resolved.add(resolved_path)
        targets.append(
            {
                "path": path,
                "resolved_path": resolved_path,
                "kind": "file",
                "source": field,
                "operation": "delete_file",
                "exists": _path_exists(path),
            }
        )

    return targets, warnings


def _reference_warnings(config: dict[str, Any], resolved: dict[str, Any]) -> list[str]:
    selected_tokens = {
        str(resolved["service_key"]).casefold(),
        str(resolved["process_name"]).casefold(),
    }
    if resolved["instance_name"]:
        selected_tokens.add(str(resolved["instance_name"]).casefold())
    references = []
    for _, _, entry in _configured_entries(config):
        owner = str(entry.get("process_name") or "").strip()
        if not owner or owner == resolved["process_name"]:
            continue
        values = []
        for field in ("core_service", "core_services"):
            value = entry.get(field)
            values.extend(value if isinstance(value, list) else [value])
        if any(str(value or "").casefold() in selected_tokens for value in values):
            references.append(owner)
    if not references:
        return []
    return [
        "Other configured services reference this target: "
        + ", ".join(sorted(set(references)))
        + ". Their configuration will not be changed."
    ]


def build_service_reset_preview(
    config_manager, process_name: str, action: str
) -> dict[str, Any]:
    action = str(action or "reset").strip().lower()
    if action not in {"reset", "remove"}:
        raise ServiceResetError("action must be 'reset' or 'remove'.")
    defaults = _load_defaults(config_manager)
    resolved = _resolve_service(config_manager, process_name)
    template_name, _ = _default_template(defaults, resolved)
    is_custom_instance = bool(
        resolved["instance_name"] is not None
        and resolved["instance_name"] != template_name
    )
    instances = resolved["section"].get("instances")
    default_instance_after_removal = (
        template_name
        if action == "remove"
        and is_custom_instance
        and isinstance(instances, dict)
        and len(instances) == 1
        else None
    )
    config_action = (
        "remove_instance"
        if action == "remove" and is_custom_instance
        else "reset_to_defaults"
    )
    targets: list[dict[str, Any]] = []
    warnings = _reference_warnings(config_manager.config, resolved)
    if action == "remove":
        if resolved["service_key"] == "postgres":
            warnings.append(
                "PostgreSQL is a shared dependency. Its cluster files and databases are retained; only its DUMB configuration can be reset."
            )
        else:
            targets, path_warnings = _candidate_targets(
                config_manager, defaults, resolved
            )
            warnings.extend(path_warnings)
    retained = [
        "Media mounts and symlink libraries",
        "Shared install/dependency caches",
        "PostgreSQL databases and other external data stores",
        "Configuration belonging to other services",
    ]
    return {
        "process_name": resolved["process_name"],
        "service_key": resolved["service_key"],
        "instance_name": resolved["instance_name"],
        "action": action,
        "config_action": config_action,
        "default_instance_after_removal": default_instance_after_removal,
        "will_stop": True,
        "file_targets": targets,
        "warnings": warnings,
        "retained": retained,
        "confirmation": resolved["process_name"],
    }


def _write_private_config_backup(config_manager, process_name: str) -> str:
    backup_dir = os.path.join(
        os.path.dirname(config_manager.file_path), "service-reset-backups"
    )
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(backup_dir, 0o700)
    except OSError:
        pass
    filename = (
        f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-"
        f"{_safe_slug(process_name) or 'service'}-{uuid.uuid4().hex[:8]}.json"
    )
    fd, temporary = tempfile.mkstemp(dir=backup_dir, suffix=".tmp")
    destination = os.path.join(backup_dir, filename)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config_manager.config, handle, indent=4)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        return destination
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _clear_directory(path: str) -> None:
    resolved = os.path.realpath(path)
    if not os.path.exists(resolved):
        return
    if not os.path.isdir(resolved):
        raise ServiceResetError(f"Expected a managed directory at {path}.")
    for name in os.listdir(resolved):
        child = os.path.join(resolved, name)
        if os.path.islink(child) or os.path.isfile(child):
            os.unlink(child)
        elif os.path.isdir(child):
            shutil.rmtree(child)
        else:
            os.unlink(child)


def _delete_target(target: dict[str, Any]) -> bool:
    path = target["path"]
    if target["operation"] == "clear_directory":
        existed = _path_exists(path)
        _clear_directory(path)
        return existed
    if target["operation"] == "delete_file":
        if not os.path.lexists(path):
            return False
        if os.path.isdir(path) and not os.path.islink(path):
            raise ServiceResetError(f"Expected a managed file at {path}.")
        os.unlink(path)
        return True
    raise ServiceResetError("Unknown managed cleanup operation.")


def _stop_and_verify(process_handler, process_name: str) -> None:
    process = None
    process_group = None
    process_names = getattr(process_handler, "process_names", None)
    if isinstance(process_names, dict):
        internal_name = process_name
        prefixed_name = getattr(process_handler, "_prefixed_name", None)
        if callable(prefixed_name):
            internal_name = (
                process_name
                if process_name in process_names
                else prefixed_name(process_name)
            )
        process = process_names.get(internal_name)
        if process is not None:
            try:
                process_group = os.getpgid(process.pid)
            except (OSError, AttributeError):
                process_group = None

    process_handler.stop_process(process_name)
    process_alive = process is not None and process.poll() is None
    group_alive = False
    group_check = getattr(process_handler, "_process_group_alive", None)
    if process_group is not None and callable(group_check):
        group_alive = group_check(process_group)
    if process_alive or group_alive:
        raise ServiceResetError(
            "The selected service did not stop completely; no files or configuration were changed."
        )


def _apply_config_reset(
    config_manager, preview: dict[str, Any], defaults: dict[str, Any]
) -> None:
    resolved = _resolve_service(config_manager, preview["process_name"])
    template_name, template = _default_template(defaults, resolved)
    service_key = resolved["service_key"]
    instance_name = resolved["instance_name"]
    if preview["config_action"] == "remove_instance":
        instances = config_manager.config[service_key].get("instances")
        if not isinstance(instances, dict) or instance_name not in instances:
            raise ServiceResetError("The selected instance no longer exists.")
        del instances[instance_name]
        # ConfigManager's normal default merge restores the built-in template
        # whenever an instance map is empty. Persist that stable state now so
        # observers never see an intermediate ``instances: {}`` and a later,
        # apparently unrelated config save does not appear to change it.
        if not instances:
            template["enabled"] = False
            instances[template_name] = template
    elif instance_name is None:
        template["enabled"] = False
        config_manager.config[service_key] = template
    else:
        instances = config_manager.config[service_key].get("instances")
        if not isinstance(instances, dict):
            raise ServiceResetError("The selected instance no longer exists.")
        if instance_name == template_name:
            template["enabled"] = False
            instances[instance_name] = template
        else:
            instances[instance_name] = _reset_instance_config(
                template, resolved["current"]
            )
    config_manager.save_config()


def execute_service_reset(
    config_manager,
    process_handler,
    process_name: str,
    action: str,
    confirmation: str,
) -> dict[str, Any]:
    preview = build_service_reset_preview(config_manager, process_name, action)
    if str(confirmation or "") != preview["confirmation"]:
        raise ServiceResetError("Confirmation must exactly match the process name.")

    _stop_and_verify(process_handler, preview["process_name"])
    config_before = copy.deepcopy(config_manager.config)
    backup_path = _write_private_config_backup(config_manager, preview["process_name"])
    removed_paths = []
    try:
        if preview["action"] == "remove":
            for target in preview["file_targets"]:
                if _delete_target(target):
                    removed_paths.append(target["path"])
        defaults = _load_defaults(config_manager)
        _apply_config_reset(config_manager, preview, defaults)
    except Exception:
        config_manager.config = config_before
        # File removal is intentionally irreversible, but the full DUMB config
        # backup remains available for manual recovery if a later step fails.
        raise

    return {
        **preview,
        "status": "completed",
        "config_backup_path": backup_path,
        "removed_paths": removed_paths,
    }
