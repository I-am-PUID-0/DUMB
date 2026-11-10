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
def _child_directory(parent, name, *, create=False):
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
        if stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o077:
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
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
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
        }

    def preview(self, request):
        value = self._requested_profile(request)
        blockers = []
        try:
            inspected = self._inspect(value["path"])
            value.update(
                {key: inspected[key] for key in ("filesystem_type", "storage_type")}
            )
        except (BackupStorageError, OSError):
            blockers.append(
                "The existing container-mounted destination could not be inspected safely; no directory was created."
            )
        value.update(
            registered=False, ready=not blockers and value["enabled"], blockers=blockers
        )
        return {
            "protocol": PROTOCOL,
            "ready": value["ready"],
            "profile": value,
            "blockers": blockers,
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
            }
            matches = bool(record) and all(
                current[key] == status.get(key) for key in current
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
                    "blockers": (
                        []
                        if matches
                        else ["Register and verify this destination before use."]
                    ),
                }
            )
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
                if previous and (
                    previous["binding"]["path"] != inspected["path"]
                    or previous["binding"]["mount_signature"]
                    != inspected["mount_signature"]
                ):
                    raise BackupStorageError(
                        "Use a new identifier for a changed backup root or mounted filesystem."
                    )
                with _directory(inspected["path"]) as root_fd:
                    if self._inspect(public["path"]) != inspected:
                        raise BackupStorageError(
                            "The destination mount changed before registration."
                        )
                    marker = self._register_marker(
                        root_fd, profile, previous, inspected, public
                    )
                if self._inspect(public["path"]) != inspected:
                    raise BackupStorageError(
                        "The destination mount changed during registration."
                    )
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
                public.update(
                    {key: inspected[key] for key in ("filesystem_type", "storage_type")}
                )
                public.update(registered=True, ready=True, blockers=[])
                # Retain proof of the newly created owned marker even if a raw
                # configuration edit races the final configuration publication.
                self._write(
                    binding_path,
                    {
                        "binding": binding,
                        "public": public,
                        "checked_at": int(time.time()),
                    },
                )
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
                    key: public[key] for key in ("name", "path", "enabled")
                }
                if request.get("make_default"):
                    config["default_destination_id"] = profile
                self.config_manager.save_config()
            return {"protocol": PROTOCOL, "profile": public}
        finally:
            with self._lock:
                self._registering.discard(profile)

    def _register_marker(self, root_fd, profile, previous, inspected, public):
        with _child_directory(root_fd, ".dumb-backups", create=True) as base_fd:
            if self._inspect(public["path"]) != inspected:
                raise BackupStorageError(
                    "The destination mount changed during registration."
                )
            with _child_directory(base_fd, profile, create=True) as descriptor:
                try:
                    marker_fd = os.open(
                        ".destination.json",
                        os.O_RDONLY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    if previous:
                        raise BackupStorageError(
                            "The registered storage identity is missing; no fallback identity was created."
                        ) from None
                    marker = secrets.token_hex(32)
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
                        _private_file(os.fstat(handle.fileno()))
                else:
                    with os.fdopen(marker_fd, "r") as handle:
                        _private_file(os.fstat(handle.fileno()))
                        raw = handle.read(4097)
                    if len(raw) > 4096:
                        raise BackupStorageError(
                            "The storage marker exceeds its bound."
                        )
                    marker = json.loads(raw).get("marker")
                    if not previous or marker != previous["binding"]["marker"]:
                        raise BackupStorageError(
                            "The destination contains an unowned storage identity."
                        )
                os.fsync(descriptor)
                return marker

    def set_default(self, profile):
        if profile:
            self.bound_identity(profile)
            profile = self._id(profile)
        self.config_manager.config.setdefault("backup_storage", {})[
            "default_destination_id"
        ] = (profile or "")
        self.config_manager.save_config()
        return {"protocol": PROTOCOL, "default_destination_id": profile or ""}

    def _validate(self, profile):
        profile, config = self._profile(profile)
        record = self._read(self._binding_path(profile))
        if not record:
            raise BackupStorageError(
                "Register and verify the selected destination first."
            )
        binding = record["binding"]
        inspected = self._inspect(config.get("path"))
        if any(
            inspected[key] != binding[key]
            for key in (
                "path",
                "root_identity",
                "mount_signature",
                "filesystem_type",
                "storage_type",
            )
        ):
            raise BackupStorageError(
                "The selected mounted destination is missing or replaced; no local fallback is allowed."
            )
        owned = Path(binding["path"]) / ".dumb-backups" / profile
        with _directory(owned, private=True) as descriptor:
            marker_fd = os.open(
                ".destination.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            with os.fdopen(marker_fd, "r") as handle:
                _private_file(os.fstat(handle.fileno()))
                raw = handle.read(4097)
                if (
                    len(raw) > 4096
                    or json.loads(raw).get("marker") != binding["marker"]
                ):
                    raise BackupStorageError(
                        "The destination ownership marker changed."
                    )
        return profile, config, binding, inspected, owned

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
            profile, _config, binding, inspected, owned = self._validate(profile)

            def check():
                current = self._inspect(binding["path"])
                if current != inspected:
                    raise BackupStorageError(
                        "The destination mount changed; publication is blocked and no local fallback was created."
                    )

            check()
            with _directory(owned, private=True) as profile_fd:
                check()
                with _child_directory(
                    profile_fd, namespace, create=create
                ) as descriptor:
                    check()
                    yield profile, descriptor, check
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
                    _private_file(os.fstat(handle.fileno()))
                check()
                descriptor = os.open(
                    temporary, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd
                )
                with os.fdopen(descriptor, "rb") as handle:
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
        with self._lease(profile, namespace) as (_resolved, directory_fd, check):
            descriptor = os.open(
                receipt["filename"], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd
            )
            with os.fdopen(descriptor, "rb") as handle:
                value = os.fstat(handle.fileno())
                _private_file(value)
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
        with self._lease(profile, namespace) as (_resolved, descriptor, check):
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
