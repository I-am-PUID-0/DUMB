"""Versioned setup topology and truthful native-2 handoffs; no mutations."""

from __future__ import annotations

from utils.infinidysk_v2_support import generation, has_v1_data, validate_candidate
from utils.infinidysk_v2_topology import associated_rclone, rclone_mount_path

CHOICE_KEYS = ("runtime_generation", "mount_backend")
PROTECTED = (
    "runtime_generation",
    "mount_backend",
    "mount_path",
    "v2_runtime_dir",
    "v2_data_dir",
    "v2_release_version",
)
NATIVE_SOURCE_FIELDS = (
    "branch_enabled",
    "branch",
    "commit_sha",
    "release_version_enabled",
    "release_version",
    "repo_owner",
    "repo_name",
    "command",
    "config_dir",
)
NATIVE_ENV = (
    "INFINIDYSK_CONFIG_DIR",
    "INFINIDYSK_DATABASE_URL",
    "INFINIDYSK__MOUNT__FUSE",
    "INFINIDYSK__MOUNT__DIR",
)
NATIVE_SETTINGS_REASON = (
    "InfiniDysk 2 uses its native settings, administrator/SAB credentials and API. "
    "Configure NNTP, Arr clients, WebDAV and playback integrations in its native UI; "
    "the legacy setup adapter is not applied."
)


def project_choice(current, target, options, values, config):
    previous = generation(current)
    for key in PROTECTED:
        if key not in options:
            continue
        before, after = current.get(key), options[key]
        if key == "runtime_generation":
            before, after = previous, str(after)
        elif key == "mount_backend":
            before = before or "rclone"
        if before != after:
            raise ValueError(
                "Use the advertised InfiniDysk runtime choices and guarded wizard instead of raw service options."
            )
    if previous == "2" and any(
        key in options and options[key] != current.get(key)
        for key in NATIVE_SOURCE_FIELDS
    ):
        raise ValueError(
            "Preserve the pinned native runtime and retained source identity; legacy source selectors are not native setup options."
        )
    environment = options.get("env") or {}
    if not isinstance(environment, dict):
        raise ValueError("InfiniDysk environment options must be an object.")
    if any(
        key in environment and environment[key] != (current.get("env") or {}).get(key)
        for key in NATIVE_ENV
    ):
        raise ValueError(
            "Native database and mount ownership require their guarded workflow."
        )
    selected = str(values.get("infinidysk::runtime_generation", previous))
    backend = str(
        values.get(
            "infinidysk::mount_backend", current.get("mount_backend") or "rclone"
        )
    )
    if selected not in {"1", "2"} or backend not in {"rclone", "fuse"}:
        raise ValueError(
            "Choose an advertised InfiniDysk generation and mount backend."
        )
    if selected == "1":
        backend = "rclone"
    if previous == "2" and (
        selected != previous or backend != (current.get("mount_backend") or "rclone")
    ):
        raise ValueError(
            "Preserve the current native topology; use a guarded transition to change it."
        )
    target.update(runtime_generation=selected, mount_backend=backend)
    if previous == "1":
        owners = [
            item
            for item in ((config.get("rclone") or {}).get("instances") or {}).values()
            if isinstance(item, dict)
            and item.get("enabled")
            and associated_rclone(item)
        ]
        if len(owners) == 1 and rclone_mount_path(owners[0]):
            target["mount_path"] = rclone_mount_path(owners[0])
    if previous == selected:
        valid, error = validate_candidate(
            current, target, data_root=config.get("data_root") or "/data"
        )
        if not valid:
            raise ValueError(error)


def transition(current, proposed):
    if generation(current) == "1" and generation(proposed) == "2":
        return {
            "runtime_generation": "2",
            "mode": "migrate" if has_v1_data(current) else "fresh",
            "mount_backend": proposed.get("mount_backend") or "rclone",
        }
    return None


def choice_fields(resource, current, proposed):
    locked = generation(current) == "2"
    fields = []
    for name, label, choices, default in (
        (
            "runtime_generation",
            "InfiniDysk runtime generation",
            [("1", "InfiniDysk 1"), ("2", "InfiniDysk 2 beta")],
            generation(proposed),
        ),
        (
            "mount_backend",
            "InfiniDysk mount backend",
            [("rclone", "rclone / WebDAV"), ("fuse", "Native FUSE (generation 2)")],
            proposed.get("mount_backend") or "rclone",
        ),
    ):
        fields.append(
            {
                "key": resource["id"] + "::" + name,
                "resource_id": resource["id"],
                "service_key": "infinidysk",
                "label": label,
                "type": "select",
                "required": False,
                "readonly": locked,
                "refresh_on_change": True,
                "default": default,
                "options": [
                    {"value": value, "label": title}
                    for value, title in choices
                    if not locked or value == default
                ],
                "description": (
                    "The existing native topology is preserved. Database and mount transitions use their guarded workflow."
                    if locked
                    else "Generation 2 installation or migration opens the guarded native wizard before setup can continue."
                ),
            }
        )
    return fields


def topology(config, dependencies):
    return {
        "runtime_generation": generation(config),
        "mount_backend": config.get("mount_backend") or "rclone",
        "mount_path": config.get("mount_path") or "/mnt/debrid/infinidysk",
        "dependencies": list(dependencies),
    }


def uses_legacy_adapter(action, config, payload):
    if generation(config.get("infinidysk") or {}) != "2":
        return False
    name = str(action.get("action") or "")
    if action.get("service_key") == "infinidysk":
        return name not in {"runtime", "consumer_paths", "native_transition"}
    target = str(action.get("target") or "")
    if target == "infinidysk" or target.startswith("infinidysk."):
        return True
    values = (payload.get("arrangement") or {}).get("values") or {}
    prefix = str(action.get("source") or action.get("service_key") or "") + "::"
    for key, value in values.items():
        if not str(key).startswith(prefix):
            continue
        field = str(key).removeprefix(prefix)
        if (
            field
            in {"streaming_engine", "usenet_engine", "account_source", "nntp_source"}
            and value == "infinidysk"
        ):
            return True
        if (
            field in {"workflow_test", "workflow_probe"}
            and isinstance(value, dict)
            and value.get("provider") == "infinidysk"
        ):
            return True
    return False


def manual_outcome(reason=NATIVE_SETTINGS_REASON):
    return {
        "automation": "manual",
        "declared_automation": "manual",
        "execution": "waiting_input",
        "verification": "not_checked",
        "reason": reason,
        "retryable": False,
    }


def adapt_plan(record, current, proposed, payload):
    for action in record["actions"]:
        if uses_legacy_adapter(action, proposed, payload):
            action.update(manual_outcome(), capability_reason=NATIVE_SETTINGS_REASON)
            action["missing_inputs"] = []
            action["input_keys"] = []
            action.pop("previewable", None)
    native = proposed.get("infinidysk") or {}
    handoff = transition(current.get("infinidysk") or {}, native)
    if handoff and any(
        resource["service_key"] == "infinidysk" for resource in record["services"]
    ):
        record["checks"].append(
            {
                "key": "infinidysk_native_transition",
                "label": "Guarded InfiniDysk 2 workflow",
                "ok": False,
                "critical": True,
                "detail": "Complete the guarded native installation or migration, then create a fresh setup plan. Ordinary setup will not enable the legacy service or its mount.",
            }
        )
    record["features"]["infinidysk_runtime_choices"] = True
