"""Shared admission guard for InfiniDysk's two destructive migration workflows."""

import threading
import secrets
from contextlib import contextmanager
from enum import Enum

INFINIDYSK_MIGRATION_ADMISSION_LOCK = threading.RLock()
# Runtime update journals bind the native settings present at update admission.
# Ordinary post-start synchronization must not change those settings mid-update.
INFINIDYSK_NATIVE_SETTINGS_LOCK = threading.Lock()
_EXTERNAL_MUTATIONS: dict[str, str] = {}
# History, notifications and remote synchronization must finish before an
# installation snapshot, but they do not own InfiniDysk's service lifecycle.
_INSTALLATION_CONTROL_WRITES: dict[str, str] = {}
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


class AdmissionReason(str, Enum):
    CONFLICT = "admission_conflict"
    PENDING = "native_operation_pending"
    NAMESPACE = "namespace_migration"
    LEGACY_POSTGRES = "legacy_postgres_migration"
    SERVICE_DATABASE = "service_database_migration"
    NATIVE_MIGRATION = "native_migration"
    MAJOR_MIGRATION = "major_migration"
    NATIVE_POSTGRES = "native_postgres_migration"
    BACKUP = "installation_backup"
    ARR_SYNC = "arr_synchronization"
    RUNTIME_UPDATE = "native_runtime_update"
    SERVICE_LIFECYCLE = "service_lifecycle"
    SETUP = "managed_setup"
    API_REQUEST = "dashboard_request"
    CONFIGURATION = "configuration_synchronization"
    MAINTENANCE = "service_maintenance"
    EXTERNAL = "managed_operation"


_ADMISSION_MESSAGES = {
    AdmissionReason.CONFLICT: "Another lifecycle or migration operation owns admission.",
    AdmissionReason.PENDING: (
        "Another InfiniDysk 2 request is still starting or finishing. "
        "Wait for it to finish, then retry."
    ),
    AdmissionReason.NAMESPACE: (
        "The InfiniDysk namespace migration is active or needs recovery review. "
        "Open its migration status and finish or resolve it before continuing."
    ),
    AdmissionReason.LEGACY_POSTGRES: (
        "An existing InfiniDysk database migration is active or needs recovery review. "
        "Finish or resolve that database migration before continuing."
    ),
    AdmissionReason.SERVICE_DATABASE: (
        "A managed application's database migration is active or needs recovery review. "
        "Finish or resolve it before starting another migration."
    ),
    AdmissionReason.NATIVE_MIGRATION: (
        "Another InfiniDysk 2 migration is active or needs recovery review. "
        "Open migration progress and recovery before continuing."
    ),
    AdmissionReason.MAJOR_MIGRATION: (
        "An InfiniDysk 2 installation or upgrade is active, prepared, or needs recovery review. "
        "Open its migration progress and recovery before starting another operation."
    ),
    AdmissionReason.NATIVE_POSTGRES: (
        "An InfiniDysk 2 database conversion is active or needs recovery review. "
        "Open its database conversion status before preparing another destination."
    ),
    AdmissionReason.BACKUP: (
        "A backup, restore, or its recovery is controlling the installation. "
        "Open Backup and restore for its status before continuing."
    ),
    AdmissionReason.ARR_SYNC: (
        "DUMB is checking or updating InfiniDysk's Arr download-client connections. "
        "Wait for those checks to finish, then retry."
    ),
    AdmissionReason.RUNTIME_UPDATE: (
        "An InfiniDysk 2 runtime update is in progress. "
        "Wait for the update to finish before continuing."
    ),
    AdmissionReason.SERVICE_LIFECYCLE: (
        "DUMB is starting or updating a service. "
        "Wait for service startup or the update to finish, then retry."
    ),
    AdmissionReason.SETUP: (
        "DUMB is applying service setup. Wait for setup to finish, then retry."
    ),
    AdmissionReason.API_REQUEST: (
        "Another dashboard request is changing settings or a service. "
        "Wait for that request to finish, then retry."
    ),
    AdmissionReason.CONFIGURATION: (
        "DUMB is synchronizing InfiniDysk configuration. "
        "Wait for synchronization to finish, then retry."
    ),
    AdmissionReason.MAINTENANCE: (
        "A service maintenance operation is in progress. "
        "Wait for it to finish, then retry."
    ),
    AdmissionReason.EXTERNAL: (
        "Another DUMB operation is changing managed settings or service state. "
        "Wait for it to finish, then retry."
    ),
}


class InfiniDyskMigrationAdmissionError(RuntimeError):
    """A conflict with a static public reason, separate from private diagnostics."""

    def __init__(self, message=None, *, reason=AdmissionReason.CONFLICT):
        try:
            reason = AdmissionReason(reason)
        except (TypeError, ValueError):
            reason = AdmissionReason.CONFLICT
        self.reason_code = reason.value
        self.public_message = _ADMISSION_MESSAGES[reason]
        super().__init__(self.public_message if message is None else message)


def _external_admission_reason():
    """Classify known reservation labels without exposing their private suffixes."""
    reasons = set()
    for label in _EXTERNAL_MUTATIONS.values():
        if not isinstance(label, str):
            return AdmissionReason.EXTERNAL
        if label == "InfiniDysk managed Arr credential synchronization":
            reason = AdmissionReason.ARR_SYNC
        elif label.startswith("native runtime update: "):
            reason = AdmissionReason.RUNTIME_UPDATE
        elif label.startswith("service startup/update: "):
            reason = AdmissionReason.SERVICE_LIFECYCLE
        elif label == "Managed arrangement setup":
            reason = AdmissionReason.SETUP
        elif label.startswith("API "):
            reason = AdmissionReason.API_REQUEST
        elif label == "InfiniDysk configuration synchronization":
            reason = AdmissionReason.CONFIGURATION
        elif label.startswith(("symlink ", "rclone optimizer ")):
            reason = AdmissionReason.MAINTENANCE
        else:
            reason = AdmissionReason.EXTERNAL
        reasons.add(reason)
    return next(iter(reasons)) if len(reasons) == 1 else AdmissionReason.EXTERNAL


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


def infinidysk_major_recovery_pending():
    """Hold boot mutations while an interrupted major import needs recovery."""
    from utils.infinidysk_v2_migration import INFINIDYSK_V2_MIGRATION_MANAGER

    return (
        INFINIDYSK_V2_MIGRATION_MANAGER.has_recovery_pending_job()
        or INFINIDYSK_V2_MIGRATION_MANAGER.has_prepared_job()
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

            # Keep the original guard order and short-circuit behavior: report
            # the first actual blocker without probing later owners or evidence.
            if kind in _V2_PENDING:
                reason = AdmissionReason.PENDING
            elif infinidysk_namespace_migration_active():
                reason = AdmissionReason.NAMESPACE
            elif infinidysk_postgres_migration_active():
                reason = AdmissionReason.LEGACY_POSTGRES
            elif POSTGRES_MIGRATION_MANAGER.has_namespace_conflicting_job():
                reason = AdmissionReason.SERVICE_DATABASE
            elif infinidysk_v2_jobs_active():
                reason = AdmissionReason.NATIVE_MIGRATION
            elif _EXTERNAL_MUTATIONS:
                reason = _external_admission_reason()
            elif _installation_blocked():
                reason = AdmissionReason.BACKUP
            else:
                reason = None
            if reason is not None:
                raise InfiniDyskMigrationAdmissionError(
                    EXTERNAL_MUTATION_BLOCKER, reason=reason
                )
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
                    raise InfiniDyskMigrationAdmissionError(
                        EXTERNAL_MUTATION_BLOCKER,
                        reason=(
                            AdmissionReason.MAJOR_MIGRATION
                            if kind == "major"
                            else AdmissionReason.NATIVE_POSTGRES
                        ),
                    )
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


def _installation_blocked():
    from utils.installation_backup import installation_backup_blocker

    return installation_backup_blocker()


def infinidysk_recovery_blocks_service(
    service_key: str | None, process_name: str | None = None
) -> str | None:
    """Return a fail-closed boot blocker for migration recovery states."""

    key = str(service_key or "").strip().lower()
    installation = _installation_blocked()
    if installation and key not in {"dumb", "dumb_frontend"}:
        return installation
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
        if (
            _installation_blocked()
            or infinidysk_namespace_migration_active()
            or infinidysk_v2_jobs_active()
        ):
            raise InfiniDyskMigrationAdmissionError(ACTIVE_NAMESPACE_MIGRATION_BLOCKER)
        token = secrets.token_hex(16)
        _EXTERNAL_MUTATIONS[token] = str(label or "managed mutation")[:200]
        return token


def reserve_installation_control_write(label):
    """Track a control-plane writer without treating notification/history as native service mutation."""
    with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
        if _installation_blocked():
            raise InfiniDyskMigrationAdmissionError(
                "Installation snapshot owns control-plane persistence."
            )
        token = secrets.token_hex(16)
        _INSTALLATION_CONTROL_WRITES[token] = str(label)[:200]
        return token


def release_installation_control_write(token: str | None) -> None:
    """Release exactly one installation-only persistence reservation."""
    if not token:
        return
    with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
        _INSTALLATION_CONTROL_WRITES.pop(str(token), None)


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
            _installation_blocked()
            or token not in _EXTERNAL_MUTATIONS
            or infinidysk_namespace_migration_active()
            or infinidysk_v2_jobs_active()
        ):
            raise InfiniDyskMigrationAdmissionError(ACTIVE_NAMESPACE_MIGRATION_BLOCKER)


def infinidysk_external_mutation_active() -> bool:
    """Return whether a reserved mutation blocks namespace admission."""

    with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
        return (
            bool(_EXTERNAL_MUTATIONS)
            or bool(_installation_blocked())
            or infinidysk_v2_jobs_active()
        )


def installation_persistence_active() -> bool:
    """Include background writers when admitting a cold snapshot or recovery."""
    with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
        return (
            bool(_INSTALLATION_CONTROL_WRITES) or infinidysk_external_mutation_active()
        )
