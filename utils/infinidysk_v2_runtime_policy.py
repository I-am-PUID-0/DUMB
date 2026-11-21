"""Release completed imports from their pin without changing migration evidence."""

import copy
import re
import time

from utils.infinidysk_migration_admission import INFINIDYSK_MIGRATION_ADMISSION_LOCK
from utils.infinidysk_v2_runtime import (
    RELEASE,
    installed_v2_version,
    resolve_v2_data_dir,
    runtime_dir,
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
                if (job.get("runtime_updates_unlock") or {}).get("state") == "pending":
                    return "The native runtime selector change needs to finish before an update."
    return None


def _selector_pair(old_service, new_service):
    from utils.infinidysk_v2_postgres import NativePostgresError

    old, new = copy.deepcopy(old_service), copy.deepcopy(new_service)
    old.pop("v2_release_version", None)
    new.pop("v2_release_version", None)
    if (
        old != new
        or str(old_service.get("v2_release_version") or RELEASE) != RELEASE
        or new_service.get("v2_release_version") != "latest"
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
    """One durable selector transition after a fully settled, verified import."""
    from utils.infinidysk_v2_migration import InfiniDyskV2MigrationError, _digest
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
            if _current(manager, job, service) and job.get("status") == "completed"
        ]
        if not jobs:
            return False  # Fresh native installs need no historical source job.
        job = max(
            jobs, key=lambda item: (item.get("created_at", 0), item.get("job_id", ""))
        )
        if job.get("runtime_updates_unlocked") is True or _settled(manager, job):
            return False
        intent = job.get("runtime_updates_unlock")
        if intent is None:
            if str(service.get("v2_release_version") or RELEASE) != RELEASE:
                # An explicit user choice already replaced the migration pin.
                job["runtime_updates_unlocked"] = True
                manager._write(manager._job_path(job["job_id"]), job)
                return False
            if installed_v2_version(service) != RELEASE:
                return False
            current = _digest(manager._owned_for_job(configuration, job))
            if current != job.get("activation_fingerprint"):
                raise InfiniDyskV2MigrationError(
                    "The completed native configuration changed before its release pin could be unlocked."
                )
            new_service = copy.deepcopy(service)
            new_service["v2_release_version"] = "latest"
            changed = copy.deepcopy(configuration)
            changed["infinidysk"] = new_service
            intent = {
                "state": "pending",
                "before": _digest(service),
                "after": _digest(new_service),
                "old_selector_present": "v2_release_version" in service,
                "old_selector": service.get("v2_release_version"),
                "old_activation": current,
                "new_activation": _digest(manager._owned_for_job(changed, job)),
            }
            job["runtime_updates_unlock"] = intent
            manager._write(manager._job_path(job["job_id"]), job)
        if (
            not isinstance(intent, dict)
            or intent.get("state") != "pending"
            or _digest(service) not in {intent.get("before"), intent.get("after")}
        ):
            raise InfiniDyskV2MigrationError(
                "The interrupted native release selector change requires recovery review."
            )
        old_service = copy.deepcopy(service)
        if intent["old_selector_present"]:
            old_service["v2_release_version"] = intent["old_selector"]
        else:
            old_service.pop("v2_release_version", None)
        new_service = copy.deepcopy(old_service)
        new_service["v2_release_version"] = "latest"
        _selector_pair(old_service, new_service)
        if (
            _digest(old_service) != intent["before"]
            or _digest(new_service) != intent["after"]
        ):
            raise InfiniDyskV2MigrationError(
                "The retained native release selector intent changed."
            )
        if installed_v2_version(service) != RELEASE:
            raise InfiniDyskV2MigrationError(
                "The pinned native runtime changed during its release selector transition."
            )
        expected_owned = (
            intent["old_activation"]
            if _digest(service) == intent["before"]
            else intent["new_activation"]
        )
        if _digest(manager._owned_for_job(configuration, job)) != expected_owned:
            raise InfiniDyskV2MigrationError(
                "The owned native configuration changed during its selector transition."
            )
        consumers = job.get("consumer_recovery") or {}
        target = consumers.get("target_binding") or {}
        if target.get("configuration") != intent["old_activation"]:
            raise InfiniDyskV2MigrationError(
                "The completed native recovery configuration binding changed."
            )
        process_name = service.get("process_name") or "InfiniDysk"
        for binding in consumers.get("scope_bindings") or []:
            if binding.get("name") == process_name and binding.get("sha256") != _digest(
                {"key": "infinidysk", "service": old_service}
            ):
                raise InfiniDyskV2MigrationError(
                    "The completed native runtime scope binding changed."
                )
        if _digest(service) == intent["before"]:
            live = manager.config_manager.config["infinidysk"]
            if _digest(live) != intent["before"]:
                raise InfiniDyskV2MigrationError(
                    "The native release selector changed during unlock."
                )
            live["v2_release_version"] = "latest"
            try:
                manager.config_manager.save_config(
                    service.get("process_name") or "InfiniDysk"
                )
            except Exception:
                if _digest(live) == intent["after"]:
                    live.clear()
                    live.update(old_service)
                raise
        _pg().rebind_runtime_selector(old_service, new_service)
        target["configuration"] = intent["new_activation"]
        for binding in consumers.get("scope_bindings") or []:
            if binding.get("name") == process_name:
                binding["sha256"] = _digest(
                    {"key": "infinidysk", "service": new_service}
                )
        job["activation_fingerprint"] = intent["new_activation"]
        intent["state"] = "completed"
        job["runtime_updates_unlocked"] = True
        manager._write(manager._job_path(job["job_id"]), job)
        return True


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
            if _current(manager, job, service) and job.get("status") == "completed":
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
