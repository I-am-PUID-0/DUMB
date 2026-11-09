"""Shared admission guard for InfiniDysk's two destructive migration workflows."""

import threading
import secrets
from contextlib import contextmanager

INFINIDYSK_MIGRATION_ADMISSION_LOCK = threading.RLock()
_EXTERNAL_MUTATIONS: dict[str, str] = {}
_V2_CONTEXT = threading.local()
_V2_PENDING: set[str] = set()

ACTIVE_NAMESPACE_MIGRATION_BLOCKER = (
    "An InfiniDysk namespace migration job is active or needs recovery review. "
    "Finish or resolve it before changing InfiniDysk or PostgreSQL."
)
ACTIVE_POSTGRES_MIGRATION_BLOCKER = (
    "An InfiniDysk SQLite-to-PostgreSQL migration job is active or has a pending "
    "guarded rollback. Finish or resolve it before changing InfiniDysk or PostgreSQL."
)
RECOVERY_PENDING_BLOCKER = (
    "An InfiniDysk migration was interrupted or its rollback needs operator "
    "attention. Resolve the retained recovery state before starting services."
)
EXTERNAL_MUTATION_BLOCKER = (
    "Another DUMB operation is changing managed configuration, runtime state, "
    "or paths. Wait for it to finish before starting the InfiniDysk namespace "
    "migration."
)


class InfiniDyskMigrationAdmissionError(RuntimeError):
    """Raised when two destructive migration-adjacent operations overlap."""


@contextmanager
def v2_migration_owner(kind):
    """Server-only execution context; requests cannot supply this authority."""
    if kind not in {"major", "postgres"}:
        raise ValueError("Unknown native migration owner")
    previous = getattr(_V2_CONTEXT, "owners", frozenset())
    _V2_CONTEXT.owners = previous | {kind}
    try:
        yield
    finally:
        _V2_CONTEXT.owners = previous


def infinidysk_v2_jobs_active():
    from utils.infinidysk_v2_migration import INFINIDYSK_V2_MIGRATION_MANAGER
    from utils.infinidysk_v2_postgres import INFINIDYSK_V2_POSTGRES_MANAGER

    owners = getattr(_V2_CONTEXT, "owners", frozenset())
    with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
        if _V2_PENDING - owners:
            return True
    return bool(
        ("major" not in owners and INFINIDYSK_V2_MIGRATION_MANAGER.has_blocking_job())
        or (
            "postgres" not in owners
            and INFINIDYSK_V2_POSTGRES_MANAGER.has_blocking_job()
        )
    )


def infinidysk_v2_recovery_pending():
    from utils.infinidysk_v2_migration import INFINIDYSK_V2_MIGRATION_MANAGER
    from utils.infinidysk_v2_postgres import INFINIDYSK_V2_POSTGRES_MANAGER

    owners = getattr(_V2_CONTEXT, "owners", frozenset())
    return bool(
        (
            "major" not in owners
            and INFINIDYSK_V2_MIGRATION_MANAGER.has_recovery_pending_job()
        )
        or (
            "postgres" not in owners
            and INFINIDYSK_V2_POSTGRES_MANAGER.has_recovery_pending_job()
        )
    )


@contextmanager
def admit_v2_job(kind, *, existing=False):
    """Reserve short admission without holding the RLock during native I/O."""
    with v2_migration_owner(kind):
        with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
            from utils.arr_postgres_migration import POSTGRES_MIGRATION_MANAGER

            if (
                kind in _V2_PENDING
                or infinidysk_namespace_migration_active()
                or infinidysk_postgres_migration_active()
                or POSTGRES_MIGRATION_MANAGER.has_namespace_conflicting_job()
                or infinidysk_v2_jobs_active()
                or _EXTERNAL_MUTATIONS
            ):
                raise InfiniDyskMigrationAdmissionError(EXTERNAL_MUTATION_BLOCKER)
            if not existing:
                from utils.infinidysk_v2_migration import (
                    INFINIDYSK_V2_MIGRATION_MANAGER,
                )
                from utils.infinidysk_v2_postgres import INFINIDYSK_V2_POSTGRES_MANAGER

                manager = (
                    INFINIDYSK_V2_MIGRATION_MANAGER
                    if kind == "major"
                    else INFINIDYSK_V2_POSTGRES_MANAGER
                )
                if manager.has_blocking_job():
                    raise InfiniDyskMigrationAdmissionError(EXTERNAL_MUTATION_BLOCKER)
            _V2_PENDING.add(kind)
        try:
            yield
        finally:
            with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
                _V2_PENDING.discard(kind)


def infinidysk_postgres_migration_active() -> bool:
    """Return whether a PostgreSQL job blocks lifecycle/config admission."""

    from utils.arr_postgres_migration import POSTGRES_MIGRATION_MANAGER

    return POSTGRES_MIGRATION_MANAGER.has_active_infinidysk_job()


def infinidysk_namespace_migration_active() -> bool:
    """Return whether a namespace job blocks lifecycle/config admission."""

    from utils.infinidysk_migration import INFINIDYSK_MIGRATION_MANAGER

    return INFINIDYSK_MIGRATION_MANAGER.has_blocking_job()


def infinidysk_postgres_recovery_pending() -> bool:
    """Return whether a terminal PostgreSQL job requires recovery attention."""

    from utils.arr_postgres_migration import POSTGRES_MIGRATION_MANAGER

    return POSTGRES_MIGRATION_MANAGER.has_infinidysk_recovery_pending_job()


def infinidysk_namespace_recovery_pending() -> bool:
    """Return whether a terminal namespace job requires recovery attention."""

    from utils.infinidysk_migration import INFINIDYSK_MIGRATION_MANAGER

    return INFINIDYSK_MIGRATION_MANAGER.has_recovery_pending_job()


def infinidysk_namespace_pre_mutation_interrupted() -> bool:
    """Return whether ordinary cold boot may restore the legacy topology."""

    from utils.infinidysk_migration import INFINIDYSK_MIGRATION_MANAGER

    return INFINIDYSK_MIGRATION_MANAGER.has_pre_mutation_interrupted_job()


def infinidysk_recovery_blocks_service(
    service_key: str | None, process_name: str | None = None
) -> str | None:
    """Return a fail-closed boot blocker for migration recovery states."""

    key = str(service_key or "").strip().lower()
    normalized_name = str(process_name or "").replace(" ", "").strip().lower()
    if infinidysk_namespace_recovery_pending() or infinidysk_v2_recovery_pending():
        # Preserve the API/frontend/Traefik control plane so the operator can
        # inspect and resolve recovery. Other managed services remain frozen
        # because the retained namespace inventory can include any of them.
        if key not in {"dumb", "dumb_frontend", "traefik"} and normalized_name not in {
            "dumbapi",
            "dumbfrontend",
            "traefik",
        }:
            return RECOVERY_PENDING_BLOCKER
    if key in {"infinidysk", "postgres"} and infinidysk_postgres_recovery_pending():
        return RECOVERY_PENDING_BLOCKER
    return None


def reserve_infinidysk_external_mutation(label: str) -> str:
    """Atomically reserve a migration-adjacent filesystem/runtime mutation."""

    with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
        if infinidysk_namespace_migration_active() or infinidysk_v2_jobs_active():
            raise InfiniDyskMigrationAdmissionError(ACTIVE_NAMESPACE_MIGRATION_BLOCKER)
        token = secrets.token_hex(16)
        _EXTERNAL_MUTATIONS[token] = str(label or "managed mutation")[:200]
        return token


def release_infinidysk_external_mutation(token: str | None) -> None:
    """Release one exact mutation reservation."""

    if not token:
        return
    with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
        _EXTERNAL_MUTATIONS.pop(str(token), None)


def assert_infinidysk_external_mutation_reserved(token: str) -> None:
    """Fail closed before an asynchronous reserved mutation starts writing."""

    with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
        if (
            token not in _EXTERNAL_MUTATIONS
            or infinidysk_namespace_migration_active()
            or infinidysk_v2_jobs_active()
        ):
            raise InfiniDyskMigrationAdmissionError(ACTIVE_NAMESPACE_MIGRATION_BLOCKER)


def infinidysk_external_mutation_active() -> bool:
    """Return whether a reserved mutation blocks namespace admission."""

    with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
        return bool(_EXTERNAL_MUTATIONS) or infinidysk_v2_jobs_active()
