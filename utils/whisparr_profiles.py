"""Saved Whisparr family profiles and non-destructive SQLite profile seeding."""

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import time

import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException

from utils.whisparr_settings import (
    WhisparrConfigurationError,
    _configured_binary,
    read_whisparr_version,
    normalize_whisparr_release_version,
    resolve_whisparr_version,
    whisparr_release_selector,
)

_FAMILIES = ("v2", "v3")
_DATABASES = {"v2": ("whisparr.db", "whisparr2.db"), "v3": ("whisparr3.db",)}
_PROFILE_FIELDS = (
    "config_dir",
    "config_file",
    "log_file",
    "install_dir",
    "command",
    "release_version_enabled",
    "release_version",
    "pinned_version",
    "branch",
    "branch_enabled",
    "commit_sha",
    "repo_owner",
    "repo_name",
)
_MARKER = ".dumb-whisparr-profile.json"


def whisparr_profile_defaults():
    """Return the fixed configuration shape used by defaults and persistence."""
    return {
        family: {
            "initialized": False,
            "config_dir": "",
            "config_file": "",
            "log_file": "",
            "install_dir": "",
            "command": [],
            "release_version_enabled": False,
            "release_version": "latest",
            "pinned_version": "",
            "branch": "",
            "branch_enabled": False,
            "commit_sha": "",
            "repo_owner": "Whisparr",
            "repo_name": "Whisparr-Eros" if family == "v3" else "Whisparr",
            "source_config_dir": "",
        }
        for family in _FAMILIES
    }


def _profiles(config):
    profiles = whisparr_profile_defaults()
    for family, saved in (config.get("whisparr_profiles") or {}).items():
        if family not in profiles or not isinstance(saved, dict):
            raise WhisparrConfigurationError("Invalid Whisparr profile configuration.")
        profiles[family].update(deepcopy(saved))
    return profiles


def _artifacts(root, name):
    paths = [root / (name + suffix) for suffix in ("", "-wal", "-shm", "-journal")]
    paths.append(root / name.replace(".db", ".restore"))
    return [path for path in paths if path.exists() or path.is_symlink()]


def _database_names(root):
    return {
        family: [name for name in names if _artifacts(root, name)]
        for family, names in _DATABASES.items()
    }


def _detected_family(config):
    probe = deepcopy(config)
    probe.update(
        {
            "whisparr_version": "",
            "release_version_enabled": False,
            "pinned_version": "",
            "commit_sha": "",
        }
    )
    return resolve_whisparr_version(probe)


def _profile_from_config(config, family, existing=None):
    profile = deepcopy(existing or whisparr_profile_defaults()[family])
    for field in _PROFILE_FIELDS:
        if field in config:
            profile[field] = deepcopy(config[field])
    profile["initialized"] = True
    return profile


def _runtime_dir(instance_name, config, family):
    name = instance_name if instance_name is not None else config.get("process_name")
    if not name:
        return ""
    slug = str(name).lower().replace(" ", "_")
    if slug in {"", ".", ".."} or "/" in slug or "\\" in slug:
        raise WhisparrConfigurationError(
            "Whisparr instance name cannot form a safe installation directory."
        )
    return str(Path("/opt/whisparr/instances") / slug / family)


def _path_for_profile(original, root, destination, fallback):
    if original:
        try:
            relative = Path(original).relative_to(root)
            return str(destination / relative)
        except ValueError:
            pass
    return str(destination / fallback)


def _isolated_profile(config, family, source, *, instance_name, seed=False):
    profile = whisparr_profile_defaults()[family]
    root = Path(source)
    destination = root / "whisparr-profiles" / family
    profile.update(
        {
            "initialized": True,
            "config_dir": str(destination),
            "config_file": _path_for_profile(
                config.get("config_file"), root, destination, "config.xml"
            ),
            "log_file": _path_for_profile(
                config.get("log_file"), root, destination, "logs/whisparr.txt"
            ),
            "install_dir": _runtime_dir(instance_name, config, family),
            "source_config_dir": str(root) if seed else "",
        }
    )
    return profile


def _ensure_distinct_profiles(profiles):
    roots = [
        Path(profile["config_dir"]).resolve()
        for profile in profiles.values()
        if profile.get("initialized") and profile.get("config_dir")
    ]
    if len(roots) != len(set(roots)):
        raise WhisparrConfigurationError(
            "Whisparr v2 and v3 profiles require separate data directories."
        )


def plan_whisparr_profile(previous, candidate, *, instance_name=None):
    """Select saved paths without creating directories or modifying any files."""
    result = deepcopy(candidate)
    profiles = _profiles(previous)
    # Profiles are managed history. Stale dashboard/default maps must not
    # reset retained paths or restart a completed non-destructive seed.
    if "whisparr_profiles" in previous or "whisparr_profiles" in candidate:
        result["whisparr_profiles"] = deepcopy(profiles)

    explicit = result.get("whisparr_version", "")
    previous_explicit = previous.get("whisparr_version", "")
    if explicit not in ("", *_FAMILIES) or previous_explicit not in ("", *_FAMILIES):
        raise WhisparrConfigurationError("whisparr_version must be empty, v2, or v3.")
    root = Path(previous.get("config_dir") or candidate.get("config_dir") or ".")
    names = (
        _database_names(root)
        if not previous.get("postgres_enabled")
        and not candidate.get("postgres_enabled")
        else {family: [] for family in _FAMILIES}
    )
    if len(names["v2"]) > 1:
        raise WhisparrConfigurationError(
            "Both legacy whisparr.db and current whisparr2.db artifacts exist. "
            "Choose the intended v2 library before preparing a saved profile; "
            "DUMB will not merge or remove these files."
        )
    families = {family for family, files in names.items() if files}
    mixed = len(families) > 1
    if mixed and not explicit:
        raise WhisparrConfigurationError(
            "Choose v2 or v3 explicitly to prepare separate profiles from this "
            "mixed Whisparr directory; both original libraries will be preserved."
        )

    if previous_explicit:
        old_family = previous_explicit
    elif mixed:
        # A saved fixed release identifies the family whose settings should be
        # retained even when the previously automatic mixed root was ambiguous.
        selector = previous.get(
            "release_version"
            if previous.get("release_version_enabled")
            else "pinned_version"
        )
        fixed_version = normalize_whisparr_release_version(selector)
        old_family = (
            f"v{fixed_version.split('.')[0]}"
            if fixed_version and fixed_version.split(".")[0] in {"2", "3"}
            else explicit
        )
    else:
        old_family = _detected_family(previous)
    family = explicit or old_family
    switched = family != old_family
    if names[family]:
        database = root / names[family][0]
        if not database.is_file() or database.is_symlink():
            raise WhisparrConfigurationError(
                "Only Whisparr recovery files remain for the selected family. "
                "Preserve them and recover the original library before switching."
            )

    managed = any(profile.get("initialized") for profile in profiles.values())
    if (
        previous.get("postgres_enabled")
        and explicit
        and not previous_explicit
        and not managed
    ):
        runtime_version = read_whisparr_version(_configured_binary(previous))
        if not runtime_version or runtime_version.split(".")[0] not in {"2", "3"}:
            raise WhisparrConfigurationError(
                "The existing PostgreSQL Whisparr family cannot be verified. "
                "Keep its current selection or use a separate instance and "
                "databases; DUMB will not guess its schema."
            )
    if not switched and not mixed and not managed:
        # An unrelated save must not move an existing runtime or change legacy
        # automatic detection into an explicit selector.
        whisparr_release_selector(result)
        return result

    if mixed:
        source_config = previous.get("config_file")
        if (
            source_config
            and Path(source_config).absolute() != (root / "config.xml").absolute()
        ):
            raise WhisparrConfigurationError(
                "Automatic mixed-directory profiles require the existing native "
                "config_file at config_dir/config.xml. Preserve custom native "
                "configuration and prepare separate directories explicitly."
            )

    if (
        previous.get("postgres_enabled") or candidate.get("postgres_enabled")
    ) and switched:
        runtime_version = read_whisparr_version(_configured_binary(previous))
        known_family = (
            previous_explicit
            or managed
            or (runtime_version and runtime_version.split(".")[0] in {"2", "3"})
        )
        if known_family:
            raise WhisparrConfigurationError(
                "Changing Whisparr family with PostgreSQL requires a separate "
                "instance and databases; saved SQLite profiles do not convert schemas."
            )
        # A new PostgreSQL instance may select Eros directly. Preserve the
        # existing installed-runtime guard without introducing SQLite profiles.
        whisparr_release_selector(result)
        return result

    if mixed:
        runtime_version = read_whisparr_version(_configured_binary(previous))
        runtime_family = (
            f"v{runtime_version.split('.')[0]}" if runtime_version else old_family
        )
        for version in _FAMILIES:
            saved = profiles[version]
            if not saved.get("initialized") or (
                saved.get("config_dir")
                and Path(saved["config_dir"]).resolve() == root.resolve()
            ):
                isolated = _isolated_profile(
                    previous, version, root, instance_name=instance_name, seed=True
                )
                if version == old_family:
                    for field in _PROFILE_FIELDS:
                        if (
                            field
                            not in {
                                "config_dir",
                                "config_file",
                                "log_file",
                                "install_dir",
                                "command",
                            }
                            and field in previous
                        ):
                            isolated[field] = deepcopy(previous[field])
                for selector_field in ("pinned_version", "release_version"):
                    saved_selector = previous.get(selector_field)
                    fixed_version = normalize_whisparr_release_version(saved_selector)
                    if not fixed_version:
                        continue
                    if fixed_version.split(".")[0] == version[1:]:
                        isolated[selector_field] = saved_selector
                        if selector_field == "release_version":
                            isolated["release_version_enabled"] = bool(
                                previous.get("release_version_enabled")
                            )
                    else:
                        isolated[selector_field] = (
                            "latest" if selector_field == "release_version" else ""
                        )
                        if selector_field == "release_version":
                            isolated["release_version_enabled"] = False
                if version == runtime_family:
                    isolated["command"] = deepcopy(previous.get("command") or [])
                profiles[version] = isolated
    else:
        profiles[old_family] = _profile_from_config(
            previous, old_family, profiles[old_family]
        )

    if switched and not profiles[family].get("initialized"):
        profiles[family] = _isolated_profile(
            previous, family, root, instance_name=instance_name
        )

    target = profiles[family]
    if switched or mixed:
        for field in _PROFILE_FIELDS:
            result[field] = deepcopy(target[field])
        # Full-config saves contain old active values. Only intentional changed
        # values override restored target settings.
        for field in _PROFILE_FIELDS:
            if field in candidate and candidate.get(field) != previous.get(field):
                result[field] = deepcopy(candidate[field])
        if result.get("command"):
            result["command"] = [
                (
                    f"--data={result['config_dir']}"
                    if isinstance(argument, str) and argument.startswith("--data=")
                    else argument
                )
                for argument in result["command"]
            ]
    result["whisparr_version"] = family
    profiles[family] = _profile_from_config(result, family, target)
    _ensure_distinct_profiles(profiles)
    if profiles[family].get("source_config_dir") and (
        Path(result["config_file"]).absolute().parent
        != Path(result["config_dir"]).absolute()
    ):
        raise WhisparrConfigurationError(
            "A seeded Whisparr config_file must be directly inside config_dir."
        )
    result["whisparr_profiles"] = profiles
    # Pending seed is the only reason the selected destination can be absent.
    # Existing selected paths still get the normal family/release safeguards.
    whisparr_release_selector(result)
    if not result.get("postgres_enabled"):
        selected_root = Path(result.get("config_dir") or ".")
        selected_names = _database_names(selected_root)[family]
        if selected_names and not (selected_root / selected_names[0]).is_file():
            raise WhisparrConfigurationError(
                "Only Whisparr recovery files remain in the selected profile. "
                "Preserve them and recover the original library before switching."
            )
    return result


def _direct_file(path):
    try:
        info = path.lstat()
    except OSError as error:
        raise WhisparrConfigurationError(
            f"Whisparr profile source {path.name} is unavailable."
        ) from error
    if not stat.S_ISREG(info.st_mode):
        raise WhisparrConfigurationError(
            f"Whisparr profile source {path.name} must be a regular file."
        )
    return info.st_dev, info.st_ino


def _snapshot_sqlite(source, destination):
    identity = _direct_file(source)
    started = time.monotonic()
    source_connection = None
    destination_connection = None
    try:
        source_connection = sqlite3.connect(
            source.resolve().as_uri() + "?mode=ro", uri=True, timeout=2
        )
        source_connection.execute("PRAGMA query_only = ON")
        if _direct_file(source) != identity:
            raise WhisparrConfigurationError("Whisparr profile source changed.")
        destination_connection = sqlite3.connect(str(destination), timeout=2)

        def progress(status, remaining, total):
            del status, remaining, total
            if time.monotonic() - started > 10:
                raise WhisparrConfigurationError(
                    "Whisparr SQLite profile snapshot timed out."
                )

        source_connection.backup(
            destination_connection, pages=256, progress=progress, sleep=0.05
        )
        # A standalone snapshot must not depend on sidecars from its source.
        destination_connection.execute("PRAGMA journal_mode = DELETE")
        result = destination_connection.execute("PRAGMA quick_check").fetchall()
        if result != [("ok",)]:
            raise WhisparrConfigurationError(
                "Whisparr SQLite profile snapshot failed its integrity check."
            )
        if _direct_file(source) != identity:
            raise WhisparrConfigurationError("Whisparr profile source changed.")
    except sqlite3.Error as error:
        raise WhisparrConfigurationError(
            "Cannot safely snapshot the selected Whisparr SQLite library."
        ) from error
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
    destination.chmod(0o600)


def _profile_file_descriptor(directory_fd, name):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise WhisparrConfigurationError(
            "Whisparr profile entries must be regular files."
        )
    return descriptor


def _set_profile_owner(descriptor, owner_ids):
    if owner_ids is None:
        return
    user_id, group_id = owner_ids
    info = os.fstat(descriptor)
    if (info.st_uid, info.st_gid) != (user_id, group_id):
        os.fchown(descriptor, user_id, group_id)


def _completed_seed(destination, source, family, name, config_name, owner_ids):
    directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptors = []
    try:
        directory_info = os.fstat(directory_fd)
        try:
            marker_fd = _profile_file_descriptor(directory_fd, _MARKER)
        except FileNotFoundError:
            return False
        descriptors.append(marker_fd)
        with os.fdopen(os.dup(marker_fd), "r", encoding="utf-8") as handle:
            try:
                document = json.loads(handle.read(65537))
            except (OSError, ValueError) as error:
                raise WhisparrConfigurationError(
                    "Whisparr profile completion marker is unreadable."
                ) from error
        if document != {
            "family": family,
            "source": str(source.resolve()),
            "database": name,
        }:
            raise WhisparrConfigurationError(
                "Whisparr profile contains unrelated data; it will not be overwritten."
            )
        for filename in (name, config_name):
            try:
                descriptor = _profile_file_descriptor(directory_fd, filename)
            except FileNotFoundError:
                if filename == config_name:
                    continue
                raise
            descriptors.append(descriptor)
        resolve_whisparr_version(
            {"config_dir": f"/proc/self/fd/{directory_fd}", "whisparr_version": family}
        )
        for descriptor in descriptors:
            _set_profile_owner(descriptor, owner_ids)
        _set_profile_owner(directory_fd, owner_ids)
        current = destination.lstat()
        if (current.st_dev, current.st_ino) != (
            directory_info.st_dev,
            directory_info.st_ino,
        ):
            raise WhisparrConfigurationError(
                "Whisparr profile destination changed during preparation."
            )
        return True
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
        os.close(directory_fd)


def _create_profile_parents(parent):
    missing = []
    ancestor = parent
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o755)
        except FileExistsError:
            continue
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            # Only new intermediate directories are made traversable. Existing
            # data roots retain their ownership and permissions.
            os.fchmod(descriptor, 0o755)
        finally:
            os.close(descriptor)


def _seed_profile(source, destination, family, config_file, owner_ids=None):
    source = source.resolve()
    destination = destination.absolute()
    if destination.resolve() == source:
        raise WhisparrConfigurationError(
            "Whisparr profile destination must differ from its original directory."
        )
    names = _database_names(source)
    if len(names[family]) > 1:
        raise WhisparrConfigurationError(
            "Multiple Whisparr databases identify the same family; choose the "
            "intended library before preparing a profile."
        )
    if not names[family]:
        raise WhisparrConfigurationError(
            "The selected Whisparr source library is missing; refusing a silent "
            "fresh start while a profile snapshot is pending."
        )
    name = names[family][0]
    database = source / name
    if not database.exists() or database.is_symlink():
        raise WhisparrConfigurationError(
            "Only Whisparr recovery files remain for this family. Preserve them "
            "and recover the original library before preparing a profile."
        )
    for artifact in _artifacts(source, name):
        _direct_file(artifact)
        if artifact.suffix == ".restore":
            raise WhisparrConfigurationError(
                "Whisparr restore files require recovery before profile seeding."
            )
    target_config = Path(config_file).absolute()
    if target_config.parent != destination:
        raise WhisparrConfigurationError(
            "A seeded Whisparr config_file must be directly inside config_dir."
        )
    if destination.is_symlink():
        raise WhisparrConfigurationError(
            "Whisparr profile seed destination cannot be a symbolic link."
        )
    if destination.exists():
        if not destination.is_dir():
            raise WhisparrConfigurationError(
                "Whisparr profile destination is not a directory."
            )
        if _completed_seed(
            destination, source, family, name, target_config.name, owner_ids
        ):
            return
        if any(destination.iterdir()):
            raise WhisparrConfigurationError(
                "Whisparr profile destination contains existing data; "
                "DUMB will not overwrite it."
            )
    _create_profile_parents(destination.parent)
    staging = Path(
        tempfile.mkdtemp(prefix=".dumb-whisparr-seed-", dir=destination.parent)
    )
    created = {}
    destination_created = False
    destination_identity = None
    destination_fd = None
    try:
        _snapshot_sqlite(database, staging / name)
        native_config = source / "config.xml"
        if native_config.exists() or native_config.is_symlink():
            _direct_file(native_config)
            with native_config.open("rb") as handle:
                content = handle.read(65537)
            if len(content) > 65536:
                raise WhisparrConfigurationError(
                    "Whisparr native configuration is too large to seed safely."
                )
            try:
                native_root = ET.fromstring(content)
                if native_root.tag != "Config":
                    raise ValueError("Unexpected native configuration root.")
            except (ET.ParseError, ValueError, DefusedXmlException):
                raise WhisparrConfigurationError(
                    "Whisparr native configuration is invalid or unsafe; "
                    "the original configuration has been preserved."
                ) from None
            (staging / target_config.name).write_bytes(content)
            (staging / target_config.name).chmod(0o600)
        marker = staging / _MARKER
        marker.write_text(
            json.dumps({"family": family, "source": str(source), "database": name}),
            encoding="utf-8",
        )
        marker.chmod(0o600)
        try:
            destination.mkdir(mode=0o700)
            destination_created = True
        except FileExistsError:
            if (
                destination.is_symlink()
                or not destination.is_dir()
                or any(destination.iterdir())
            ):
                raise WhisparrConfigurationError(
                    "Whisparr profile destination changed during preparation."
                )
        destination_fd = os.open(
            destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        info = os.fstat(destination_fd)
        destination_identity = info.st_dev, info.st_ino
        staging_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for entry in staging.iterdir():
                descriptor = _profile_file_descriptor(staging_fd, entry.name)
                try:
                    _set_profile_owner(descriptor, owner_ids)
                finally:
                    os.close(descriptor)
        finally:
            os.close(staging_fd)
        _set_profile_owner(destination_fd, owner_ids)
        # Hard-link publication is same-filesystem and refuses collisions.
        # Publish the marker last; an interrupted partial seed never appears done.
        entries = [
            entry for entry in sorted(staging.iterdir()) if entry.name != _MARKER
        ] + [marker]
        for entry in entries:
            identity = _direct_file(entry)
            os.link(
                entry,
                entry.name,
                dst_dir_fd=destination_fd,
                follow_symlinks=False,
            )
            created[entry.name] = identity
        info = destination.lstat()
        if (info.st_dev, info.st_ino) != destination_identity:
            raise WhisparrConfigurationError(
                "Whisparr profile destination changed during preparation."
            )
    except Exception:
        if destination_fd is not None:
            for name, identity in created.items():
                try:
                    info = os.stat(name, dir_fd=destination_fd, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) == identity:
                        os.unlink(name, dir_fd=destination_fd)
                except FileNotFoundError:
                    pass
            if destination_created:
                try:
                    info = destination.lstat()
                    if (info.st_dev, info.st_ino) == destination_identity:
                        destination.rmdir()
                except OSError:
                    pass
        raise
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        shutil.rmtree(staging)


def prepare_whisparr_profile(config, *, owner_ids=None):
    """Seed one admitted profile; never delete or alter its original library."""
    result = deepcopy(config)
    family = result.get("whisparr_version", "")
    profiles = _profiles(result)
    if family not in _FAMILIES:
        return result
    profile = profiles[family]
    source = profile.get("source_config_dir")
    if not source:
        return result
    if result.get("postgres_enabled"):
        raise WhisparrConfigurationError(
            "SQLite profile seeding is unavailable for PostgreSQL instances."
        )
    try:
        _seed_profile(
            Path(source),
            Path(result["config_dir"]),
            family,
            result["config_file"],
            owner_ids=owner_ids,
        )
    except OSError as error:
        raise WhisparrConfigurationError(
            "Cannot safely prepare the selected Whisparr profile."
        ) from error
    profile["source_config_dir"] = ""
    result["whisparr_profiles"] = profiles
    resolve_whisparr_version(result)
    return result
