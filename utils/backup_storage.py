"""Registered mounted destinations for immutable cold artifacts; no import-time I/O."""

from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from urllib.parse import urlsplit

PROTOCOL = 1
MAX_JSON_BYTES = 256 * 1024 * 1024
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_NAMESPACE = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}")
_ARTIFACT = re.compile(r"[0-9a-f]{32}")
_LOCAL = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "overlay", "tmpfs", "ramfs"}
_CACHE = {}
_CACHE_LOCK = threading.RLock()


class BackupStorageError(RuntimeError):
    """A destination or owned artifact cannot be established safely."""


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _decode_mount(value):
    for encoded, plain in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        value = value.replace(encoded, plain)
    return value


def mount_records():
    records = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, separator, after = line.partition(" - ")
        fields, filesystem = before.split(), after.split()
        if not separator or len(fields) < 6 or len(filesystem) < 2:
            raise BackupStorageError("The mounted storage inventory is unavailable.")
        records.append(
            {
                "mount_id": fields[0],
                "path": _decode_mount(fields[4]),
                "filesystem": filesystem[0],
                # Private filesystem coordinates distinguish bind aliases.
                "device": fields[2],
                "root": _decode_mount(fields[3]),
                "signature": _digest(
                    [filesystem[0], filesystem[1], fields[3], fields[4]]
                ),
            }
        )
    return records


def _durable_inspection(value):
    """Device numbers can change after remounts; owned storage coordinates persist."""
    if not isinstance(value, dict):
        raise BackupStorageError("The registered storage identity is invalid.")
    root = value.get("root_identity")
    if not isinstance(root, dict) or any(
        type(root.get(key)) is not int for key in ("device", "inode", "mode")
    ):
        raise BackupStorageError("The registered storage root identity is invalid.")
    keys = ("path", "mount_signature", "filesystem_type", "storage_type")
    if any(not isinstance(value.get(key), str) or not value[key] for key in keys):
        raise BackupStorageError("The registered storage coordinates are invalid.")
    return {key: value[key] for key in keys} | {
        "root_identity": {key: root[key] for key in ("inode", "mode")}
    }


def _absolute(value):
    path = Path(str(value))
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise BackupStorageError("Choose a dedicated existing absolute container path.")
    return path


def _mount(path):
    target = Path(path)
    matches = [
        row
        for row in mount_records()
        if target == Path(row["path"]) or Path(row["path"]) in target.parents
    ]
    if not matches:
        raise BackupStorageError("The selected mounted storage is unavailable.")
    return max(matches, key=lambda row: len(Path(row["path"]).parts))


def _path_ancestry(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise BackupStorageError(
            "Storage containment requires an absolute direct path."
        )
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    identities = []
    complete = True
    try:
        value = os.fstat(descriptor)
        identities.append((value.st_dev, value.st_ino))
        for part in path.parts[1:]:
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                complete = False
                break
            os.close(descriptor)
            descriptor = child
            value = os.fstat(descriptor)
            identities.append((value.st_dev, value.st_ino))
        return (identities[-1] if complete else None), tuple(identities)
    except OSError as error:
        raise BackupStorageError(
            "Storage ancestry could not be verified without following links."
        ) from error
    finally:
        os.close(descriptor)


def _filesystem_coordinate(path, records):
    matches = [
        row
        for row in records
        if path == Path(row["path"]) or Path(row["path"]) in path.parents
    ]
    if not matches:
        raise BackupStorageError("The storage filesystem ancestry is unavailable.")
    record = max(matches, key=lambda row: len(Path(row["path"]).parts))
    if "device" not in record or "root" not in record:
        return None
    relative = path.relative_to(record["path"])
    return record["device"], Path(record["root"]) / relative


def paths_overlap(first, second):
    """Compare direct paths, including bind aliases and not-yet-created tails."""
    first, second = Path(first), Path(second)
    if any(not path.is_absolute() or ".." in path.parts for path in (first, second)):
        raise BackupStorageError("Storage containment requires absolute direct paths.")
    if first == second or first in second.parents or second in first.parents:
        return True
    first_root, first_ancestors = _path_ancestry(first)
    second_root, second_ancestors = _path_ancestry(second)
    if (first_root is not None and first_root in second_ancestors) or (
        second_root is not None and second_root in first_ancestors
    ):
        return True
    records = mount_records()
    left, right = _filesystem_coordinate(first, records), _filesystem_coordinate(
        second, records
    )
    if left is not None and right is not None and left[0] == right[0]:
        return (
            left[1] == right[1]
            or left[1] in right[1].parents
            or right[1] in left[1].parents
        )
    return False


def require_local_path(path):
    """Network/unknown filesystems cannot host control or active SQLite state."""
    path = _absolute(path)
    record = _mount(path)
    if record["filesystem"] not in _LOCAL:
        raise BackupStorageError(
            "Control state, SQLite databases and migration scratch require a verified local filesystem."
        )
    return record


def _identity(value):
    return {"device": value.st_dev, "inode": value.st_ino, "mode": value.st_mode}


@contextmanager
def _directory(path, *, create=False, private=False):
    path = _absolute(path)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise BackupStorageError(
                        "The selected storage directory is missing; no local fallback was created."
                    ) from None
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    # Concurrent creation must still pass the no-follow open.
                    pass
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = child
        value = os.fstat(descriptor)
        if private and stat.S_IMODE(value.st_mode) & 0o077:
            raise BackupStorageError(
                "The owned backup directory must enforce private mode 0700."
            )
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _child_directory(parent, name, *, create=False, private=True):
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
    except FileNotFoundError:
        if not create:
            raise BackupStorageError(
                "The owned backup directory is missing; no local fallback was created."
            ) from None
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            # A raced entry is accepted only as a validated direct directory.
            pass
        descriptor = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
    try:
        if private and stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o077:
            raise BackupStorageError(
                "The owned backup directory must enforce private mode 0700."
            )
        yield descriptor
    finally:
        os.close(descriptor)


def _file_stamp(value):
    return (_identity(value), value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _private_file(value):
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_nlink != 1
        or stat.S_IMODE(value.st_mode) & 0o077
    ):
        raise BackupStorageError(
            "The owned backup file must be a private direct regular file."
        )


def _shared_permissions(value):
    allowed = value.get("allow_shared_permissions", False)
    if not isinstance(allowed, bool):
        raise BackupStorageError("Shared storage permission consent must be a boolean.")
    return allowed


def _record_permissions(record):
    # Ownership is immutable. A reviewed permission policy is separate metadata.
    if "allow_shared_permissions" in record:
        return _shared_permissions(record)
    return _shared_permissions(record.get("binding") or {})


def _external_permissions(value, *, allow_shared_permissions=False, directory=False):
    if directory:
        if not stat.S_ISDIR(value.st_mode):
            raise BackupStorageError(
                "The archive directory must be a direct directory."
            )
    elif not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise BackupStorageError(
            "The archive file must be a direct single-link regular file."
        )
    if not allow_shared_permissions and stat.S_IMODE(value.st_mode) & 0o077:
        kind = "directory" if directory else "file"
        required = "0700" if directory else "0600"
        observed = f"{stat.S_IMODE(value.st_mode):04o}"
        raise BackupStorageError(
            f"The external archive {kind} reports mode {observed}; private mode "
            f"{required} is required unless shared storage permissions are explicitly trusted. "
            "DUMB does not change mounted storage permissions."
        )


def _permission_warning(allowed):
    return (
        [
            "Shared storage permissions are trusted for external archives. Archives may "
            "contain credentials and private keys; mount access controls and everyone "
            "with access must be trusted. DUMB does not secure or encrypt this storage "
            "and does not change its permissions."
        ]
        if allowed
        else []
    )


@contextmanager
def _external_child(parent, name, *, create=False, allow_shared_permissions=False):
    with _child_directory(parent, name, create=create, private=False) as descriptor:
        _external_permissions(
            os.fstat(descriptor),
            allow_shared_permissions=allow_shared_permissions,
            directory=True,
        )
        yield descriptor


def _reference(profile, namespace, artifact):
    return f"dumb-backup://{profile}/{namespace}/{artifact}"


def parse_backup_reference(value):
    parsed = urlsplit(str(value))
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "dumb-backup"
        or parsed.query
        or parsed.fragment
        or not _ID.fullmatch(parsed.netloc)
        or len(parts) != 2
        or not _NAMESPACE.fullmatch(parts[0])
        or not _ARTIFACT.fullmatch(parts[1])
    ):
        raise BackupStorageError("The backup reference is invalid.")
    return parsed.netloc, parts[0], parts[1]


def is_backup_reference(value):
    return isinstance(value, str) and value.startswith("dumb-backup:")


def selected_backup_destination(service):
    value = (service or {}).get("backup_destination_id") or ""
    if not isinstance(value, str):
        raise BackupStorageError("The selected backup destination is invalid.")
    return value.strip() or None


class _Writer:
    def __init__(self, handle, check):
        self.handle, self.check = handle, check
        self.digest = hashlib.sha256()
        self.size = 0
        self.checked = time.monotonic()

    def write(self, data):
        if time.monotonic() - self.checked >= 1:
            self.check()
            self.checked = time.monotonic()
        written = self.handle.write(data)
        self.digest.update(data[:written])
        self.size += written
        return written

    def flush(self):
        return self.handle.flush()


class _Reader:
    def __init__(self, handle, check):
        self.handle, self.check = handle, check
        self.digest = hashlib.sha256()
        self.size = 0
        self.checked = time.monotonic()

    def read(self, size=-1):
        if time.monotonic() - self.checked >= 1:
            self.check()
            self.checked = time.monotonic()
        data = self.handle.read(size)
        self.digest.update(data)
        self.size += len(data)
        return data


class BackupStorage:
    def __init__(self, config_manager, *, state_root=None):
        self.config_manager = config_manager
        config_path = getattr(config_manager, "file_path", None)
        parent = (
            Path(config_path).parent
            if isinstance(config_path, (str, os.PathLike))
            else Path("/config")
        )
        self.state_root = (
            Path(state_root) if state_root is not None else parent / "backup-storage"
        )
        self._lock = threading.RLock()
        self._active = {}
        self._registering = set()
        self._control_identity = None
        self._runtime_bindings = {}
        self._last_checks = {}
        self._revalidation_reviews = {}

    def _config(self):
        return (getattr(self.config_manager, "config", {}) or {}).get(
            "backup_storage"
        ) or {}

    def _id(self, profile):
        if profile == "@default":
            profile = self._config().get("default_destination_id") or ""
        if not isinstance(profile, str) or not _ID.fullmatch(profile):
            raise BackupStorageError("Select an enabled registered backup destination.")
        return profile

    def _check_control(self):
        if getattr(self, "boot_error", None):
            raise BackupStorageError(
                "The local backup registry failed private-state validation; review its retained state and restart."
            )
        record = require_local_path(self.state_root)
        with _directory(self.state_root.parent) as parent:
            current = {
                "mount": record["mount_id"],
                "signature": record["signature"],
                "parent": _identity(os.fstat(parent)),
            }
        if self._control_identity is None:
            self._control_identity = current
        elif current != self._control_identity:
            raise BackupStorageError("The local backup control filesystem changed.")

    def _state(self, *parts):
        if any(
            not isinstance(part, str)
            or part in {"", ".", ".."}
            or Path(part).name != part
            for part in parts
        ):
            raise BackupStorageError("The local backup receipt identity is invalid.")
        with self._lock:
            self._check_control()
            with _directory(self.state_root.parent) as parent:
                with _child_directory(
                    parent, self.state_root.name, create=True
                ) as descriptor:
                    descriptors = []
                    try:
                        for name in parts[:-1]:
                            with _child_directory(
                                descriptor, name, create=True
                            ) as current:
                                child = os.dup(current)
                            descriptors.append(child)
                            descriptor = child
                    finally:
                        for child in reversed(descriptors):
                            os.close(child)
            self._check_control()
        return self.state_root.joinpath(*parts)

    def _read(self, path):
        self._check_control()
        try:
            value = path.lstat()
        except FileNotFoundError:
            return None
        _private_file(value)
        if value.st_uid != os.geteuid() or value.st_size > MAX_RECEIPT_BYTES:
            raise BackupStorageError("The local storage receipt is unsafe.")
        with _directory(path.parent, private=True) as parent:
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            with os.fdopen(descriptor, "rb") as handle:
                if _identity(os.fstat(handle.fileno())) != _identity(value):
                    raise BackupStorageError(
                        "The storage receipt changed while being opened."
                    )
                return json.loads(handle.read(MAX_RECEIPT_BYTES + 1))

    def _write(self, path, payload):
        self._check_control()
        if path.exists() or path.is_symlink():
            self._read(path)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > MAX_RECEIPT_BYTES:
            raise BackupStorageError("The storage receipt exceeds its bound.")
        with _directory(path.parent, private=True) as parent:
            temporary = f".{path.name}.{secrets.token_hex(8)}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._check_control()
                os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
                self._check_control()
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def _binding_path(self, profile):
        return self._state("profiles", f"{profile}.json")

    def _profile(self, profile):
        profile = self._id(profile)
        value = (
            self._config().get("profiles") or self._config().get("instances") or {}
        ).get(profile)
        if not isinstance(value, dict) or not value.get("enabled"):
            raise BackupStorageError(
                "The selected backup destination is disabled or missing."
            )
        return profile, copy.deepcopy(value)

    def _inspect(self, path):
        path = _absolute(path)
        if any(
            path == Path(item) or Path(item) in path.parents
            for item in (
                "/proc",
                "/sys",
                "/dev",
                "/run",
                "/etc",
                "/root",
                "/mnt/debrid",
            )
        ):
            raise BackupStorageError(
                "Choose dedicated backup storage outside system and delivery paths."
            )
        record = _mount(path)
        with _directory(path) as descriptor:
            identity = _identity(os.fstat(descriptor))
        return {
            "path": str(path),
            "root_identity": identity,
            "mount_signature": record["signature"],
            "mount_id": record["mount_id"],
            "filesystem_type": record["filesystem"],
            "storage_type": "local" if record["filesystem"] in _LOCAL else "network",
        }

    def _requested_profile(self, request):
        if not isinstance(request.get("enabled", True), bool) or not isinstance(
            request.get("make_default", False), bool
        ):
            raise BackupStorageError(
                "Storage enabled and default options must be booleans."
            )
        profile = request.get("id") or ""
        if not isinstance(profile, str) or not _ID.fullmatch(profile):
            raise BackupStorageError(
                "Use a backup profile identifier containing lowercase letters, digits, underscores or hyphens."
            )
        return {
            "id": profile,
            "name": str(request.get("name") or profile)[:120],
            "path": str(request.get("path") or ""),
            "enabled": request.get("enabled", True),
            "allow_shared_permissions": _shared_permissions(request),
        }

    def _observe_permissions(self, root_fd, profile, allowed):
        observed = {"directory_mode": None, "file_mode": None}
        blockers = []
        descriptors = []
        descriptor = root_fd
        try:
            for name in (".dumb-backups", profile):
                try:
                    child = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    break
                descriptors.append(child)
                descriptor = child
                value = os.fstat(descriptor)
                mode = f"{stat.S_IMODE(value.st_mode):04o}"
                if (
                    observed["directory_mode"] is None
                    or stat.S_IMODE(value.st_mode) & 0o077
                ):
                    observed["directory_mode"] = mode
                try:
                    _external_permissions(
                        value, allow_shared_permissions=allowed, directory=True
                    )
                except BackupStorageError as error:
                    blockers.append(str(error))
            else:
                try:
                    marker = os.open(
                        ".destination.json",
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    pass
                else:
                    try:
                        value = os.fstat(marker)
                        observed["file_mode"] = f"{stat.S_IMODE(value.st_mode):04o}"
                        try:
                            _external_permissions(
                                value, allow_shared_permissions=allowed
                            )
                        except BackupStorageError as error:
                            blockers.append(str(error))
                    finally:
                        os.close(marker)
        finally:
            for child in reversed(descriptors):
                os.close(child)
        return observed, list(dict.fromkeys(blockers))

    def preview(self, request):
        value = self._requested_profile(request)
        blockers = []
        observed = {"directory_mode": None, "file_mode": None}
        warnings = _permission_warning(value["allow_shared_permissions"])
        try:
            inspected = self._inspect(value["path"])
            value.update(
                {key: inspected[key] for key in ("filesystem_type", "storage_type")}
            )
            with _directory(value["path"]) as descriptor:
                observed, blockers = self._observe_permissions(
                    descriptor, value["id"], value["allow_shared_permissions"]
                )
        except (BackupStorageError, OSError):
            blockers.append(
                "The existing container-mounted destination could not be inspected safely; no directory was created."
            )
        value.update(
            registered=False,
            ready=not blockers and value["enabled"],
            blockers=blockers,
            warnings=warnings,
            observed_permissions=observed,
        )
        return {
            "protocol": PROTOCOL,
            "ready": value["ready"],
            "profile": value,
            "blockers": blockers,
            "warnings": warnings,
            "observed_permissions": observed,
        }

    def profiles(self):
        # Catalog reads are local: a hard NFS outage must not hang all settings/status.
        values = []
        for profile, item in (
            self._config().get("profiles") or self._config().get("instances") or {}
        ).items():
            if not _ID.fullmatch(str(profile)) or not isinstance(item, dict):
                continue
            record = self._read(self._binding_path(profile))
            status = (record or {}).get("public", {})
            current = {
                "name": str(item.get("name") or profile),
                "path": str(item.get("path") or ""),
                "enabled": bool(item.get("enabled")),
                "allow_shared_permissions": _shared_permissions(item),
            }
            matches = bool(record) and all(
                current[key]
                == (
                    status.get(key, False)
                    if key == "allow_shared_permissions"
                    else status.get(key)
                )
                for key in current
            )
            values.append(
                {
                    "id": profile,
                    **current,
                    "registered": bool(record),
                    "ready": bool(matches and current["enabled"]),
                    "storage_type": status.get("storage_type"),
                    "filesystem_type": status.get("filesystem_type"),
                    "last_checked_at": (record or {}).get("checked_at"),
                    "warnings": _permission_warning(
                        current["allow_shared_permissions"]
                    ),
                    "observed_permissions": status.get(
                        "observed_permissions",
                        {"directory_mode": None, "file_mode": None},
                    ),
                    "blockers": (
                        []
                        if matches
                        else ["Register and verify this destination before use."]
                    ),
                }
            )
        for row in values:
            config = (
                self._config().get("profiles") or self._config().get("instances") or {}
            ).get(row["id"], {})
            checked = self._last_checks.get(row["id"])
            if checked and checked.get("config_fingerprint") == _digest(config):
                row.update(
                    {
                        key: value
                        for key, value in checked.items()
                        if key != "config_fingerprint"
                    }
                )
                row["last_checked_at"] = checked["checked_at"]
            else:
                row.update(
                    live_checked=False,
                    validation_state="not_checked",
                    revalidation_required=False,
                )
            row["usage"] = self._profile_usage(row["id"])
        return {
            "protocol": PROTOCOL,
            "default_destination_id": self._config().get("default_destination_id")
            or "",
            "destinations": values,
        }

    def save_profile(self, request):
        public = self._requested_profile(request)
        profile = public["id"]
        _absolute(public["path"])
        previous_record = self._read(self._binding_path(profile))
        if previous_record and previous_record["binding"]["path"] != public["path"]:
            raise BackupStorageError(
                "Use a new identifier for a changed backup root or mounted filesystem."
            )
        if (
            previous_record
            and _record_permissions(previous_record)
            and not public["allow_shared_permissions"]
        ):
            usage = self._profile_usage(profile)
            if usage:
                raise BackupStorageError(usage[0]["label"])
        if not public["enabled"]:
            usage = self._profile_usage(profile)
            if usage:
                raise BackupStorageError(usage[0]["label"])
        with self._lock:
            if self._active.get(profile) or profile in self._registering:
                raise BackupStorageError(
                    "An active backup still owns this destination."
                )
            self._registering.add(profile)
            expected_config = copy.deepcopy(
                (
                    self._config().get("profiles")
                    or self._config().get("instances")
                    or {}
                ).get(profile)
            )
        try:
            if public["enabled"]:
                inspected = self._inspect(public["path"])
                binding_path = self._binding_path(profile)
                previous = self._read(binding_path)
                if previous and _durable_inspection(
                    previous["binding"]
                ) != _durable_inspection(inspected):
                    raise BackupStorageError(
                        "Use a new identifier for a changed backup root or mounted filesystem."
                    )
                with self._lock:
                    runtime = self._runtime_bindings.get(profile)
                    if runtime is not None and runtime != inspected:
                        raise BackupStorageError(
                            "The destination mount changed since this controller verified it; "
                            "review and revalidate it before editing its registration."
                        )
                with _directory(inspected["path"]) as root_fd:
                    if self._inspect(public["path"]) != inspected:
                        raise BackupStorageError(
                            "The destination mount changed before registration."
                        )
                    marker, observed = self._register_marker(
                        root_fd, profile, previous, inspected, public
                    )
                if self._inspect(public["path"]) != inspected:
                    raise BackupStorageError(
                        "The destination mount changed during registration."
                    )
                if previous:
                    binding = copy.deepcopy(previous["binding"])
                else:
                    binding = {
                        key: inspected[key]
                        for key in (
                            "path",
                            "root_identity",
                            "mount_signature",
                            "filesystem_type",
                            "storage_type",
                        )
                    }
                    binding["marker"] = marker
                    binding["allow_shared_permissions"] = public[
                        "allow_shared_permissions"
                    ]
                public.update(
                    {key: inspected[key] for key in ("filesystem_type", "storage_type")}
                )
                public.update(
                    registered=True,
                    ready=True,
                    blockers=[],
                    warnings=_permission_warning(public["allow_shared_permissions"]),
                    observed_permissions=observed,
                )
                # Retain proof of the newly created owned marker even if a raw
                # configuration edit races the final configuration publication.
                self._write(
                    binding_path,
                    {
                        "binding": binding,
                        "allow_shared_permissions": public["allow_shared_permissions"],
                        "public": public,
                        "checked_at": int(time.time()),
                    },
                )
                self._clear_pending_registration(profile)
            else:
                if request.get("make_default"):
                    raise BackupStorageError(
                        "A disabled destination cannot be the default."
                    )
                public.update(
                    registered=False,
                    ready=False,
                    blockers=["This destination is disabled."],
                )
            with self._lock:
                current = (
                    self._config().get("profiles")
                    or self._config().get("instances")
                    or {}
                ).get(profile)
                if current != expected_config:
                    raise BackupStorageError(
                        "The profile configuration changed during registration; review and register again."
                    )
                config = self.config_manager.config.setdefault("backup_storage", {})
                config.setdefault("profiles", {})[profile] = {
                    key: public[key]
                    for key in ("name", "path", "enabled", "allow_shared_permissions")
                }
                if request.get("make_default"):
                    config["default_destination_id"] = profile
                self.config_manager.save_config()
                if public["enabled"]:
                    self._pin_runtime(profile, inspected)
                self._last_checks.pop(profile, None)
            return {"protocol": PROTOCOL, "profile": public}
        finally:
            with self._lock:
                self._registering.discard(profile)

    def _pending_registration(self, profile, inspected):
        path = self._state("pending", f"{profile}.json")
        pending = self._read(path)
        stable = {
            key: inspected[key]
            for key in (
                "path",
                "root_identity",
                "mount_signature",
                "filesystem_type",
                "storage_type",
            )
        }
        if pending is not None:
            if (
                not isinstance(pending, dict)
                or not isinstance(pending.get("inspected"), dict)
                or not isinstance(pending.get("marker"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", pending["marker"])
            ):
                raise BackupStorageError(
                    "The unfinished storage registration proof is invalid."
                )
            original = pending["inspected"]
            if _durable_inspection(original) != _durable_inspection(stable):
                raise BackupStorageError(
                    "The unfinished storage registration belongs to a different mounted directory."
                )
        return path, pending, stable

    def _clear_pending_registration(self, profile):
        path = self._state("pending", f"{profile}.json")
        value = self._read(path)
        if value is None:
            return
        with _directory(path.parent, private=True) as descriptor:
            current = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
            _private_file(current)
            if current.st_uid != os.geteuid():
                raise BackupStorageError(
                    "The unfinished storage registration owner changed."
                )
            os.unlink(path.name, dir_fd=descriptor)
            os.fsync(descriptor)

    def _register_marker(self, root_fd, profile, previous, inspected, public):
        allowed = public["allow_shared_permissions"]
        observed = {"directory_mode": None, "file_mode": None}
        with _external_child(
            root_fd,
            ".dumb-backups",
            create=not previous,
            allow_shared_permissions=allowed,
        ) as base_fd:
            observed["directory_mode"] = (
                f"{stat.S_IMODE(os.fstat(base_fd).st_mode):04o}"
            )
            if self._inspect(public["path"]) != inspected:
                raise BackupStorageError(
                    "The destination mount changed during registration."
                )
            with _external_child(
                base_fd, profile, create=not previous, allow_shared_permissions=allowed
            ) as descriptor:
                mode = os.fstat(descriptor).st_mode
                if (
                    not int(observed["directory_mode"], 8) & 0o077
                    or stat.S_IMODE(mode) & 0o077
                ):
                    observed["directory_mode"] = f"{stat.S_IMODE(mode):04o}"
                pending_path, pending, stable = self._pending_registration(
                    profile, inspected
                )
                try:
                    marker_fd = os.open(
                        ".destination.json",
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    if previous:
                        raise BackupStorageError(
                            "The registered storage identity is missing; no fallback identity was created."
                        ) from None
                    marker = (pending or {}).get("marker") or secrets.token_hex(32)
                    # Persist authority before the external O_EXCL write: a fixed
                    # file-mode mask may reject the newly created marker afterward.
                    self._write(pending_path, {"inspected": stable, "marker": marker})
                    marker_fd = os.open(
                        ".destination.json",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=descriptor,
                    )
                    with os.fdopen(marker_fd, "w") as handle:
                        handle.write(json.dumps({"marker": marker}))
                        handle.flush()
                        os.fsync(handle.fileno())
                        value = os.fstat(handle.fileno())
                        observed["file_mode"] = f"{stat.S_IMODE(value.st_mode):04o}"
                        _external_permissions(value, allow_shared_permissions=allowed)
                else:
                    with os.fdopen(marker_fd, "r") as handle:
                        value = os.fstat(handle.fileno())
                        observed["file_mode"] = f"{stat.S_IMODE(value.st_mode):04o}"
                        _external_permissions(value, allow_shared_permissions=allowed)
                        raw = handle.read(4097)
                    if len(raw) > 4096:
                        raise BackupStorageError(
                            "The storage marker exceeds its bound."
                        )
                    marker = json.loads(raw).get("marker")
                    authority = previous["binding"] if previous else pending
                    if not authority or marker != authority.get("marker"):
                        raise BackupStorageError(
                            "The destination contains an unowned storage identity."
                        )
                os.fsync(descriptor)
                return marker, observed

    def _configured(self, profile):
        profile = self._id(profile)
        value = (
            self._config().get("profiles") or self._config().get("instances") or {}
        ).get(profile)
        if not isinstance(value, dict):
            raise BackupStorageError("The backup destination registration is missing.")
        return profile, copy.deepcopy(value)

    def _references_profile(self, value, profile):
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    key in {"destination_id", "backup_destination_id"}
                    and item == profile
                ):
                    return True
                if self._references_profile(item, profile):
                    return True
        elif isinstance(value, list):
            return any(self._references_profile(item, profile) for item in value)
        elif isinstance(value, str) and value.startswith("dumb-backup://"):
            return parse_backup_reference(value)[0] == profile
        return False

    def _recovery_usage(self, profile):
        """Read only bounded local recovery metadata, never mounted backup storage."""
        usage = []
        budget = [0, 0]

        @contextmanager
        def directory(path):
            # Anchor every component below the local config directory. Never stat
            # or follow an absolute recovery path through a replaced parent link.
            mount = require_local_path(path)
            relative = path.relative_to(self.state_root.parent)
            with _directory(self.state_root.parent) as root:
                anchor = os.fstat(root)
                descriptor = os.dup(root)
            trusted_uids = {os.geteuid(), anchor.st_uid}
            trusted_gids = {os.getegid(), anchor.st_gid}
            for key, trusted in (("puid", trusted_uids), ("pgid", trusted_gids)):
                configured = (self.config_manager.config or {}).get(key)
                if type(configured) is int and 0 <= configured < 2**32:
                    trusted.add(configured)
                elif (
                    isinstance(configured, str)
                    and configured.isdecimal()
                    and len(configured) <= 10
                ):
                    numeric = int(configured)
                    if numeric < 2**32:
                        trusted.add(numeric)
            try:
                for index, part in enumerate(relative.parts):
                    try:
                        child = os.open(
                            part,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor,
                        )
                    except FileNotFoundError:
                        yield None
                        return
                    os.close(descriptor)
                    descriptor = child
                    current = os.fstat(descriptor)
                    wrapper = (
                        relative.parts[:2] == ("migrations", "infinidysk-v2")
                        and index < 2
                    )
                    if wrapper:
                        # The migration controller chmods only jobs/previews. Its
                        # mkdir(parents=True) intermediates may be ordinary
                        # application-owned directories below the trusted anchor.
                        mode = stat.S_IMODE(current.st_mode)
                        if (
                            current.st_uid not in trusted_uids
                            or mode & 0o002
                            or (mode & 0o020 and current.st_gid not in trusted_gids)
                        ):
                            raise BackupStorageError(
                                "Retained migration ancestry has an untrusted owner or writer."
                            )
                    elif (
                        current.st_uid != os.geteuid()
                        or stat.S_IMODE(current.st_mode) & 0o077
                    ):
                        raise BackupStorageError(
                            "Retained recovery ancestry is not private and controller-owned."
                        )
                value = os.fstat(descriptor)
                if stat.S_IMODE(value.st_mode) & 0o077 or value.st_uid != os.geteuid():
                    raise BackupStorageError(
                        "Retained recovery directories must remain private and controller-owned."
                    )
                if require_local_path(path) != mount:
                    raise BackupStorageError(
                        "The recovery reference filesystem changed."
                    )
                yield descriptor
                if require_local_path(path) != mount:
                    raise BackupStorageError(
                        "The recovery reference filesystem changed."
                    )
            finally:
                os.close(descriptor)

        def read(path):
            with directory(path.parent) as parent_fd:
                if parent_fd is None:
                    return None
                try:
                    descriptor = os.open(
                        path.name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=parent_fd,
                    )
                except FileNotFoundError:
                    return None
                with os.fdopen(descriptor, "rb") as handle:
                    value = os.fstat(handle.fileno())
                    _private_file(value)
                    if (
                        value.st_uid != os.geteuid()
                        or value.st_size > MAX_RECEIPT_BYTES
                    ):
                        raise BackupStorageError(
                            "A retained recovery reference is unsafe."
                        )
                    budget[0] += 1
                    budget[1] += value.st_size
                    if budget[0] > 2048 or budget[1] > 16 * 1024 * 1024:
                        raise BackupStorageError(
                            "Recovery reference inventory exceeds its bound."
                        )
                    raw = handle.read(MAX_RECEIPT_BYTES + 1)
                    if len(raw) > MAX_RECEIPT_BYTES or _file_stamp(
                        value
                    ) != _file_stamp(os.fstat(handle.fileno())):
                        raise BackupStorageError(
                            "A retained recovery reference changed."
                        )
                    current = os.stat(
                        path.name, dir_fd=parent_fd, follow_symlinks=False
                    )
                    if _file_stamp(current) != _file_stamp(value):
                        raise BackupStorageError(
                            "A retained recovery reference was replaced."
                        )
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise BackupStorageError("A retained recovery reference is invalid.")
            return result

        def records(parent):
            with directory(parent) as descriptor:
                if descriptor is None:
                    return
                stamp = _file_stamp(os.fstat(descriptor))
                names = os.listdir(descriptor)
                if len(names) > 2048:
                    raise BackupStorageError(
                        "Recovery reference inventory exceeds its bound."
                    )
                for name in sorted(names):
                    if name.endswith(".json"):
                        record = read(parent / name)
                        if record is None:
                            raise BackupStorageError(
                                "A retained recovery reference disappeared."
                            )
                        yield parent / name, record
                if _file_stamp(os.fstat(descriptor)) != stamp:
                    raise BackupStorageError(
                        "The retained recovery inventory changed during review."
                    )

        parent = self.state_root.parent
        installation = parent / "installation-backups"
        schedule = read(installation / "schedule.json")
        if (
            schedule
            and schedule.get("enabled")
            and self._references_profile(schedule, profile)
        ):
            usage.append(
                {
                    "kind": "installation_schedule",
                    "label": "An enabled installation backup schedule selects this destination.",
                }
            )
        active = read(installation / "active.json")
        active_id = (active or {}).get("job_id")
        if active is not None and (
            not isinstance(active_id, str) or not _ARTIFACT.fullmatch(active_id)
        ):
            raise BackupStorageError(
                "The active installation recovery reference is invalid."
            )
        found_active = False
        for path, job in records(installation / "jobs"):
            if (
                not _ARTIFACT.fullmatch(path.stem)
                or job.get("job_id") != path.stem
                or job.get("job_type")
                not in {"installation_backup", "installation_restore"}
                or job.get("status")
                not in {
                    "queued",
                    "running",
                    "completed",
                    "failed",
                    "recovery_required",
                    "rolled_back",
                }
                or not isinstance(job.get("plan"), dict)
            ):
                raise BackupStorageError("A retained installation job is invalid.")
            plan = job["plan"]
            if job["job_type"] == "installation_backup":
                destination = plan.get("destination")
                if not isinstance(destination, dict) or not (
                    isinstance(destination.get("destination_id"), str)
                    and _ID.fullmatch(destination["destination_id"])
                ):
                    raise BackupStorageError(
                        "A retained backup destination is invalid."
                    )
            else:
                source = plan.get("source")
                if not isinstance(source, dict):
                    raise BackupStorageError("A retained restore source is invalid.")
                if source.get("reference"):
                    parse_backup_reference(source["reference"])
                elif (
                    not isinstance(source.get("archive_path"), str)
                    or not Path(source["archive_path"]).is_absolute()
                ):
                    raise BackupStorageError("A retained restore source is invalid.")
            selected = {key: plan.get(key) for key in ("destination", "source")}
            is_active = path.stem == active_id
            found_active |= is_active
            retained = (
                is_active
                or job.get("status") in {"queued", "running", "recovery_required"}
                or bool(job.get("can_rollback") or job.get("restart_required"))
            )
            if retained and self._references_profile(selected, profile):
                usage.append(
                    {
                        "kind": "installation_recovery",
                        "label": "An active installation backup or retained restore recovery still requires this destination.",
                    }
                )
        if active_id and not found_active:
            raise BackupStorageError(
                "The active installation recovery job is unavailable."
            )
        major = parent / "migrations" / "infinidysk-v2"
        active_states = {
            "queued",
            "running",
            "quiescing",
            "snapshotting",
            "rehearsing",
            "importing",
            "validating",
            "cutover",
            "rolling_back",
            "interrupted",
            "recovery_required",
        }
        for _path, job in records(major / "jobs"):
            if (
                not _ARTIFACT.fullmatch(_path.stem)
                or job.get("job_id") != _path.stem
                or job.get("job_type") != "infinidysk_v1_to_v2"
                or job.get("status")
                not in active_states
                | {
                    "completed",
                    "rehearsed",
                    "failed",
                    "failed_restored",
                    "cancelled",
                    "rolled_back",
                }
                or ("state" in job and job["state"] != job.get("status"))
                or not isinstance(job.get("plan"), dict)
                or job.get("mode") not in {"cutover", "rehearsal"}
            ):
                raise BackupStorageError("A retained migration job is invalid.")
            plan = job["plan"]
            if not isinstance(plan.get("options"), dict):
                raise BackupStorageError("A retained migration plan is invalid.")
            destination = plan.get("backup_storage")
            if destination is not None and (
                not isinstance(destination, dict)
                or not (
                    isinstance(destination.get("destination_id"), str)
                    and _ID.fullmatch(destination["destination_id"])
                )
            ):
                raise BackupStorageError("A retained migration destination is invalid.")
            if job.get("snapshot") is not None and not isinstance(
                job["snapshot"], dict
            ):
                raise BackupStorageError("A retained migration snapshot is invalid.")
            if job.get("cleanup") is not None and not isinstance(job["cleanup"], dict):
                raise BackupStorageError(
                    "A retained migration cleanup record is invalid."
                )
            snapshot = job.get("snapshot") or {}
            selected = {
                "backup_storage": plan.get("backup_storage"),
                "backup_destination_id": (plan.get("options") or {}).get(
                    "backup_destination_id"
                ),
                "cold_archive": snapshot.get("cold_archive"),
            }
            state = job["status"]
            cleanup = job.get("cleanup") or {}
            cleanup_pending = cleanup.get("state") in {
                "verifying",
                "retiring",
                "deleting",
            } or bool(cleanup.get("resumable"))
            retained = (
                state in active_states
                or cleanup_pending
                or (
                    bool(snapshot)
                    and job.get("mode") == "cutover"
                    and state == "completed"
                    and job.get("rollback_retired") is not True
                )
            )
            if retained and self._references_profile(selected, profile):
                usage.append(
                    {
                        "kind": "migration_recovery",
                        "label": "An active migration, retained rollback or unfinished v1 cleanup still requires this destination.",
                    }
                )
        for _path, preview in records(major / "previews"):
            expires = preview.get("expires_at")
            if (
                type(expires) not in (int, float)
                or not 0 <= expires <= 253402300799
                or not isinstance(preview.get("plan"), dict)
            ):
                raise BackupStorageError("A retained migration preview is invalid.")
            if (
                isinstance(preview.get("expires_at"), (int, float))
                and preview["expires_at"] > time.time()
            ):
                plan = preview["plan"]
                if not isinstance(plan.get("options"), dict):
                    raise BackupStorageError(
                        "A retained migration preview plan is invalid."
                    )
                destination = plan.get("backup_storage")
                if destination is not None and (
                    not isinstance(destination, dict)
                    or not (
                        isinstance(destination.get("destination_id"), str)
                        and _ID.fullmatch(destination["destination_id"])
                    )
                ):
                    raise BackupStorageError(
                        "A retained migration preview destination is invalid."
                    )
                choice = plan["options"].get("backup_destination_id")
                if choice not in (None, "", "@default") and (
                    not isinstance(choice, str) or not _ID.fullmatch(choice)
                ):
                    raise BackupStorageError(
                        "A retained migration preview selection is invalid."
                    )
                selected = {
                    "backup_storage": plan.get("backup_storage"),
                    "backup_destination_id": (plan.get("options") or {}).get(
                        "backup_destination_id"
                    ),
                }
                if self._references_profile(selected, profile):
                    usage.append(
                        {
                            "kind": "migration_preview",
                            "label": "An unexpired migration preview selects this destination; let it expire or review a different destination.",
                        }
                    )
        return usage

    def _profile_usage(self, profile):
        usage = []
        if self._config().get("default_destination_id") == profile:
            usage.append(
                {
                    "kind": "global_default",
                    "label": "Clear or change the global default before removing or disabling this destination.",
                }
            )
        config = {
            key: value
            for key, value in (self.config_manager.config or {}).items()
            if key != "backup_storage"
        }
        try:
            referenced = self._references_profile(config, profile)
        except BackupStorageError:
            usage.append(
                {
                    "kind": "configured_reference_unavailable",
                    "label": "A configured backup reference is malformed; review configured selections before removing or disabling this destination.",
                }
            )
        else:
            if referenced:
                usage.append(
                    {
                        "kind": "configured_consumer",
                        "label": "A configured backup or migration selection still uses this destination; change that selection first.",
                    }
                )
        with self._lock:
            if self._active.get(profile):
                usage.append(
                    {
                        "kind": "active_artifact",
                        "label": "An active backup, download or restore still owns this destination.",
                    }
                )
        try:
            usage.extend(self._recovery_usage(profile))
        except (BackupStorageError, OSError, ValueError, TypeError, KeyError):
            usage.append(
                {
                    "kind": "recovery_unavailable",
                    "label": "Retained recovery references could not be inspected safely; preserve this registration and review local recovery state.",
                }
            )
        return list({item["kind"]: item for item in usage}.values())

    def update_profile(self, profile, request):
        profile, current = self._configured(profile)
        requested = self._requested_profile(request)
        if requested["id"] != profile or requested["path"] != current.get("path"):
            raise BackupStorageError(
                "An existing destination identifier and path cannot be changed; register a new destination instead."
            )
        record = self._read(self._binding_path(profile))
        if record and requested["path"] != record["binding"]["path"]:
            raise BackupStorageError(
                "The original registered storage path must be retained."
            )
        if not requested["enabled"]:
            usage = self._profile_usage(profile)
            if usage:
                raise BackupStorageError(usage[0]["label"])
        changed_policy = (
            _shared_permissions(current) != requested["allow_shared_permissions"]
        )
        enabling = requested["enabled"] and not current.get("enabled")
        if changed_policy or enabling or request.get("make_default") or not record:
            return self.save_profile(request)
        # A label edit or an unused disable is local-only and works during an outage.
        with self._lock:
            if self._active.get(profile) or profile in self._registering:
                raise BackupStorageError(
                    "An active backup still owns this destination."
                )
            if self._configured(profile)[1] != current:
                raise BackupStorageError(
                    "The registration changed; refresh before editing it."
                )
            saved = {
                key: requested[key]
                for key in ("name", "path", "enabled", "allow_shared_permissions")
            }
            self.config_manager.config.setdefault("backup_storage", {}).setdefault(
                "profiles", {}
            )[profile] = saved
            self.config_manager.save_config()
            public = {
                **record.get("public", {}),
                **requested,
                "registered": True,
                "ready": bool(requested["enabled"]),
            }
            record["public"] = public
            self._write(self._binding_path(profile), record)
            self._last_checks.pop(profile, None)
        return {"protocol": PROTOCOL, "profile": public}

    def remove_profile(self, profile):
        profile, current = self._configured(profile)
        with self._lock:
            if self._active.get(profile) or profile in self._registering:
                raise BackupStorageError(
                    "An active backup still owns this destination."
                )
            self._registering.add(profile)
        try:
            usage = self._profile_usage(profile)
            if usage:
                raise BackupStorageError(usage[0]["label"])
            with self._lock:
                if self._configured(profile)[1] != current:
                    raise BackupStorageError(
                        "The registration changed; refresh before removing it."
                    )
                # Recheck local owners immediately before changing configuration.
                usage = self._profile_usage(profile)
                if usage:
                    raise BackupStorageError(usage[0]["label"])
                config = self.config_manager.config.setdefault("backup_storage", {})
                for key in ("profiles", "instances"):
                    if isinstance(config.get(key), dict):
                        config[key].pop(profile, None)
                self.config_manager.save_config()
                self._last_checks.pop(profile, None)
                # Explicit unused removal ends this registration's runtime lease.
                # Durable ownership/receipts continue reserving the same ID/root.
                self._runtime_bindings.pop(profile, None)
                self._revalidation_reviews = {
                    token: review
                    for token, review in self._revalidation_reviews.items()
                    if review["profile"] != profile
                }
            return {
                "protocol": PROTOCOL,
                "destination_id": profile,
                "removed": True,
                "archives_preserved": True,
                "receipts_preserved": True,
            }
        finally:
            with self._lock:
                self._registering.discard(profile)

    def _safe_descriptor(self, inspected):
        root = inspected["root_identity"]
        return {
            key: inspected[key]
            for key in ("path", "filesystem_type", "storage_type", "mount_signature")
        } | {
            "root_inode": root["inode"],
            "root_mode": f"{stat.S_IMODE(root['mode']):04o}",
            "root_device": root.get("device"),
        }

    def preview_revalidation(self, profile):
        profile, _config = self._configured(profile)
        try:
            profile, config, binding, inspected, _owned = self._validate(
                profile, review_runtime=True
            )
            with self._lock:
                runtime = self._runtime_bindings.get(profile)
                required = runtime is not None and runtime != inspected
                if self._active.get(profile) or profile in self._registering:
                    raise BackupStorageError(
                        "An active backup still owns this destination."
                    )
                token = secrets.token_hex(32)
                expires = int(time.time()) + 300
                self._revalidation_reviews = {
                    key: value
                    for key, value in self._revalidation_reviews.items()
                    if value["expires_at"] > time.time()
                }
                if len(self._revalidation_reviews) >= 32:
                    raise BackupStorageError(
                        "Too many storage reviews are pending; wait for an earlier review to expire."
                    )
                self._revalidation_reviews[token] = {
                    "profile": profile,
                    "config": config,
                    "binding": copy.deepcopy(binding),
                    "inspected": inspected,
                    "expires_at": expires,
                }
            changes = []
            original = runtime or binding
            if original.get("root_identity", {}).get("device") != inspected[
                "root_identity"
            ].get("device"):
                changes.append("device_number")
            if runtime and runtime.get("mount_id") != inspected.get("mount_id"):
                changes.append("mount_id")
            return {
                "protocol": PROTOCOL,
                "ready": True,
                "token": token,
                "expires_at": expires,
                "revalidation_required": required,
                "registered": self._safe_descriptor(binding),
                "observed": self._safe_descriptor(inspected),
                "changes": changes,
                "blockers": [],
            }
        except (BackupStorageError, OSError, ValueError) as error:
            message = (
                str(error)
                if isinstance(error, BackupStorageError)
                else "The existing mounted storage could not be verified safely."
            )
            return {
                "protocol": PROTOCOL,
                "ready": False,
                "revalidation_required": False,
                "blockers": [message],
            }

    def revalidate_profile(self, profile, request):
        profile = self._id(profile)
        token = request.get("token")
        if request.get("confirm") is not True or not isinstance(token, str):
            raise BackupStorageError(
                "Review the current mount and explicitly confirm revalidation."
            )
        with self._lock:
            review = self._revalidation_reviews.pop(token, None)
            if (
                not review
                or review["profile"] != profile
                or review["expires_at"] <= time.time()
            ):
                raise BackupStorageError(
                    "The storage review expired or changed; review this destination again."
                )
            if self._active.get(profile) or profile in self._registering:
                raise BackupStorageError(
                    "An active backup still owns this destination."
                )
            self._registering.add(profile)
        try:
            _profile, config, binding, inspected, _owned = self._validate(
                profile, review_runtime=True
            )
            if (
                config != review["config"]
                or binding != review["binding"]
                or inspected != review["inspected"]
            ):
                raise BackupStorageError(
                    "The reviewed storage or configuration changed; generate a fresh review."
                )
            self._pin_runtime(profile, inspected, refresh=True)
        finally:
            with self._lock:
                self._registering.discard(profile)
        return self.check_profile(profile)

    def check_profile(self, profile):
        profile, config = self._configured(profile)
        checked = int(time.time())
        blockers = []
        try:
            self._validate(profile)
        except (BackupStorageError, OSError, ValueError) as error:
            blockers.append(
                str(error)
                if isinstance(error, BackupStorageError)
                else "The existing mounted storage could not be verified safely."
            )
        required = False
        if blockers:
            try:
                _profile, _config, _binding, inspected, _owned = self._validate(
                    profile, review_runtime=True
                )
                with self._lock:
                    runtime = self._runtime_bindings.get(profile)
                    required = runtime is not None and runtime != inspected
            except (BackupStorageError, OSError, ValueError):
                pass
        status = {
            "ready": not blockers,
            "blockers": blockers,
            "live_checked": True,
            "validation_state": "blocked" if blockers else "ready",
            "revalidation_required": required,
            "checked_at": checked,
            "config_fingerprint": _digest(config),
        }
        with self._lock:
            self._last_checks[profile] = status
        public = next(
            row for row in self.profiles()["destinations"] if row["id"] == profile
        )
        return {
            "protocol": PROTOCOL,
            "profile": public,
            "checked_at": checked,
            "usage": public["usage"],
        }

    def set_default(self, profile):
        selected = self._id(profile) if profile else ""
        if selected:
            with self._lock:
                if selected in self._registering:
                    raise BackupStorageError(
                        "The destination registration is being changed."
                    )
                self._registering.add(selected)
            try:
                _profile, config, _binding, _inspected, _owned = self._validate(
                    selected, review_runtime=True
                )
                if not config.get("enabled"):
                    raise BackupStorageError(
                        "A disabled destination cannot be the default."
                    )
                # _validate in review mode does not alter the runtime pin.
                with self._lock:
                    if self._configured(selected)[1] != config:
                        raise BackupStorageError(
                            "The destination changed while selecting its default."
                        )
                    self._pin_runtime(selected, _inspected)
                    self.config_manager.config.setdefault("backup_storage", {})[
                        "default_destination_id"
                    ] = selected
                    self.config_manager.save_config()
            finally:
                with self._lock:
                    self._registering.discard(selected)
        else:
            with self._lock:
                self.config_manager.config.setdefault("backup_storage", {})[
                    "default_destination_id"
                ] = ""
                self.config_manager.save_config()
        return {"protocol": PROTOCOL, "default_destination_id": selected}

    def _pin_runtime(self, profile, inspected, *, refresh=False):
        with self._lock:
            previous = self._runtime_bindings.get(profile)
            if previous is not None and previous != inspected and not refresh:
                raise BackupStorageError(
                    "The destination mount changed since this controller verified it; "
                    "review and revalidate the existing storage before use. "
                    "No local fallback is allowed."
                )
            self._runtime_bindings[profile] = copy.deepcopy(inspected)

    def _validate(self, profile, *, review_runtime=False):
        profile, config = (
            self._configured(profile) if review_runtime else self._profile(profile)
        )
        record = self._read(self._binding_path(profile))
        if not record:
            raise BackupStorageError(
                "Register and verify the selected destination first."
            )
        binding = record["binding"]
        allowed = _record_permissions(record)
        if _shared_permissions(config) != allowed:
            raise BackupStorageError(
                "The storage permission policy changed; register and verify this destination again."
            )
        inspected = self._inspect(config.get("path"))
        if _durable_inspection(inspected) != _durable_inspection(binding):
            raise BackupStorageError(
                "The selected mounted destination is missing or replaced; no local fallback is allowed."
            )
        with self._lock:
            if profile in self._registering and not review_runtime:
                raise BackupStorageError(
                    "The destination registration is being changed."
                )
            runtime = self._runtime_bindings.get(profile)
            if runtime is not None and runtime != inspected and not review_runtime:
                raise BackupStorageError(
                    "The destination mount changed since this controller verified it; "
                    "review and revalidate the existing storage before use. "
                    "No local fallback is allowed."
                )
        owned = Path(binding["path"]) / ".dumb-backups" / profile
        with self._owned_directory(
            binding, profile, allow_shared_permissions=allowed
        ) as descriptor:
            marker_fd = os.open(
                ".destination.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=descriptor,
            )
            with os.fdopen(marker_fd, "r") as handle:
                _external_permissions(
                    os.fstat(handle.fileno()), allow_shared_permissions=allowed
                )
                raw = handle.read(4097)
                if (
                    len(raw) > 4096
                    or json.loads(raw).get("marker") != binding["marker"]
                ):
                    raise BackupStorageError(
                        "The destination ownership marker changed."
                    )
        if self._inspect(config.get("path")) != inspected:
            raise BackupStorageError(
                "The destination mount changed during validation; no fallback was created."
            )
        if not review_runtime:
            self._pin_runtime(profile, inspected)
        return profile, config, binding, inspected, owned

    @contextmanager
    def _owned_directory(self, binding, profile, *, allow_shared_permissions):
        allowed = allow_shared_permissions
        with _directory(binding["path"]) as root:
            with _external_child(
                root, ".dumb-backups", allow_shared_permissions=allowed
            ) as base:
                with _external_child(
                    base, profile, allow_shared_permissions=allowed
                ) as descriptor:
                    yield descriptor

    def bound_identity(self, profile):
        profile, config, binding, _inspected, owned = self._validate(profile)
        return {
            "destination_id": profile,
            "name": config.get("name") or profile,
            "path": binding["path"],
            "artifact_root": str(owned),
            "storage_type": binding["storage_type"],
            "filesystem_type": binding["filesystem_type"],
            "binding_fingerprint": _digest(binding),
            "allow_shared_permissions": _shared_permissions(config),
        }

    @contextmanager
    def _lease(self, profile, namespace, *, create=False):
        if not _NAMESPACE.fullmatch(str(namespace)):
            raise BackupStorageError("The artifact namespace is invalid.")
        profile = self._id(profile)
        with self._lock:
            if profile in self._registering:
                raise BackupStorageError("The destination is being registered.")
            self._active[profile] = self._active.get(profile, 0) + 1
        try:
            profile, config, binding, inspected, owned = self._validate(profile)

            def check():
                current = self._inspect(binding["path"])
                if current != inspected:
                    raise BackupStorageError(
                        "The destination mount changed; publication is blocked and no local fallback was created."
                    )

            check()
            allowed = _shared_permissions(config)
            with self._owned_directory(
                binding, profile, allow_shared_permissions=allowed
            ) as profile_fd:
                check()
                with _external_child(
                    profile_fd,
                    namespace,
                    create=create,
                    allow_shared_permissions=allowed,
                ) as descriptor:
                    check()
                    yield profile, descriptor, check, allowed
                    check()
        finally:
            with self._lock:
                self._active[profile] -= 1

    def _catalog(self, profile, namespace, artifact):
        return self._state("artifacts", profile, namespace, f"{artifact}.json")

    def _public_receipt(self, receipt):
        return {
            key: receipt[key]
            for key in (
                "artifact_id",
                "reference",
                "backup_destination_id",
                "namespace",
                "created_at",
                "size_bytes",
                "sha256",
                "verified",
                "extension",
            )
        } | {
            "artifact_kind": (receipt.get("metadata") or {}).get("kind"),
            "retention_scope": (receipt.get("metadata") or {}).get("retention_scope"),
            "allow_shared_permissions": _shared_permissions(receipt),
        }

    def write_file(
        self,
        profile,
        namespace,
        producer,
        *,
        verifier=None,
        extension="tar",
        metadata=None,
        owner_check=None,
    ):
        if extension not in {"tar", "json"}:
            raise BackupStorageError("The closed artifact format is unsupported.")
        artifact = secrets.token_hex(16)
        temporary, filename = f".{artifact}.partial", f"{artifact}.{extension}"
        with self._lease(profile, namespace, create=True) as (
            resolved,
            directory_fd,
            lease_check,
            allowed,
        ):

            def check():
                lease_check()
                if owner_check is not None:
                    owner_check()

            check()
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    writer = _Writer(handle, check)
                    producer(writer)
                    handle.flush()
                    os.fsync(handle.fileno())
                    expected_hash, expected_size = (
                        writer.digest.hexdigest(),
                        writer.size,
                    )
                    _external_permissions(
                        os.fstat(handle.fileno()), allow_shared_permissions=allowed
                    )
                check()
                descriptor = os.open(
                    temporary,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
                with os.fdopen(descriptor, "rb") as handle:
                    _external_permissions(
                        os.fstat(handle.fileno()), allow_shared_permissions=allowed
                    )
                    reader = _Reader(handle, check)
                    verification = verifier(reader) if verifier is not None else None
                    while reader.read(1024 * 1024):
                        pass
                    if (
                        reader.digest.hexdigest() != expected_hash
                        or reader.size != expected_size
                    ):
                        raise BackupStorageError(
                            "The closed archive readback did not match its complete bytes."
                        )
                check()
                reservation = os.open(
                    filename,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                os.close(reservation)
                os.replace(
                    temporary,
                    filename,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                os.fsync(directory_fd)
                check()
                receipt = {
                    "artifact_id": artifact,
                    "reference": _reference(resolved, namespace, artifact),
                    "backup_destination_id": resolved,
                    "namespace": namespace,
                    "filename": filename,
                    "created_at": int(time.time()),
                    "size_bytes": expected_size,
                    "sha256": expected_hash,
                    "verified": True,
                    "extension": extension,
                    "metadata": metadata or {},
                    "verification": verification,
                    "allow_shared_permissions": allowed,
                }
                self._write(self._catalog(resolved, namespace, artifact), receipt)
                return self._public_receipt(receipt)
            finally:
                try:
                    lease_check()
                    os.unlink(temporary, dir_fd=directory_fd)
                except (FileNotFoundError, OSError, BackupStorageError):
                    pass

    def write_json(self, profile, namespace, payload, metadata=None):
        def producer(handle):
            size = 0
            for chunk in json.JSONEncoder(
                sort_keys=True, separators=(",", ":"), allow_nan=False
            ).iterencode(payload):
                data = chunk.encode()
                size += len(data)
                if size > MAX_JSON_BYTES:
                    raise BackupStorageError(
                        "The closed JSON artifact exceeds its bound."
                    )
                handle.write(data)

        return self.write_file(
            profile, namespace, producer, extension="json", metadata=metadata
        )

    @contextmanager
    def open_artifact(self, reference):
        profile, namespace, artifact = parse_backup_reference(reference)
        receipt = self._read(self._catalog(profile, namespace, artifact))
        if (
            not receipt
            or receipt.get("reference") != reference
            or not receipt.get("verified")
            or receipt.get("extension") not in {"json", "tar"}
            or receipt.get("filename") != f"{artifact}.{receipt.get('extension')}"
            or receipt.get("backup_destination_id") != profile
            or receipt.get("namespace") != namespace
        ):
            raise BackupStorageError("The owned backup receipt is unavailable.")
        with self._lease(profile, namespace) as (
            _resolved,
            directory_fd,
            check,
            allowed,
        ):
            descriptor = os.open(
                receipt["filename"],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
            with os.fdopen(descriptor, "rb") as handle:
                value = os.fstat(handle.fileno())
                _external_permissions(value, allow_shared_permissions=allowed)
                reader = _Reader(handle, check)
                while reader.read(1024 * 1024):
                    pass
                if (
                    value.st_size != receipt["size_bytes"]
                    or reader.digest.hexdigest() != receipt["sha256"]
                    or _file_stamp(value) != _file_stamp(os.fstat(handle.fileno()))
                ):
                    raise BackupStorageError(
                        "The owned backup artifact changed or did not verify."
                    )
                handle.seek(0)
                yield handle
                check()
                if _file_stamp(value) != _file_stamp(os.fstat(handle.fileno())):
                    raise BackupStorageError(
                        "The artifact changed while it was consumed."
                    )

    def read_json(self, reference, *, expected_kind=None):
        profile, namespace, artifact = parse_backup_reference(reference)
        receipt = self._read(self._catalog(profile, namespace, artifact))
        if (
            not receipt
            or receipt.get("extension") != "json"
            or receipt.get("size_bytes", MAX_JSON_BYTES + 1) > MAX_JSON_BYTES
        ):
            raise BackupStorageError(
                "The reference does not identify a bounded JSON artifact."
            )
        if (
            expected_kind is not None
            and (receipt.get("metadata") or {}).get("kind") != expected_kind
        ):
            raise BackupStorageError(
                "The owned artifact kind does not match this operation."
            )
        with self.open_artifact(reference) as handle:
            raw = handle.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                raise BackupStorageError(
                    "The JSON artifact exceeds its supported bound."
                )
            return json.loads(raw)

    def list_artifacts(self, profile, namespace):
        profile = self._id(profile)
        if not _NAMESPACE.fullmatch(str(namespace)):
            raise BackupStorageError("The artifact namespace is invalid.")
        self._validate(profile)
        parent = self._state("artifacts", profile, namespace, "placeholder.json").parent
        receipts = []
        for path in parent.glob("*.json"):
            record = self._read(path)
            if (
                record
                and record.get("namespace") == namespace
                and record.get("backup_destination_id") == profile
            ):
                receipts.append(self._public_receipt(record))
        return sorted(receipts, key=lambda row: (row["created_at"], row["artifact_id"]))

    def delete_artifact(self, reference):
        profile, namespace, artifact = parse_backup_reference(reference)
        with self.open_artifact(reference) as handle:
            expected = _file_stamp(os.fstat(handle.fileno()))
        receipt = self._read(self._catalog(profile, namespace, artifact))
        with self._lease(profile, namespace) as (
            _resolved,
            descriptor,
            check,
            _allowed,
        ):
            actual = os.stat(
                receipt["filename"], dir_fd=descriptor, follow_symlinks=False
            )
            if _file_stamp(actual) != expected:
                raise BackupStorageError(
                    "The artifact changed before retention deletion."
                )
            check()
            os.unlink(receipt["filename"], dir_fd=descriptor)
            os.fsync(descriptor)
        self._catalog(profile, namespace, artifact).unlink()
        return {"deleted": True, "reference": reference}


def get_backup_storage(config_manager=None):
    if config_manager is None:
        from utils.config_loader import CONFIG_MANAGER

        config_manager = CONFIG_MANAGER
    with _CACHE_LOCK:
        key = id(config_manager)
        if key not in _CACHE:
            _CACHE[key] = BackupStorage(config_manager)
        return _CACHE[key]
