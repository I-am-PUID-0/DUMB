"""Release completed imports from their pin without changing migration evidence."""

import copy
import re
import time

from utils.infinidysk_migration_admission import INFINIDYSK_MIGRATION_ADMISSION_LOCK
from utils.infinidysk_release_controls import selected_release, set_release_selection
from utils.infinidysk_v2_runtime import (
    RELEASE,
    installed_v2_version,
    resolve_v2_data_dir,
    runtime_dir,
)

RELEASE_CONTROLS_SCHEMA = "standard-v1"
LEGACY_RELEASE_RECOVERY = (
    "This completed migration used the retired release configuration format. "
    "Its recovery files remain retained, but automatic rollback is unavailable."
)


def legacy_completed_release_job(job):
    return bool(
        job
        and job.get("status") == "completed"
        and (job.get("plan") or {}).get("release_controls_schema")
        != RELEASE_CONTROLS_SCHEMA
    )


ROLLBACK_RETIRED = (
    "This retained migration rollback is unavailable because a later native "
    "runtime update was authorized. Original backups remain retained. Use a "
    "reviewed restore procedure compatible with the current database."
)


def _pg():
    from utils.infinidysk_v2_postgres import INFINIDYSK_V2_POSTGRES_MANAGER

    return INFINIDYSK_V2_POSTGRES_MANAGER


def _current(manager, job, service):
    target = (job.get("plan") or {}).get("target") or {}
    configuration = manager._config()
    return (
        str(service.get("runtime_generation")) == "2"
        and str(runtime_dir(service)) == target.get("runtime_dir")
        and resolve_v2_data_dir(service, configuration.get("data_root") or "/data")
        == target.get("data_dir")
    )


def _settled(manager, job):
    if job.get("status") != "completed" or job.get("activation_committed") is not True:
        return "Complete the native migration and its recovery before updating its runtime."
    consumers = job.get("consumer_recovery") or {}
    if (
        job.get("activation_exposed") is not True
        or not job.get("accepted_import")
        or consumers.get("schema") != 1
        or consumers.get("pending")
        or consumers.get("failed")
        or consumers.get("ready") is not True
    ):
        return "Finish the retained native activation and affected app recovery before updating."
    if job.get("staging_cleanup_required"):
        return (
            "Finish the retained migration staging cleanup before updating its runtime."
        )
    restart = (job.get("result") or {}).get("restart_required") is True
    if restart and not (
        job.get("worker_id") != manager._worker_id
        and getattr(manager.process_handler, "startup_phase", None)
        in {"ready", "degraded"}
    ):
        return "Complete the required DUMB restart before updating the native runtime."
    return None


def major_runtime_update_blocker(manager):
    with INFINIDYSK_MIGRATION_ADMISSION_LOCK, manager._lock:
        if manager.has_blocking_job():
            return "A native migration, prepared result or recovery still owns the runtime."
        blocker = _pg().runtime_update_blocker()
        if blocker:
            return blocker
        service = manager._config().get("infinidysk") or {}
        for job in manager._jobs():
            if _current(manager, job, service) and job.get("status") == "completed":
                blocker = _settled(manager, job)
                if blocker:
                    return blocker
                if (
                    not legacy_completed_release_job(job)
                    and (job.get("runtime_updates_unlock") or {}).get("state")
                    == "pending"
                ):
                    return "The native runtime selector change needs to finish before an update."
    return None


def _selector_pair(old_service, new_service):
    from utils.infinidysk_v2_postgres import NativePostgresError

    old, new = copy.deepcopy(old_service), copy.deepcopy(new_service)
    for value in (old, new):
        for key in ("v2_release_version", "release_version", "release_version_enabled"):
            value.pop(key, None)
    if old != new or (
        selected_release(old_service) != selected_release(new_service)
        and (selected_release(old_service), selected_release(new_service))
        != (RELEASE, "latest")
    ):
        raise NativePostgresError(
            "Only the completed import's release selector may be rebound."
        )


def pg_runtime_update_blocker(manager):
    # Native status/recovery can hold its I/O lock while consulting lifecycle
    # protection. Never wait for that lock from the shared admission lane.
    if not manager._lock.acquire(blocking=False):
        return "Native database recovery is busy; runtime updates are deferred."
    try:
        job = manager._job()
        if job and (
            job.get("status")
            in {
                "starting",
                "copying",
                "syncing",
                "cutover",
                "rolling_back",
                "recovery_required",
            }
            or job.get("recovery_required")
            or job.get("cleanup_required")
        ):
            return "A native database migration or its cleanup/recovery still owns the runtime."
        if manager._boot().get("migration"):
            return (
                "A native database migration is active; runtime updates are deferred."
            )
    finally:
        manager._lock.release()
    return None


def rebind_runtime_selector(manager, old_service, new_service):
    """Rebind one selector-only change, without touching provider or DB identity."""
    from utils.infinidysk_v2_postgres import NativePostgresError

    _selector_pair(old_service, new_service)
    with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
        if not manager._lock.acquire(blocking=False):
            raise NativePostgresError(
                "Native database recovery is busy; retry the selector change later."
            )
        try:
            return _rebind_locked(manager, old_service, new_service)
        finally:
            manager._lock.release()


def _rebind_locked(manager, old_service, new_service):
    from utils.infinidysk_v2_postgres import NativePostgresError, _fingerprint

    if pg_runtime_update_blocker(manager):
        raise NativePostgresError(
            "Finish the native database operation before changing the runtime selector."
        )
    job = manager._job()
    if not job or job.get("status") != "completed":
        return False
    before, after = _fingerprint(old_service), _fingerprint(new_service)
    if _fingerprint(manager._service()) not in {before, after} or job.get(
        "service_fingerprint"
    ) not in {before, after}:
        raise NativePostgresError(
            "The completed native database service binding changed."
        )
    if job["service_fingerprint"] == after:
        return False
    job["runtime_selector_rebind"] = {"before": before, "after": after}
    job["service_fingerprint"] = after
    manager._save(job)
    return True


def unlock_runtime_updates(manager):
    """Release the former migration pin once; new jobs never impose one.

    Only completed legacy metadata is marked. Its original plan, snapshots and
    fingerprints remain retained, and automatic legacy rollback is unavailable.
    A saved latest selector lets a restart finish the marker after an interrupted
    save; a later deliberate pin is respected once the marker exists.
    """
    from utils.installation_backup import installation_backup_blocker

    with INFINIDYSK_MIGRATION_ADMISSION_LOCK, manager._lock:
        if installation_backup_blocker():
            return False
        if manager.has_blocking_job() or _pg().runtime_update_blocker():
            return False
        configuration = manager._config()
        service = configuration.get("infinidysk") or {}
        jobs = [
            job
            for job in manager._jobs()
            if _current(manager, job, service) and legacy_completed_release_job(job)
        ]
        if not jobs:
            return False
        job = max(
            jobs, key=lambda item: (item.get("created_at", 0), item.get("job_id", ""))
        )
        if job.get("runtime_updates_unlocked") is True or _settled(manager, job):
            return False
        changed = False
        if selected_release(service) == RELEASE:
            if installed_v2_version(service) != RELEASE:
                return False
            live = manager.config_manager.config["infinidysk"]
            before = copy.deepcopy(live)
            if live != service:
                return False
            set_release_selection(live, "latest")
            try:
                manager.config_manager.save_config(
                    service.get("process_name") or "InfiniDysk"
                )
            except Exception:
                live.clear()
                live.update(before)
                raise
            changed = True
        job["runtime_updates_unlocked"] = True
        manager._write(manager._job_path(job["job_id"]), job)
        return changed


def _retirement(receipt, service):
    from utils.infinidysk_v2_postgres import NativePostgresError

    if (
        not isinstance(receipt, dict)
        or receipt.get("format") != "native-runtime-update-v1"
        or receipt.get("runtime") != str(runtime_dir(service))
        or receipt.get("launched") is not False
        or receipt.get("state") != "active"
        or not isinstance(receipt.get("id"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", receipt["id"])
        or not isinstance((receipt.get("release_metadata") or {}).get("release"), str)
    ):
        raise NativePostgresError(
            "The native runtime update has no verified prelaunch retirement receipt."
        )
    return {
        "update_id": receipt["id"],
        "release": receipt["release_metadata"]["release"],
        "at": int(time.time()),
        "reason": ROLLBACK_RETIRED,
    }


def retire_major_runtime_rollback(manager, receipt):
    from utils.infinidysk_v2_migration import InfiniDyskV2MigrationError

    with INFINIDYSK_MIGRATION_ADMISSION_LOCK, manager._lock:
        service = manager._config().get("infinidysk") or {}
        retirement = _retirement(receipt, service)
        if manager.has_blocking_job():
            raise InfiniDyskV2MigrationError(
                "Native migration recovery still owns its retained rollback."
            )
        changed = False
        for job in manager._jobs():
            if (
                _current(manager, job, service)
                and job.get("status") == "completed"
                and not legacy_completed_release_job(job)
            ):
                if (
                    _settled(manager, job)
                    or (job.get("runtime_updates_unlock") or {}).get("state")
                    == "pending"
                ):
                    raise InfiniDyskV2MigrationError(
                        "The completed native migration is not settled for a runtime update."
                    )
                if not job.get("runtime_rollback_retired"):
                    job["runtime_rollback_retired"] = retirement
                    manager._write(manager._job_path(job["job_id"]), job)
                    changed = True
        return changed


def retire_pg_runtime_rollback(manager, receipt):
    from utils.infinidysk_v2_postgres import NativePostgresError

    with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
        if not manager._lock.acquire(blocking=False):
            raise NativePostgresError(
                "Native database recovery is busy; retry the runtime update later."
            )
        try:
            return _retire_pg_locked(manager, receipt)
        finally:
            manager._lock.release()


def _retire_pg_locked(manager, receipt):
    from utils.infinidysk_v2_postgres import NativePostgresError

    retirement = _retirement(receipt, manager._service())
    if pg_runtime_update_blocker(manager):
        raise NativePostgresError(
            "Native database recovery still owns its retained rollback."
        )
    job = manager._job()
    if (
        not job
        or job.get("status") != "completed"
        or job.get("runtime_rollback_retired")
    ):
        return False
    job["runtime_rollback_retired"] = retirement
    job["rollback_unavailable_reason"] = ROLLBACK_RETIRED
    job["can_rollback"] = False
    manager._save(job)
    return True
