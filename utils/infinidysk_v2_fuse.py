"""Read-only FUSE prerequisites and bounded public runtime preparation reasons."""

from pathlib import Path
import shutil

FUSE_DEVICE_REQUIRED = "Native FUSE requires /dev/fuse to be passed into the container"
FUSE_DEVICE_UNAVAILABLE = (
    "Native FUSE could not inspect /dev/fuse; verify the device mapping and permissions"
)
FUSE_HELPER_REQUIRED = "Native FUSE requires fusermount3 in the container image"
FUSE_POLICY_REQUIRED = (
    "Native FUSE with an unprivileged owner requires user_allow_other in /etc/fuse.conf"
)
FUSE_POLICY_UNREADABLE = "Native FUSE with an unprivileged owner requires readable /etc/fuse.conf with user_allow_other"
FUSE_UID_REQUIRED = "Native FUSE requires a valid non-negative process UID"


_PUBLIC_PREPARATION_REASONS = frozenset(
    {
        FUSE_DEVICE_REQUIRED,
        FUSE_DEVICE_UNAVAILABLE,
        FUSE_HELPER_REQUIRED,
        FUSE_POLICY_REQUIRED,
        FUSE_POLICY_UNREADABLE,
        FUSE_UID_REQUIRED,
        "Native FUSE requires /dev/fuse and fusermount3",
        "Native mount path must be an absolute dedicated directory",
        "Native mount path crosses a symbolic link",
        "Native mount target is occupied; stop and unmount its verified owner first",
        "Native mount target must be an empty directory",
        "Associated rclone and native mount paths disagree",
        "An unrelated rclone instance owns the selected mount path",
        "Multiple associated rclone instances make mount ownership ambiguous",
        "InfiniDysk 2.x requires exactly one enabled associated rclone mount",
        "InfiniDysk mount_path must match its existing Arr-visible rclone mount",
        "Native runtime is missing or its pinned provenance changed",
        "Native runtime/data paths must be absolute dedicated directories",
        "Native runtime/data paths cannot cross symbolic links",
        "Native runtime/data path is not a directory",
        "Native program, native data and retained 1.x directories must be separate",
        "Use a dedicated native program/data subdirectory",
        "Native data requires a dedicated owned subdirectory",
        "Refusing foreign nonempty native data; use the guarded major migration for a retained 1.x copy",
        "A 1.x import requires the guarded contained candidate executor",
        "Native private credentials must be a bounded mode-0600 regular file",
        "Native controller credentials are invalid",
        "Native controller credentials have not been prepared",
    }
)


def validate_fuse_prerequisites(uid):
    """Inspect fixed image/device policy without changing it or mount state."""
    if type(uid) is not int or uid < 0:
        return False, FUSE_UID_REQUIRED
    try:
        if not Path("/dev/fuse").exists():
            return False, FUSE_DEVICE_REQUIRED
    except OSError:
        return False, FUSE_DEVICE_UNAVAILABLE
    if not shutil.which("fusermount3"):
        return False, FUSE_HELPER_REQUIRED
    if uid != 0:
        try:
            lines = Path("/etc/fuse.conf").read_text().splitlines()
        except (OSError, UnicodeError):
            return False, FUSE_POLICY_UNREADABLE
        if not any(
            line.split("#", 1)[0].strip() == "user_allow_other" for line in lines
        ):
            return False, FUSE_POLICY_REQUIRED
    return True, None


def safe_native_preparation_error(reason):
    """Expose authored causes, never arbitrary filesystem/configuration values."""
    if isinstance(reason, str):
        if reason in _PUBLIC_PREPARATION_REASONS:
            return reason
        lowered = reason.lower()
        if "permission denied" in lowered or "operation not permitted" in lowered:
            return "Native runtime or mount preparation lacks required filesystem permissions"
        if "no such file or directory" in lowered:
            return (
                "A required native runtime, state or mount-parent path is unavailable"
            )
    return "Native runtime preparation failed for an unrecognized reason; review retained private recovery evidence"
