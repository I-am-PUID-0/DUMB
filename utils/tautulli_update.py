"""Keep Tautulli's bundled templates separate from its colocated runtime data."""

import os
from pathlib import Path


def tautulli_persistent_excludes(target_dir, exclusions):
    """Expand the legacy data exclusion so data/interfaces can be updated.

    Preserve every existing runtime entry, including unknown/custom files. Only
    the upstream-owned interfaces tree is replaceable; explicit narrower
    exclusions remain authoritative. A failed scan aborts before any clearing.
    """
    target = Path(os.path.abspath(target_dir))
    data = target / "data"
    protected = set()
    for value in exclusions:
        path = Path(str(value))
        if not path.is_absolute():
            path = target / path
        path = Path(os.path.abspath(path))
        if path != data:
            protected.add(str(path))
            continue
        if data.is_symlink():
            # Never clear a symlinked runtime tree.
            protected.add(str(data))
            continue
        if data.exists():
            for entry in data.iterdir():
                if entry.name != "interfaces" or entry.is_symlink():
                    protected.add(str(entry))
        # Protect runtime names even on a fresh install or an empty data volume.
        for name in (
            "config.ini",
            "tautulli.db",
            "tautulli.db-wal",
            "tautulli.db-shm",
            "tautulli.db-journal",
            "logs",
            "cache",
            "backups",
            "newsletters",
        ):
            protected.add(str(data / name))
    return sorted(protected)
