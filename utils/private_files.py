"""Helpers for atomically writing application files that contain credentials."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def read_private_text(
    path: str | os.PathLike[str],
    *,
    encoding: str = "utf-8",
    owner_ids: tuple[int, ...] = (),
    max_bytes: int = 8 * 1024 * 1024,
) -> str:
    """Read one bounded private regular file without following its leaf link.

    Managed parent aliases remain supported. Existing files may legitimately be
    owned by the controller, their application directory owner, or an explicitly
    supplied service UID; unsafe records are errors, never missing ownership.
    """
    target = Path(path)
    try:
        parent_owner = target.parent.stat().st_uid
    except FileNotFoundError:
        if any(
            parent.is_symlink() and not parent.exists() for parent in target.parents
        ):
            raise PermissionError(
                "Ownership metadata has an unavailable managed path alias; existing ownership was not discarded."
            ) from None
        raise
    allowed = {os.geteuid(), parent_owner, *owner_ids}
    descriptor = os.open(
        target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid not in allowed
            or before.st_mode & 0o077
            or before.st_nlink != 1
            or before.st_size > max_bytes
        ):
            raise PermissionError(
                "Ownership metadata must be a bounded private regular file owned by its controller or configured service; existing records were preserved."
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
        if len(raw) > max_bytes or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_mode,
            before.st_uid,
            before.st_nlink,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_mode,
            after.st_uid,
            after.st_nlink,
        ):
            raise PermissionError(
                "Ownership metadata changed while it was read; existing records were preserved."
            )
        return raw.decode(encoding)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def atomic_write_private_text(
    path: str | os.PathLike[str],
    content: str,
    *,
    encoding: str = "utf-8",
    owner: tuple[int, int] | None = None,
) -> None:
    """Atomically replace ``path`` with a mode-0600 text file.

    An explicit owning service may be supplied before any secret is written.
    Otherwise the existing owner/group are retained so refreshes preserve access.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    if owner is None:
        try:
            stat_result = target.lstat()
            owner = (stat_result.st_uid, stat_result.st_gid)
        except FileNotFoundError:
            pass

    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        if owner is not None and owner != (os.geteuid(), os.getegid()):
            try:
                os.fchown(descriptor, *owner)
            except PermissionError:
                # An unprivileged writer may own the target but not be allowed
                # to restore its previous group. Mode 0600 depends only on the
                # retained owner in that case.
                if owner[0] != os.geteuid():
                    raise

        with os.fdopen(descriptor, "w", encoding=encoding) as handle:
            descriptor = -1
            # These files contain credentials required by their owning
            # applications. Atomic replacement, mode 0600, and retained
            # ownership are the security boundary for this at-rest data.
            # codeql[py/clear-text-storage-sensitive-data]
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        descriptor = -1

        os.replace(temporary, target)
        target.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
