"""Helpers for atomically writing application files that contain credentials."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_private_text(
    path: str | os.PathLike[str],
    content: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """Atomically replace ``path`` with a mode-0600 text file.

    The existing owner and, when permitted, group are retained so a service
    keeps access when DUMB refreshes its configuration after setup.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    owner: tuple[int, int] | None = None
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
