"""Versioned admission policy for native InfiniDysk support.

This module has no configuration singleton imports: validation must also work
for candidate configuration and metadata-only API clients.
"""

from pathlib import Path
import re

API_MINIMUM = "2.24.0"
MIGRATION_PROTOCOL = 1
V2_RELEASE = "v2.0.0-beta.6"
_STABLE_VERSION = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def api_version_supported(version):
    if not isinstance(version, str):
        return False
    match = _STABLE_VERSION.fullmatch(version)
    if not match:
        return False
    numbers = tuple(map(int, match.groups()[:3]))
    return numbers > (2, 24, 0) or (numbers == (2, 24, 0) and match[4] is None)


def versioned_capabilities(version):
    supported = api_version_supported(version)
    return {
        "api_version": str(version),
        "infinidysk_v2": supported,
        "infinidysk_v2_migration": supported,
        "infinidysk_v2_migration_protocol": MIGRATION_PROTOCOL,
        "infinidysk_v2_postgres": supported,
        "infinidysk_v2_low_space_migration": supported,
        "infinidysk_v2_reviewed_skips": supported,
        "infinidysk_v2_rclone_optimizer": False,
        "infinidysk_v2_minimum_api_version": API_MINIMUM,
        "infinidysk_v2_mount_backends": ["rclone", "fuse"],
    }


def generation(service):
    value = str((service or {}).get("runtime_generation") or "1")
    if value not in {"1", "2"}:
        raise ValueError("InfiniDysk runtime generation must be 1 or 2.")
    return value


def v1_data_root(service):
    service = service or {}
    return Path(
        (service.get("env") or {}).get("CONFIG_PATH")
        or service.get("config_dir")
        or "/infinidysk"
    )


def has_v1_data(service):
    if (service or {}).get("postgres_enabled"):
        return True
    env = (service or {}).get("env") or {}
    if str(env.get("DATABASE_PROVIDER") or "").lower() in {"postgres", "postgresql"}:
        return True
    root = v1_data_root(service)
    # Existence, rather than size, protects incomplete/empty-but-owned databases.
    return any(
        (root / name).exists() or (root / name).is_symlink()
        for name in ("db.sqlite", "db.sqlite-wal", "db-contract.json")
    )


def validate_candidate(current, candidate, *, data_root="/data"):
    """Reject a raw provider/generation/mount change that needs a guided job.

    Trusted migration managers publish configuration only after retaining their
    own recovery state; they do not route that publication through this API edit
    validator. Ordinary config edits can never supply an authorization receipt.
    """
    current, candidate = current or {}, candidate or {}
    old, new = generation(current), generation(candidate)
    backend = str(candidate.get("mount_backend") or "rclone")
    if backend not in {"rclone", "fuse"}:
        return False, "Choose rclone or native FUSE for the InfiniDysk mount."
    if new == "1" and backend != "rclone":
        return False, "Native FUSE requires the guided InfiniDysk 2.x workflow."
    if old == "1" and new == "2" and has_v1_data(current):
        return (
            False,
            "Use the guided InfiniDysk 1.x to 2.x migration to preserve existing data.",
        )
    if old == "2" and new != old:
        return (
            False,
            "Use the retained major-migration rollback; a config edit cannot downgrade the database.",
        )
    if new == "2":
        if candidate.get("postgres_enabled"):
            return (
                False,
                "Use InfiniDysk 2.x's native PostgreSQL workflow instead of the 1.x provider toggle.",
            )
        for name in ("DATABASE_PROVIDER", "DATABASE_CONNECTION_STRING"):
            if (candidate.get("env") or {}).get(name):
                return (
                    False,
                    "Legacy database variables are reserved for the guarded major importer.",
                )
        if str(candidate.get("v2_release_version") or V2_RELEASE) != V2_RELEASE:
            return (
                False,
                "This integration supports the verified InfiniDysk v2.0.0-beta.6 artifact.",
            )
        for name in ("v2_runtime_dir", "v2_data_dir", "mount_path"):
            value = str(candidate.get(name) or "")
            if value and (not Path(value).is_absolute() or ".." in Path(value).parts):
                return (
                    False,
                    "InfiniDysk native paths must be absolute without parent traversal.",
                )
        target = Path(candidate.get("v2_data_dir") or Path(data_root) / "infinidysk-v2")
        if target.resolve() == v1_data_root(current).resolve():
            return (
                False,
                "InfiniDysk 2.x data must remain separate from the retained 1.x data.",
            )
        if old == "2":
            guarded = ("mount_backend", "mount_path", "v2_data_dir", "v2_runtime_dir")
            if any(current.get(name) != candidate.get(name) for name in guarded):
                return (
                    False,
                    "Use a guided, owner-checked transition before changing native runtime/data/mount paths.",
                )
            previous_env, next_env = (
                current.get("env") or {},
                candidate.get("env") or {},
            )
            guarded_env = (
                "INFINIDYSK_CONFIG_DIR",
                "INFINIDYSK_DATABASE_URL",
                "INFINIDYSK__MOUNT__FUSE",
                "INFINIDYSK__MOUNT__DIR",
            )
            if any(
                previous_env.get(name) != next_env.get(name) for name in guarded_env
            ):
                return (
                    False,
                    "Native database and mount ownership cannot be changed through raw environment edits.",
                )
    return True, None
