"""Private accepted-release state, separate from mutable service configuration."""

from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from utils.infinidysk_v2_releases import compare_releases
from utils.infinidysk_v2_runtime import (
    MARKER,
    resolve_v2_data_dir,
    runtime_dir,
)
from utils.private_files import atomic_write_private_text, read_private_text

_FORMAT = "native-installed-release-v1"
_FIELDS = {"format", "runtime", "data_dir", "release_metadata", "origin"}
_REVIEW = (
    "The saved InfiniDysk installed-release record needs review. "
    "Keep the current data and runtime-update evidence; no other release was installed."
)


class InstalledReleaseError(ValueError):
    """Accepted artifact or explicit installation scope cannot be verified."""


def _scope(service, state_root, data_root):
    from utils.infinidysk_v2_update import _direct

    if not isinstance(service, dict) or not isinstance(data_root, (str, os.PathLike)):
        raise InstalledReleaseError(_REVIEW)
    _direct(data_root)
    runtime = _direct(runtime_dir(service))
    data = _direct(resolve_v2_data_dir(service, data_root))
    root = _direct(state_root)
    if (
        root == runtime
        or runtime in root.parents
        or root == data
        or data in root.parents
    ):
        raise InstalledReleaseError(
            "Installed-release state must remain outside the native runtime and data directory."
        )
    key = hashlib.sha256(os.fsencode(runtime)).hexdigest()[:24]
    return runtime, data, root / f"installed-release-{key}.json"


def _private_root(root):
    value = root.lstat()
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid not in {os.geteuid(), root.parent.stat().st_uid}
        or stat.S_IMODE(value.st_mode) & 0o077
    ):
        raise InstalledReleaseError(_REVIEW)
    return value


def _root_stamp(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
    )


def _same_root(root, expected):
    from utils.infinidysk_v2_update import _direct

    _direct(root)
    if _root_stamp(_private_root(root)) != expected:
        raise InstalledReleaseError(_REVIEW)


@contextmanager
def _installed_lock(path):
    """Serialize accepted-state writers without changing active update locking."""
    from utils.infinidysk_v2_update import _direct

    root = path.parent
    root.parent.mkdir(parents=True, exist_ok=True)
    parent = root.parent.stat()
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    else:
        if os.geteuid() == 0:
            os.chown(root, parent.st_uid, parent.st_gid)
    _direct(root)
    owner = _private_root(root)
    stamp = _root_stamp(owner)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        if _root_stamp(os.fstat(root_fd)) != stamp:
            raise InstalledReleaseError(_REVIEW)
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        name = path.with_suffix(".lock").name
        try:
            descriptor = os.open(
                name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root_fd
            )
        except FileExistsError:
            descriptor = os.open(name, flags, dir_fd=root_fd)
        else:
            if os.geteuid() == 0:
                os.fchown(descriptor, owner.st_uid, owner.st_gid)
        locked = os.fstat(descriptor)
        if (
            not stat.S_ISREG(locked.st_mode)
            or locked.st_nlink != 1
            or locked.st_uid not in {os.geteuid(), owner.st_uid}
            or stat.S_IMODE(locked.st_mode) & 0o077
        ):
            raise InstalledReleaseError(_REVIEW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstalledReleaseError(
                "Another native installed-release writer owns accepted state."
            ) from None
        named = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if _root_stamp(named) != _root_stamp(locked) or named.st_nlink != 1:
            raise InstalledReleaseError(_REVIEW)
        _same_root(root, stamp)
        yield owner, stamp
        _same_root(root, stamp)
        named = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if _root_stamp(named) != _root_stamp(locked) or named.st_nlink != 1:
            raise InstalledReleaseError(_REVIEW)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(root_fd)


def _load(path, runtime, data):
    from utils.infinidysk_v2_update import _metadata

    try:
        _private_root(path.parent)
        record = json.loads(read_private_text(path, max_bytes=32768))
    except FileNotFoundError:
        return None
    if (
        not isinstance(record, dict)
        or set(record) != _FIELDS
        or record.get("format") != _FORMAT
        or record.get("runtime") != str(runtime)
        or record.get("data_dir") != str(data)
        or not isinstance(record.get("origin"), str)
        or not re.fullmatch(r"[a-z_]{1,64}", record["origin"])
    ):
        raise InstalledReleaseError(_REVIEW)
    _metadata(record.get("release_metadata"))
    return record


def read_installed_release(service, state_root, *, data_root="/data"):
    """Return accepted metadata without opening native data or changing state.

    An existing invalid record or different data scope is never interpreted as
    an absent installation, including when the program directory was ephemeral.
    """
    try:
        runtime, data, path = _scope(service, state_root, data_root)
        record = _load(path, runtime, data)
        return copy.deepcopy(record["release_metadata"]) if record else None
    except InstalledReleaseError:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise InstalledReleaseError(_REVIEW) from None


def _verify_artifact(service, runtime, metadata):
    from utils.infinidysk_v2_update import _artifact, _directory, _read

    marker = json.loads(_read(runtime / MARKER, 8192))
    if isinstance(marker, dict) and "format" not in marker:
        # The established migration artifact has a smaller provenance marker.
        # Normalize its verified metadata without modifying its program files.
        from utils.infinidysk_v2_runtime import installed_v2_metadata

        if installed_v2_metadata(service) != metadata:
            raise InstalledReleaseError(_REVIEW)
    else:
        _artifact(runtime, metadata, _directory(runtime))


def record_installed_release(
    service, metadata, state_root, *, data_root="/data", origin="existing_runtime"
):
    """Record only verified present bytes, preserving one explicit data scope.

    Callers own lifecycle admission. A later release may replace the accepted
    release after its verified publication; a different data directory, older
    release or changed same-version artifact cannot replace this authority.
    """
    from utils.infinidysk_v2_update import _metadata

    try:
        runtime, data, path = _scope(service, state_root, data_root)
        metadata = _metadata(metadata)
        if not isinstance(origin, str) or not re.fullmatch(r"[a-z_]{1,64}", origin):
            raise InstalledReleaseError(_REVIEW)
        with _installed_lock(path) as (owner, root_identity):
            previous = _load(path, runtime, data)
            _verify_artifact(service, runtime, metadata)
            if previous:
                accepted = previous["release_metadata"]
                if accepted == metadata:
                    return copy.deepcopy(previous)
                if compare_releases(metadata["release"], accepted["release"]) <= 0:
                    raise InstalledReleaseError(
                        "The saved installed release cannot be replaced by older or different same-version bytes."
                    )
            record = {
                "format": _FORMAT,
                "runtime": str(runtime),
                "data_dir": str(data),
                "release_metadata": metadata,
                "origin": origin,
            }
            _same_root(path.parent, root_identity)
            atomic_write_private_text(
                path,
                json.dumps(record, sort_keys=True),
                owner=(owner.st_uid, owner.st_gid),
            )
            _same_root(path.parent, root_identity)
            # Retain the accepted file through the containing-directory fsync.
            descriptor = os.open(
                path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return copy.deepcopy(record)
    except InstalledReleaseError:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise InstalledReleaseError(_REVIEW) from None
