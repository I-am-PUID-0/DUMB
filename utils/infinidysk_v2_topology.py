"""Selected InfiniDysk delivery ownership, independent of its database provider."""

import os
from pathlib import Path
from utils.core_services import has_core_service


def is_v2(service):
    return (
        isinstance(service, dict) and str(service.get("runtime_generation", "1")) == "2"
    )


def associated_rclone(instance):
    return isinstance(instance, dict) and (
        str(instance.get("key_type") or "").lower() == "infinidysk"
        or has_core_service(instance, "infinidysk")
    )


def suppress_rclone(service, instance):
    return (
        is_v2(service)
        and service.get("mount_backend", "rclone") == "fuse"
        and associated_rclone(instance)
    )


def rclone_mount_path(instance):
    if (
        not isinstance(instance, dict)
        or not instance.get("mount_dir")
        or not instance.get("mount_name")
    ):
        return None
    return os.path.normpath(os.path.join(instance["mount_dir"], instance["mount_name"]))


def selected_topology(service, instances=None):
    """Inventory configuration only; readiness still requires kernel/consumer probes."""
    owners = [
        (name, item)
        for name, item in (instances or {}).items()
        if isinstance(item, dict) and item.get("enabled") and associated_rclone(item)
    ]
    if is_v2(service) and service.get("mount_backend", "rclone") == "fuse":
        return [
            {
                "backend": "fuse",
                "process_name": service.get("process_name", "InfiniDysk"),
                "path": os.path.normpath(
                    service.get("mount_path") or "/mnt/debrid/infinidysk"
                ),
            }
        ]
    return [
        {
            "backend": "rclone",
            "instance": name,
            "process_name": item.get("process_name"),
            "path": rclone_mount_path(item),
        }
        for name, item in owners
        if rclone_mount_path(item)
    ]


def mount_records(path="/proc/self/mountinfo"):
    def decode(value):
        for escaped, plain in (
            ("\\040", " "),
            ("\\011", "\t"),
            ("\\012", "\n"),
            ("\\134", "\\"),
        ):
            value = value.replace(escaped, plain)
        return value

    records = []
    for line in Path(path).read_text().splitlines():
        before, separator, after = line.partition(" - ")
        fields, filesystem = before.split(), after.split()
        if not separator or len(fields) < 6 or len(filesystem) < 2:
            raise ValueError("Cannot establish mount ownership from mountinfo")
        records.append(
            {
                "path": decode(fields[4]),
                "filesystem": filesystem[0],
                "source": decode(filesystem[1]),
                "shared": any(x.startswith("shared:") for x in fields[6:]),
            }
        )
    return records


def mount_path_released(path, records=None):
    """Confirm no mount remains at or below a selected delivery path.

    This reads only mountinfo; it never stats a dead FUSE filesystem or detaches
    an owner. An unreadable namespace is not proof that the path is reusable.
    """
    try:
        target = Path(path)
        if not target.is_absolute() or target == Path("/") or ".." in target.parts:
            return False
        return all(
            Path(record["path"]) != target
            and target not in Path(record["path"]).parents
            for record in (mount_records() if records is None else records)
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def validate_native_mount_target(path, records=None):
    """Never invoke upstream's unconditional detach on an existing/redirected mount."""
    try:
        target = Path(path)
        if not target.is_absolute() or target == Path("/") or ".." in target.parts:
            raise ValueError(
                "Native mount path must be an absolute dedicated directory"
            )
        for parent in (target, *target.parents):
            if parent.is_symlink():
                raise ValueError("Native mount path crosses a symbolic link")
        for record in mount_records() if records is None else records:
            mounted = Path(record["path"])
            if mounted == target or target in mounted.parents:
                raise ValueError(
                    "Native mount target is occupied; stop and unmount its verified owner first"
                )
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ValueError("Native mount target must be an empty directory")
        return True, None
    except (OSError, ValueError) as error:
        return False, str(error)


def validate_rclone_topology(service, instances):
    owners = selected_topology(service, instances)
    if len(owners) != 1:
        return (
            False,
            "InfiniDysk 2.x requires exactly one enabled associated rclone mount",
        )
    desired = os.path.normpath(service.get("mount_path") or "/mnt/debrid/infinidysk")
    if owners[0]["path"] != desired:
        return (
            False,
            "InfiniDysk mount_path must match its existing Arr-visible rclone mount",
        )
    valid, error = validate_selected_owners(service, instances)
    if not valid:
        return False, error
    return True, None


def validate_selected_owners(service, instances):
    """Reject ambiguous configured owners even before either process has mounted."""
    desired = os.path.normpath(service.get("mount_path") or "/mnt/debrid/infinidysk")
    associated = []
    for item in (instances or {}).values():
        if not isinstance(item, dict) or not item.get("enabled"):
            continue
        path = rclone_mount_path(item)
        if associated_rclone(item):
            associated.append(item)
            if path != desired:
                return False, "Associated rclone and native mount paths disagree"
        elif path == desired:
            return False, "An unrelated rclone instance owns the selected mount path"
    if len(associated) > 1:
        return (
            False,
            "Multiple associated rclone instances make mount ownership ambiguous",
        )
    return True, None


def delivery_mount_ready(service, instances, records=None):
    """Verify the selected owner in this mount namespace, without reading media."""
    owners = selected_topology(service, instances)
    if len(owners) != 1:
        return False
    owner = owners[0]
    try:
        visible = [
            item
            for item in (mount_records() if records is None else records)
            if os.path.normpath(item["path"]) == owner["path"]
        ]
    except (OSError, ValueError):
        return False
    if len(visible) != 1:
        return False
    mounted = visible[0]
    if owner["backend"] == "fuse":
        return (
            mounted["filesystem"].startswith("fuse")
            and mounted["source"] == "infinidysk"
        )
    instance = (instances or {}).get(owner["instance"], {})
    return mounted["filesystem"] == "fuse.rclone" and mounted["source"] in {
        instance.get("mount_name"),
        f"{instance.get('mount_name')}:",
    }
