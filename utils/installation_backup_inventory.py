"""Installation coverage and capacity without importing the live controller."""

from contextlib import closing
import os
import json
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import hashlib

from utils.backup_storage import mount_records, paths_overlap, require_local_path
from utils.installation_backup_archive import (
    InstallationArchiveError,
    identity,
    source_rows,
    manifest,
    _insert,
    ledger_footprint,
    ledger_quota,
    footer_record_bytes,
    archive_entry_bytes,
)
from utils.service_reset import PERSISTENT_DATA_NAMES
from utils.installation_backup_limits import (
    MAX_INSTALLATION_ROOTS,
    MIN_PAIRED_WORKSPACE_BYTES,
    ARCHIVE_FIXED_OVERHEAD_BYTES,
    paired_workspace_bytes,
    safe_ledger_budget,
    installation_limits,
)
from urllib.parse import unquote

PERSISTENT_FIELDS = {
    "config_dir",
    "install_dir",
    "runtime_dir",
    "metadata_dir",
    "v2_data_dir",
    "v2_runtime_dir",
    "data_dir",
}
FILE_FIELDS = {"config_file", "zurg_config_file", "settings_file"}
FORBIDDEN_ROOTS = {
    "/",
    "/usr",
    "/opt",
    "/etc",
    "/var",
    "/mnt",
    "/mnt/debrid",
    "/dumb",
    "/utils",
}
CONTROL_KEYS = {"dumb", "dumb_frontend"}
EXCLUSIONS = [
    {"reason": "External services and databases are not copied."},
    {"reason": "Media/download payloads and mounted delivery contents are excluded."},
    {
        "reason": "Container image, logs and disposable cache are not a substitute for image recovery."
    },
    {
        "reason": "Existing closed backup archives and this operation's control state are retained separately, not recursively embedded."
    },
]


def service_entries(config):
    for key, section in config.items():
        if not isinstance(section, dict):
            continue
        if key == "dumb":
            for child in ("api_service", "frontend"):
                value = section.get(child) or {}
                if value.get("process_name"):
                    yield "dumb" if child == "api_service" else "dumb_frontend", value
        elif isinstance(section.get("instances"), dict):
            for value in section["instances"].values():
                if isinstance(value, dict):
                    yield key, value
        elif section.get("process_name"):
            yield key, section


def compatibility(config):
    postgres = config.get("postgres") or {}
    major = None
    root = Path(str(postgres.get("config_dir") or "/postgres_data"))
    if (root / "PG_VERSION").is_file():
        major = (root / "PG_VERSION").read_text().strip()
    if major is None:
        tools = sorted(Path("/usr/lib/postgresql").glob("*/bin/postgres"))
        if tools:
            major = tools[-1].parent.parent.name
    code_root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for item in sorted(
        [
            code_root / "main.py",
            code_root / "pyproject.toml",
            *code_root.glob("api/**/*.py"),
            *code_root.glob("utils/**/*.py"),
        ]
    ):
        if item.is_file():
            digest.update(
                str(item.relative_to(code_root)).encode() + b"\0" + item.read_bytes()
            )
    return {
        "controller_fingerprint": digest.hexdigest(),
        "architecture": platform.machine(),
        "system": platform.system(),
        "dumb_version": os.environ.get("DUMB_VERSION") or _code_version(),
        "python_runtime": ".".join(platform.python_version().split(".")[:2]),
        "postgres_major": major,
        "puid": config.get("puid"),
        "pgid": config.get("pgid"),
    }


def _code_version():
    from utils.project_metadata import get_project_version
    from utils.runtime_paths import pyproject_file

    return get_project_version(pyproject_file())


def _plex_dbrepair_directories(service):
    """Plex owns DBRepair's native paths even when scheduled repair is disabled."""
    settings = service.get("dbrepair")
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise InstallationArchiveError(
            "Plex DBRepair settings must be an object before complete backup."
        )
    directories = []
    # Match setup.py and plex_dbrepair.py, including the literal install default.
    # work_dir is only a subprocess cwd, not ownership of all its contents.
    for field, default in (
        ("install_dir", "/data/dbrepair"),
        ("databases_path", "/plex/Plex Media Server/Plug-in Support/Databases"),
    ):
        value = settings.get(field, default)
        if (
            not isinstance(value, str)
            or not os.path.isabs(value)
            or "\x00" in value
            or ".." in Path(value).parts
        ):
            raise InstallationArchiveError(
                f"Plex DBRepair {field} must use an absolute direct path before complete backup."
            )
        directories.append(value)
    return directories


def _unclassified_message(path, kind):
    shown = json.dumps(str(path), ensure_ascii=True)
    if len(shown) > 900:
        shown = shown[:900] + "... [path truncated]"
    return (
        f"Unclassified state exists under data_root at {shown} ({kind}). "
        "DUMB cannot identify this path as managed application state; a complete "
        "installation backup is blocked. Review this path and its owning service "
        "configuration before retrying."
    )


def _effective_persistence(key, service, data_root):
    """Reuse known native selectors, never crawl arbitrary environment values."""
    from utils.database_health import DatabaseHealthCollector, SQLITE_SERVICE_KEYS

    env = service.get("env") or {}
    directories = []
    databases = []
    if key == "plex":
        directories.extend(_plex_dbrepair_directories(service))
    if key in {"cli_debrid", "cli_battery"}:
        directories.extend(
            env[x] for x in ("USER_CONFIG", "USER_DB_CONTENT") if env.get(x)
        )
    if key == "maintainerr" and env.get("DATA_DIR"):
        directories.append(env["DATA_DIR"])
    if key == "seerr" and env.get("CONFIG_DIRECTORY"):
        directories.append(env["CONFIG_DIRECTORY"])
    if key == "profilarr":
        directories.extend(
            env[x] for x in ("APP_BASE_PATH", "PROFILARR_CONFIG_DIR") if env.get(x)
        )
    if key == "bazarr":
        command = service.get("command") or []
        if not isinstance(command, list):
            raise InstallationArchiveError(
                "A custom Bazarr command has no auditable persistence contract."
            )
        for index, argument in enumerate(command):
            if argument == "--config":
                if index + 1 >= len(command):
                    raise InstallationArchiveError(
                        "Bazarr's effective config directory is incomplete."
                    )
                directories.append(command[index + 1])
            elif isinstance(argument, str) and argument.startswith("--config="):
                directories.append(argument.split("=", 1)[1])
    if key == "infinidysk" and str(service.get("runtime_generation") or "1") == "2":
        from utils.infinidysk_v2_runtime import native_database_url

        url = native_database_url(service, data_root)
        if url.startswith("sqlite:"):
            path = unquote(url.removeprefix("sqlite:").split("?", 1)[0])
            # Native SQLx accepts absolute sqlite:/path URLs.
            if not os.path.isabs(path):
                raise InstallationArchiveError(
                    "Native SQLite uses an unqualified relative persistence path."
                )
            databases.append(path)
    elif key in SQLITE_SERVICE_KEYS and key != "bazarr":
        if key == "aiostreams":
            selected = str(
                service.get("database_uri")
                or env.get("DATABASE_URI")
                or "sqlite://./data/db.sqlite"
            )
            if not selected.startswith("sqlite:"):
                if selected.startswith("sqlite"):
                    raise InstallationArchiveError(
                        "This SQLite dialect has no qualified installation persistence contract."
                    )
                return directories, databases
            _scheme, separator, raw = selected.partition("://")
            if not separator or ":memory:" in raw:
                raise InstallationArchiveError(
                    "AIOStreams uses unsupported ephemeral SQLite persistence."
                )
            path = unquote(raw.split("?", 1)[0])
            if not os.path.isabs(path):
                path = os.path.normpath(
                    os.path.join(str(service.get("config_dir") or ""), path)
                )
            databases.append(path)
            return directories, databases
        databases.extend(
            path
            for _name, path in DatabaseHealthCollector._sqlite_paths(
                None, {"config_key": key, "service_config": service}
            )
        )
    for path in directories + databases:
        if not isinstance(path, str) or not os.path.isabs(path):
            raise InstallationArchiveError(
                "A known native persistence selector is not an absolute path."
            )
    return directories, databases


def collect_roots(config, config_path, state_root, storage):
    """All configured persistence, not only currently enabled services."""
    candidates = [(str(Path(config_path).parent), "configuration", False)]
    data_root = str(config.get("data_root") or "/data")
    for name in set(PERSISTENT_DATA_NAMES.values()) | {
        "readarr",
        "nzbdav",
        "riven",
        "infinidysk-v2",
    }:
        candidates.append((str(Path(data_root) / name), "managed_appdata", False))
    for key, service in service_entries(config):
        if key in {"dumb", "dumb_frontend"}:
            continue
        for field in PERSISTENT_FIELDS | FILE_FIELDS:
            value = service.get(field)
            if isinstance(value, str) and value.startswith("/"):
                candidates.append((value, f"{key}:{field}", False))
        directories, databases = _effective_persistence(key, service, data_root)
        candidates.extend(
            (path, f"{key}:effective_appdata", False) for path in directories
        )
        for path in databases:
            candidates.extend(
                (path + suffix, f"{key}:effective_sqlite", False)
                for suffix in ("", "-wal", "-shm", "-journal")
            )
        for value in service.get("symlink_backup_roots") or []:
            if isinstance(value, str) and value.startswith("/"):
                candidates.append((value, "link_state", True))
    auth = os.environ.get("DUMB_AUTH_CONFIG_PATH") or "/config/users.json"
    if os.path.lexists(auth):
        candidates.append((auth, "authentication", False))
    exclusions = [str(Path(state_root))]
    # Closed artifacts may be on a share or within an included local tree.
    for profile in (config.get("backup_storage") or {}).get("profiles", {}).values():
        if isinstance(profile, dict) and isinstance(profile.get("path"), str):
            profile_path = Path(profile["path"])
            if profile_path in {Path(config_path).parent, Path(data_root)}:
                raise InstallationArchiveError(
                    "A backup destination cannot be the configuration or managed appdata root."
                )
            exclusions.append(str(profile_path))
    roots = {}
    while candidates:
        raw, role, link_only = candidates.pop(0)
        path = Path(os.path.abspath(raw))
        if str(path) in FORBIDDEN_ROOTS:
            raise InstallationArchiveError(
                "A managed persistence path selects a protected system or media root."
            )
        if not os.path.lexists(path):
            continue
        # Preserve each published alias as link state, while reading its managed physical target separately.
        for ancestor in reversed(path.parents):
            if ancestor.is_symlink():
                candidates.append((str(ancestor), "managed_alias", True))
        physical_parent = path.parent.resolve(strict=True)
        physical = physical_parent / path.name
        if physical_parent != path.parent:
            path = physical
        value = path.lstat()
        current = roots.setdefault(
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
            },
        )
        if role not in current["roles"]:
            current["roles"].append(role)
        if stat.S_ISLNK(value.st_mode) and not link_only:
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError):
                raise InstallationArchiveError(
                    "A managed root alias is cyclic, missing or unreadable."
                ) from None
            if str(resolved) in FORBIDDEN_ROOTS:
                raise InstallationArchiveError(
                    "A managed alias points at a protected root."
                )
            candidates.append((str(resolved), role, False))
    selected = []
    for path, row in sorted(roots.items(), key=lambda x: len(Path(x[0]).parts)):
        if any(
            parent["kind"] == "directory"
            and (Path(parent["path"]) in Path(path).parents or parent["path"] == path)
            for parent in selected
        ):
            continue
        if any(
            Path(x) == Path(path) or Path(x) in Path(path).parents for x in exclusions
        ):
            continue
        row["id"] = f"root_{len(selected):03d}"
        selected.append(row)
    if not selected or len(selected) > MAX_INSTALLATION_ROOTS:
        raise InstallationArchiveError(
            "The complete persistence root inventory exceeds its bound."
        )
    # No mounted source may contain another managed control mount unless explicitly excluded.
    for row in selected:
        for point in mount_records():
            mount = Path(point["path"])
            if Path(row["path"]) in mount.parents and not any(
                Path(x) == mount or Path(x) in mount.parents for x in exclusions
            ):
                raise InstallationArchiveError(
                    "A managed data tree contains a nested mount. Separate its media/delivery contents before complete installation backup."
                )
    # Unknown appdata is never silently dropped or assumed disposable/media.
    data_path = Path(data_root)
    if data_path.is_dir():
        for child in data_path.iterdir():
            if any(Path(x) == child or Path(x) in child.parents for x in exclusions):
                continue
            physical = child.resolve(strict=True) if child.is_symlink() else child
            if not any(
                Path(row["path"]) == child
                or Path(row["path"]) == physical
                or Path(row["path"]) in physical.parents
                or physical in Path(row["path"]).parents
                for row in selected
            ):
                mode = child.lstat().st_mode
                kind = (
                    "directory"
                    if stat.S_ISDIR(mode)
                    else (
                        "symlink"
                        if stat.S_ISLNK(mode)
                        else "file" if stat.S_ISREG(mode) else "special file"
                    )
                )
                raise InstallationArchiveError(_unclassified_message(child, kind))
    return selected, exclusions


def _capacity_filesystems(allocations):
    groups = {}
    for path, role, required in allocations:
        anchor = Path(path)
        while not anchor.exists():
            anchor = anchor.parent
        device = anchor.stat().st_dev
        group = groups.setdefault(
            device,
            {
                "roles": [],
                "base_required_bytes": 0,
                "free_bytes": shutil.disk_usage(anchor).free,
            },
        )
        if role not in group["roles"]:
            group["roles"].append(role)
        group["base_required_bytes"] += required
    rows = []
    for group in groups.values():
        base = group["base_required_bytes"]
        margin = (base + 9) // 10
        required = base + margin
        rows.append(
            {
                "id": f"filesystem_{len(rows)+1}",
                **group,
                "safety_margin_bytes": margin,
                "required_bytes": required,
                "shortfall_bytes": max(0, required - group["free_bytes"]),
                "sufficient": group["free_bytes"] >= required,
            }
        )
    return rows


def capacity(roots, scratch, destination, exclusions=()):
    anchor = Path(scratch)
    while not anchor.exists():
        anchor = anchor.parent
    free = shutil.disk_usage(anchor).free
    minimum = MIN_PAIRED_WORKSPACE_BYTES
    if free < minimum + (minimum + 9) // 10:
        # Do not allocate a census ledger when even the legacy minimum pair
        # cannot fit. The incomplete report cannot authorize a backup.
        return {
            "status": "insufficient",
            "complete": False,
            "estimated": True,
            "reserved": False,
            "source": None,
            "limits": installation_limits(),
            "filesystems": _capacity_filesystems(
                [(scratch, "local_verification", minimum)]
            ),
        }, []
    entries = logical = footer_bytes = tar_bytes = 0
    details = []
    with manifest(scratch, max_bytes=safe_ledger_budget(free, paired=True)) as database:
        quota = ledger_quota(database)
        for root in roots:
            count = size = 0
            with closing(
                source_rows(root, exclusions or root.get("exclusions", ()))
            ) as rows:
                for row, path, stamp in rows:
                    count += 1
                    size += row["size"]
                    footer_bytes += footer_record_bytes(row)
                    if footer_bytes > quota:
                        raise InstallationArchiveError(
                            f"The complete installation manifest needs at least {footer_bytes:,} bytes; current local capacity assigns {quota:,} bytes to its verification ledger. No entries were omitted."
                        )
                    tar_bytes += archive_entry_bytes(row)
                    _insert(database, row, path, stamp, reserve_hash=True)
            entries += count
            logical += size
            details.append(
                {k: root[k] for k in ("id", "path", "kind", "roles")}
                | {"entries": count, "logical_bytes": size}
            )
        ledger_bytes = ledger_footprint(database)
    # The census file has been removed before observing free space again, so
    # its measured allocation is not subtracted and then counted twice.
    local_required = paired_workspace_bytes(ledger_bytes)
    archive_required = tar_bytes + footer_bytes + ARCHIVE_FIXED_OVERHEAD_BYTES
    rows = _capacity_filesystems(
        [
            (scratch, "local_verification", local_required),
            (destination, "cold_archive", archive_required),
        ]
    )
    return {
        "status": (
            "sufficient" if all(x["sufficient"] for x in rows) else "insufficient"
        ),
        "complete": True,
        "estimated": True,
        "reserved": False,
        "source": {
            "entries": entries,
            "logical_bytes": logical,
            "root_count": len(roots),
            "estimated_archive_bytes": archive_required,
        },
        "verification": {
            "ledger_bytes": ledger_bytes,
            "paired_workspace_bytes": local_required,
            "footer_bytes": footer_bytes,
            "ledger_verified": True,
            "ledger_quota_bytes": quota,
        },
        "limits": installation_limits(),
        "filesystems": rows,
    }, details


def verify_cold_postgres(config):
    """Physical PG data is valid only after its own clean shutdown, never a live copy."""
    service = config.get("postgres") or {}
    root = Path(str(service.get("config_dir") or "/postgres_data")).resolve()
    if not (root / "PG_VERSION").exists():
        return
    # These links refer to required cluster contents, not disposable media links.
    for name in ("pg_wal", "pg_tblspc"):
        item = root / name
        if item.is_symlink() or (
            item.is_dir() and any(x.is_symlink() for x in item.iterdir())
        ):
            raise InstallationArchiveError(
                "PostgreSQL WAL/tablespaces use external links; complete physical cluster coverage is unsupported."
            )
    binary = shutil.which("pg_controldata")
    if binary is None:
        candidates = sorted(Path("/usr/lib/postgresql").glob("*/bin/pg_controldata"))
        binary = str(candidates[-1]) if candidates else None
    if not binary:
        raise InstallationArchiveError(
            "Clean PostgreSQL shutdown cannot be verified on this image."
        )
    result = subprocess.run(
        [binary, str(root)],
        capture_output=True,
        timeout=20,
        env={**os.environ, "LC_ALL": "C"},
        text=True,
        check=False,
    )
    if result.returncode or not any(
        "Database cluster state:" in line
        and line.split(":", 1)[1].strip() == "shut down"
        for line in result.stdout.splitlines()
    ):
        raise InstallationArchiveError(
            "Managed PostgreSQL did not reach clean shutdown; physical database backup is blocked."
        )


def ensure_local_restore_roots(roots):
    for root in roots:
        if root["kind"] == "directory" and not root.get("link_only"):
            require_local_path(root["path"])
