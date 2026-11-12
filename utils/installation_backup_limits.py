"""Installation-only bounds; InfiniDysk migration keeps its independent policy."""

MAX_INSTALLATION_DEPTH = 128
MAX_INSTALLATION_ROOTS = 256
MAX_INSTALLATION_METADATA_BYTES = 4 * 1024**2
MAX_INSTALLATION_RECORD_BYTES = 64 * 1024
SQLITE_PAGE_BYTES = 4096
SQLITE_CACHE_KIB = 2048
MIN_PAIRED_WORKSPACE_BYTES = 1024**3
ARCHIVE_FIXED_OVERHEAD_BYTES = MAX_INSTALLATION_METADATA_BYTES + 64 * 1024


def paired_workspace_bytes(ledger_bytes=None):
    # Older retained proofs were bounded to 512MiB per ledger.
    if ledger_bytes is None:
        return MIN_PAIRED_WORKSPACE_BYTES
    if type(ledger_bytes) is not int or ledger_bytes <= 0:
        raise ValueError("The installation verification ledger footprint is invalid.")
    return max(MIN_PAIRED_WORKSPACE_BYTES, 2 * ledger_bytes)


def safe_ledger_budget(free_bytes, *, paired=False):
    if type(free_bytes) is not int or free_bytes < 0:
        raise ValueError("Local verification space could not be established.")
    # Keep the existing 10 percent margin. A census must leave headroom for
    # both ledgers; a running archive allocates just its next ledger here.
    available = free_bytes * (5 if paired else 10) // 11
    budget = available
    budget -= budget % SQLITE_PAGE_BYTES
    if budget < 2 * SQLITE_PAGE_BYTES:
        raise ValueError(
            "Insufficient free local space for the bounded verification ledger."
        )
    return budget


def installation_limits():
    return {
        "aggregate_entry_limit": None,
        "ledger_allocation": "available_local_space",
        "max_record_bytes": MAX_INSTALLATION_RECORD_BYTES,
        "max_metadata_bytes": MAX_INSTALLATION_METADATA_BYTES,
        "max_depth": MAX_INSTALLATION_DEPTH,
        "max_roots": MAX_INSTALLATION_ROOTS,
    }
