"""Exact service-instance backup ownership without importing the live controller.

Service archives carry only the selected configuration fragment.  Physical roots
must have exclusive declared ownership; a common data/config parent never grants
ownership of its other children.  The archive and transaction layers still check
identities, mounts, metadata and contents before copying or replacing anything.
"""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import stat

from jsonschema import ValidationError, validate

from utils.backup_storage import _filesystem_coordinate
from utils.installation_backup_archive import InstallationArchiveError, identity
from utils.installation_backup_inventory import (
    FILE_FIELDS,
    FORBIDDEN_ROOTS,
    PERSISTENT_FIELDS,
    _effective_persistence,
    service_entries,
)
from utils.installation_backup_limits import MAX_INSTALLATION_ROOTS
from utils.service_reset import _persistent_data_path


class ServiceBackupScopeError(InstallationArchiveError):
    """A requested selection cannot be isolated from other installation state."""

    def __init__(self, message, *, required_service_ids=()):
        super().__init__(message)
        self.required_service_ids = tuple(required_service_ids)


PROTECTED_SERVICE_KEYS = {"dumb", "dumb_api_service", "dumb_frontend", "postgres"}
KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
MAX_SERVICES = 256


def _service_id(key, instance_name):
    if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
        raise ServiceBackupScopeError("A service configuration key is unsupported.")
    if instance_name is not None and (
        not isinstance(instance_name, str)
        or not instance_name
        or len(instance_name) > 80
        or instance_name.strip() != instance_name
        or any(
            ord(character) < 32 or ord(character) == 127 for character in instance_name
        )
        or any(character in instance_name for character in ":/\\")
        or instance_name in {".", ".."}
    ):
        raise ServiceBackupScopeError("A service instance name is unsupported.")
    return key if instance_name is None else f"{key}:{instance_name}"


def configured_service_entries(config):
    """Return private exact selectors, including disabled configured instances."""
    if not isinstance(config, dict):
        raise ServiceBackupScopeError("The service configuration must be an object.")
    entries = []
    for key, section in config.items():
        if not isinstance(section, dict) or key in {"dumb", "dumb_frontend"}:
            continue
        instances = section.get("instances")
        candidates = (
            instances.items() if isinstance(instances, dict) else [(None, section)]
        )
        for instance_name, service in candidates:
            if not isinstance(service, dict) or not service.get("process_name"):
                continue
            identifier = _service_id(key, instance_name)
            name = service["process_name"]
            if not isinstance(name, str) or not name.strip():
                raise ServiceBackupScopeError(
                    "A service process identity is malformed."
                )
            entries.append(
                {
                    "id": identifier,
                    "key": key,
                    "instance_name": instance_name,
                    "name": name,
                    "enabled": service.get("enabled") is True,
                    "service_config": service,
                }
            )
    if len(entries) > MAX_SERVICES:
        raise ServiceBackupScopeError(
            "The configured service inventory exceeds its bound."
        )
    return entries


def _selected_entries(config, service_ids):
    from utils.installation_backup_lifecycle import auxiliary_services

    if (
        not isinstance(service_ids, (list, tuple))
        or not service_ids
        or len(service_ids) > MAX_SERVICES
        or any(not isinstance(identifier, str) for identifier in service_ids)
        or len(set(service_ids)) != len(service_ids)
    ):
        raise ServiceBackupScopeError(
            "Select one or more unique configured service IDs."
        )
    configured = {row["id"]: row for row in configured_service_entries(config)}
    process_owners = {}
    for key, service in service_entries(config):
        name = service.get("process_name")
        if isinstance(name, str) and name:
            process_owners.setdefault(name, []).append(key)
    # Reserve native helper names even before their parent services are enabled.
    auxiliary_names = {"pgAgent"}
    auxiliary_names.update(
        service["process_name"] for _key, service in auxiliary_services(config)
    )
    selected = []
    for identifier in service_ids:
        entry = configured.get(identifier)
        if entry is None:
            raise ServiceBackupScopeError(
                "A selected service no longer exists; refresh the selection."
            )
        if entry["key"] in PROTECTED_SERVICE_KEYS:
            raise ServiceBackupScopeError(
                "DUMB control services and the shared PostgreSQL cluster require a whole-installation backup."
            )
        if (
            entry["name"] in auxiliary_names
            or len(process_owners.get(entry["name"], [])) != 1
        ):
            raise ServiceBackupScopeError(
                "This service process name is shared or reserved; assign a unique application process name before using service backups."
            )
        selected.append(entry)
    return selected


def service_config_fragment(config, service_ids):
    """Copy selected sections/instances; never include global settings or auth."""
    fragment = {}
    for row in _selected_entries(config, service_ids):
        if row["instance_name"] is None:
            fragment[row["key"]] = deepcopy(row["service_config"])
        else:
            fragment.setdefault(row["key"], {"instances": {}})["instances"][
                row["instance_name"]
            ] = deepcopy(row["service_config"])
    return fragment


def _path(raw):
    if (
        not isinstance(raw, str)
        or not raw.startswith("/")
        or "\x00" in raw
        or ".." in Path(raw).parts
    ):
        raise ServiceBackupScopeError(
            "A service persistence selector is not an absolute direct path."
        )
    return Path(os.path.abspath(raw))


def _overlap(first, second, records=()):
    if first == second or first in second.parents or second in first.parents:
        return True
    if records:
        left = _filesystem_coordinate(first, records)
        right = _filesystem_coordinate(second, records)
        if left is not None and right is not None and left[0] == right[0]:
            if (
                left[1] == right[1]
                or left[1] in right[1].parents
                or right[1] in left[1].parents
            ):
                return True
    try:
        left, right = first.stat(), second.stat()
    except FileNotFoundError:
        return False
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _physical(path):
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        raise ServiceBackupScopeError(
            "A service persistence alias is cyclic or unreadable."
        ) from None


def _declared_roots(config, entry, config_parent):
    key, service = entry["key"], entry["service_config"]
    data_root = _path(config.get("data_root") or "/data")
    broad = {Path(value) for value in FORBIDDEN_ROOTS} | {
        Path("/config"),
        Path("/data"),
        Path("/cache"),
        Path("/log"),
        data_root,
        config_parent,
    }
    candidates = []
    broad_directories = []
    explicit_files = []
    for field in sorted(PERSISTENT_FIELDS | FILE_FIELDS):
        value = service.get(field)
        if value in (None, ""):
            continue
        # rclone references the provider's configuration but does not own it.
        if field == "zurg_config_file" and key == "rclone":
            continue
        path = _path(value)
        if path in broad and field in PERSISTENT_FIELDS:
            # The explicit file below may still be exclusive, e.g. rclone.config.
            # Never turn its shared parent into a service-owned directory.
            broad_directories.append(path)
            continue
        if field in FILE_FIELDS:
            explicit_files.append(path)
        candidates.append((path, f"{entry['id']}:{field}", False))
    if any(
        not any(parent in path.parents for path in explicit_files)
        for parent in broad_directories
    ):
        raise ServiceBackupScopeError(
            "This service uses a shared persistence directory without an exclusive file selector."
        )
    directories, databases = _effective_persistence(key, service, str(data_root))
    for value in directories:
        candidates.append((_path(value), f"{entry['id']}:effective_appdata", False))
    for value in databases:
        candidates.extend(
            (_path(value + suffix), f"{entry['id']}:effective_sqlite", False)
            for suffix in ("", "-wal", "-shm", "-journal")
        )
    persistent = _persistent_data_path(config, key, entry["instance_name"], service)
    if persistent:
        candidates.append((_path(persistent), f"{entry['id']}:managed_appdata", False))
    if key == "whisparr":
        profiles = service.get("whisparr_profiles") or {}
        if not isinstance(profiles, dict):
            raise ServiceBackupScopeError(
                "The saved Whisparr persistence selection is malformed."
            )
        for profile in profiles.values():
            if not isinstance(profile, dict):
                raise ServiceBackupScopeError(
                    "A saved Whisparr persistence selection is malformed."
                )
            if profile.get("initialized"):
                for field in ("config_dir", "config_file", "install_dir"):
                    if profile.get(field):
                        candidates.append(
                            (
                                _path(profile[field]),
                                f"{entry['id']}:saved_profile",
                                False,
                            )
                        )
    libraries = service.get("symlink_backup_roots") or []
    if not isinstance(libraries, list):
        raise ServiceBackupScopeError(
            "The service link-library selection is malformed."
        )
    candidates.extend(
        (_path(value), f"{entry['id']}:link_state", True) for value in libraries
    )
    for path, _role, _link in candidates:
        if path in broad or _physical(path) in broad:
            raise ServiceBackupScopeError(
                "A service persistence selector claims a shared system or media root."
            )
    # A broad declaration with no narrower owned selector cannot become an
    # apparently complete configuration-only archive.
    if not candidates and any(service.get(field) for field in PERSISTENT_FIELDS):
        raise ServiceBackupScopeError(
            "This service uses a shared persistence directory without an exclusive file selector."
        )
    return candidates


def _controller_paths(config_path, state_root):
    config = _path(str(config_path))
    parent = config.parent
    return [
        config,
        config.with_name(config.name + ".bak"),
        _path(os.environ.get("DUMB_AUTH_CONFIG_PATH") or "/config/users.json"),
        _path(str(state_root)),
        *(
            directory / name
            for directory in {parent, Path("/config")}
            for name in (
                ".env",
                "users.json",
                ".dumb-setup",
                ".dumb_branch_state",
                "backup-storage",
                "installation-backups",
                "migrations",
                "media-protection",
                "symlink-repair",
                "update_notices.json",
                "seerr_sync_state.json",
            )
        ),
    ]


def _restore_hints(restore_roots):
    if (
        not isinstance(restore_roots, (list, tuple))
        or len(restore_roots) > MAX_INSTALLATION_ROOTS
    ):
        raise ServiceBackupScopeError("The restore root hints are malformed.")
    result = {}
    for row in restore_roots:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("kind"), str)
            or row["kind"] not in {"file", "directory", "symlink"}
        ):
            raise ServiceBackupScopeError(
                "A restore root hint has an unsupported type."
            )
        path = _path(row.get("path"))
        if str(path) != row["path"] or str(path) in result:
            raise ServiceBackupScopeError(
                "The restore root hints are ambiguous or not direct paths."
            )
        result[str(path)] = row["kind"]
    return result


def resolve_service_scope(
    config, service_ids, config_path, state_root, storage=None, *, restore_roots=()
):
    """Resolve exclusive persistent roots and the private selected configuration.

    Missing roots remain declared for ownership checks, but are not archived.
    Restore-only hints can recover absent direct declared selectors; ownership,
    roles and service IDs always come from current configuration, never hints.
    Both literal paths and resolved aliases participate in collision detection.
    """
    from utils.backup_storage import mount_records

    records = mount_records()
    hints = _restore_hints(restore_roots)
    selected = _selected_entries(config, service_ids)
    identifiers = {row["id"] for row in selected}
    config_parent = _path(str(config_path)).parent
    declarations = {
        row["id"]: _declared_roots(config, row, config_parent)
        for row in configured_service_entries(config)
    }
    protected = _controller_paths(config_path, state_root)
    profiles = []
    for profile in (config.get("backup_storage") or {}).get("profiles", {}).values():
        if isinstance(profile, dict) and profile.get("path"):
            profiles.append(_path(profile["path"]))
    if storage is not None:
        destination = storage.get("path") if isinstance(storage, dict) else None
        if destination:
            profiles.append(_path(destination))
    roots = {}
    queue = [
        (path, role, link_only, entry["id"])
        for entry in selected
        for path, role, link_only in declarations[entry["id"]]
    ]
    while queue:
        path, role, link_only, owner = queue.pop(0)
        physical = _physical(path)
        variants = {path, physical}
        if any(
            _overlap(candidate, _physical(guard), records)
            for candidate in variants
            for guard in protected
        ):
            raise ServiceBackupScopeError(
                "A selected service overlaps DUMB configuration, authentication or recovery state."
            )
        if any(
            _overlap(candidate, _physical(profile), records)
            for candidate in variants
            for profile in profiles
        ):
            raise ServiceBackupScopeError(
                "A selected service overlaps a backup destination; choose a dedicated archive directory."
            )
        for other, paths in declarations.items():
            if other in identifiers:
                continue
            if any(
                _overlap(candidate, other_path, records)
                or _overlap(candidate, _physical(other_path), records)
                for candidate in variants
                for other_path, _other_role, _other_link in paths
            ):
                raise ServiceBackupScopeError(
                    f"Selected persistence is shared with unselected service {other}; include all owners or use a whole-installation backup.",
                    required_service_ids=[other],
                )
        if not os.path.lexists(path):
            kind = hints.get(str(physical))
            if kind is None:
                continue
            # Only a path independently derived from a current selector can
            # recover absent data. Hints cannot invent an old alias target.
            for ancestor in reversed(path.parents):
                if ancestor.is_symlink():
                    queue.append((ancestor, f"{owner}:managed_alias", True, owner))
            row = roots.setdefault(
                str(physical),
                {
                    "path": str(physical),
                    "identity": None,
                    "kind": kind,
                    "roles": [],
                    "link_only": link_only,
                    "service_ids": [],
                },
            )
            if role not in row["roles"]:
                row["roles"].append(role)
            if owner not in row["service_ids"]:
                row["service_ids"].append(owner)
            row["link_only"] = row["link_only"] and link_only
            continue
        # Include aliases in parent components as literal links, and archive
        # the effective path separately. The global /data parent is not owned.
        for ancestor in reversed(path.parents):
            if ancestor.is_symlink():
                queue.append((ancestor, f"{owner}:managed_alias", True, owner))
        parent = path.parent.resolve(strict=True)
        path = parent / path.name
        value = path.lstat()
        if not any(
            (
                stat.S_ISDIR(value.st_mode),
                stat.S_ISREG(value.st_mode),
                stat.S_ISLNK(value.st_mode),
            )
        ):
            raise ServiceBackupScopeError(
                "A service persistence root is not a regular file, directory or symbolic link."
            )
        row = roots.setdefault(
            str(path),
            {
                "path": str(path),
                "identity": identity(value),
                "kind": (
                    "directory"
                    if stat.S_ISDIR(value.st_mode)
                    else "symlink" if stat.S_ISLNK(value.st_mode) else "file"
                ),
                "roles": [],
                "link_only": link_only,
                "service_ids": [],
            },
        )
        if role not in row["roles"]:
            row["roles"].append(role)
        if owner not in row["service_ids"]:
            row["service_ids"].append(owner)
        row["link_only"] = row["link_only"] and link_only
        if stat.S_ISLNK(value.st_mode) and not link_only:
            try:
                target = path.resolve(strict=True)
            except (OSError, RuntimeError):
                raise ServiceBackupScopeError(
                    "A service persistence alias is cyclic, missing or unreadable."
                ) from None
            queue.append((target, role, False, owner))
    if any(path not in roots for path in hints):
        raise ServiceBackupScopeError(
            "The archive selects persistence that is not currently declared for the selected services."
        )
    selected_roots = []
    for path, row in sorted(
        roots.items(), key=lambda item: (len(Path(item[0]).parts), item[0])
    ):
        parent = next(
            (
                parent
                for parent in selected_roots
                if parent["kind"] == "directory"
                and Path(parent["path"]) in Path(path).parents
            ),
            None,
        )
        if parent is not None:
            parent["roles"] = sorted(set(parent["roles"]) | set(row["roles"]))
            parent["service_ids"] = sorted(
                set(parent["service_ids"]) | set(row["service_ids"])
            )
            continue
        row["id"] = f"root_{len(selected_roots):03d}"
        selected_roots.append(row)
    if len(selected_roots) > MAX_INSTALLATION_ROOTS:
        raise ServiceBackupScopeError(
            "The selected persistence inventory exceeds its bound."
        )
    for row in selected_roots:
        if row["kind"] == "directory" and any(
            Path(row["path"]) in Path(point["path"]).parents for point in records
        ):
            raise ServiceBackupScopeError(
                "A selected service directory contains a nested mount; separate media or delivery contents before backup."
            )
    return {
        "kind": "services",
        "service_ids": [row["id"] for row in selected],
        "services": [
            {key: value for key, value in row.items() if key != "service_config"}
            for row in selected
        ],
        "configuration": service_config_fragment(config, service_ids),
        "roots": selected_roots,
        "exclusions": [],
    }


def enumerate_service_choices(
    config,
    config_path="/config/dumb_config.json",
    state_root="/config/installation-backups",
):
    """List safe public identity/status metadata, never native settings or secrets."""
    choices = []
    for entry in configured_service_entries(config):
        choice = {key: value for key, value in entry.items() if key != "service_config"}
        selection = [entry["id"]]
        while True:
            try:
                resolve_service_scope(config, selection, config_path, state_root)
            except (InstallationArchiveError, OSError, ValueError) as error:
                required = getattr(error, "required_service_ids", ())
                additions = [
                    identifier for identifier in required if identifier not in selection
                ]
                if additions:
                    selection.extend(additions)
                    continue
                choice.update(
                    supported=False, reason=str(error), required_service_ids=[]
                )
            else:
                required = sorted(set(selection) - {entry["id"]})
                choice.update(
                    supported=True, reason=None, required_service_ids=required
                )
                if required:
                    choice["warning"] = (
                        "Shared application data requires selecting these services together."
                    )
            break
        choices.append(choice)
    return choices


def _shape(value, template, *, name="configuration"):
    if not isinstance(value, dict) or not isinstance(template, dict):
        raise ServiceBackupScopeError(
            "A service configuration fragment has an invalid shape."
        )
    if not template:
        return
    for key, child in value.items():
        if key not in template:
            raise ServiceBackupScopeError(
                f"The selected {name} contains an unsupported setting."
            )
        if key == "env" and isinstance(child, dict):
            continue
        reference = template[key]
        if isinstance(reference, dict):
            _shape(child, reference, name=name)
        elif reference is not None and not (
            type(child) is type(reference)
            or (type(reference) is float and type(child) in {int, float})
        ):
            raise ServiceBackupScopeError(
                "A service configuration setting has an invalid type."
            )


def merge_service_config(
    current, archived_config, service_ids, defaults=None, schema=None
):
    """Replace only exact selected configurations in a copy of current settings.

    Both inputs are immutable. A service absent from current config may be added
    only under an existing schema/default service family. Global settings and
    sibling instances always come from the current installation.
    """
    if not isinstance(current, dict) or not isinstance(archived_config, dict):
        raise ServiceBackupScopeError(
            "The service configuration fragment must be an object."
        )
    try:
        json.dumps(archived_config, allow_nan=False)
    except (TypeError, ValueError):
        raise ServiceBackupScopeError(
            "The restored service configuration contains unsupported values."
        ) from None
    selected = _selected_entries(archived_config, service_ids)
    expected = service_config_fragment(archived_config, service_ids)
    if expected != archived_config:
        raise ServiceBackupScopeError(
            "The archive configuration contains unselected service or global settings."
        )
    merged = deepcopy(current)
    for row in selected:
        key, instance_name, service = (
            row["key"],
            row["instance_name"],
            row["service_config"],
        )
        reference = (defaults if defaults is not None else current).get(key)
        if not isinstance(reference, dict):
            raise ServiceBackupScopeError(
                "A selected service has no supported current configuration template."
            )
        if instance_name is None:
            if isinstance(reference.get("instances"), dict):
                raise ServiceBackupScopeError(
                    "The archived service family changed its instance layout."
                )
            _shape(service, reference)
            merged[key] = deepcopy(service)
        else:
            templates = reference.get("instances")
            if not isinstance(templates, dict) or not templates:
                raise ServiceBackupScopeError(
                    "A selected instance has no supported current configuration template."
                )
            _shape(service, next(iter(templates.values())))
            family = merged.setdefault(key, {"instances": {}})
            if not isinstance(family, dict) or not isinstance(
                family.get("instances"), dict
            ):
                raise ServiceBackupScopeError(
                    "The current service family changed its instance layout."
                )
            family["instances"][instance_name] = deepcopy(service)
    names = [row["name"] for row in configured_service_entries(merged)]
    if len(names) != len(set(names)):
        raise ServiceBackupScopeError(
            "The restored service process name conflicts with another configured service."
        )
    _selected_entries(merged, service_ids)
    if schema is not None:
        try:
            validate(instance=merged, schema=schema)
        except ValidationError:
            # Validation messages can contain native passwords or tokens.
            raise ServiceBackupScopeError(
                "The restored service configuration does not satisfy the current DUMB schema."
            ) from None
    # Reject non-JSON/NaN native objects even for a schema with open settings.
    try:
        json.dumps(merged, allow_nan=False)
    except (TypeError, ValueError):
        raise ServiceBackupScopeError(
            "The restored service configuration contains unsupported values."
        ) from None
    return merged


def affected_running_rows(config, service_ids, running_rows):
    """Select the exact running writers and their transitive managed consumers.

    No prerequisites are stopped merely because a service needs them: a logical
    database backup keeps its shared PostgreSQL provider online. Family-level
    dependencies can conservatively pause consumers in another family, but never
    turn selection of one instance into selection of all its sibling instances.
    """
    from utils.dependency_map import build_conditional_dependency_map
    from utils.installation_backup_lifecycle import resume_wait_dependencies

    selected = _selected_entries(config, service_ids)
    running_names = {row["name"] for row in running_rows}
    # Stopped selected providers still identify a dependency boundary. A live
    # consumer can otherwise keep reading/writing their state during replacement.
    described = [
        *running_rows,
        *(
            {"key": row["key"], "name": row["name"]}
            for row in selected
            if row["name"] not in running_names
        ),
    ]
    waits = resume_wait_dependencies(described, config)
    dependencies = build_conditional_dependency_map(lambda key: config.get(key) or {})
    affected = {row["name"] for row in selected}
    by_name = {row["name"]: row for row in described}
    while True:
        keys = {by_name[name]["key"] for name in affected}
        consumers = {
            name
            for name, row in by_name.items()
            if name not in affected
            and (waits[name] & affected or set(dependencies.get(row["key"], ())) & keys)
        }
        if not consumers:
            return [deepcopy(row) for row in running_rows if row["name"] in affected]
        affected.update(consumers)
