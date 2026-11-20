"""Retry a trusted DUMB step while its original worker retains the continuation.

The journal describes a pause, not a serializable Python continuation. A new
controller must use ordinary interrupted-job recovery instead of replaying it.
"""

import errno
import secrets
import sqlite3
import threading
import time

_OPERATIONS = {
    "source_fingerprint": ("Verify retained files", "snapshotting"),
    "verify_import": ("Check the converted library", "validating"),
    "archive_readback": ("Verify the recovery archive", "verifying_archive"),
    "archive_write": ("Save the recovery archive", "archiving"),
    "postgres_restore": ("Check the PostgreSQL recovery copy", "postgres_snapshot"),
    "external_input": ("Check the staged import files", "verifying_blob_input"),
}
_TRANSIENT_ERRNOS = {
    errno.EIO,
    errno.ESTALE,
    errno.ETIMEDOUT,
    errno.EAGAIN,
    errno.EBUSY,
    errno.ENOSPC,
    errno.EDQUOT,
    errno.ENETDOWN,
    errno.ENETUNREACH,
    errno.EHOSTUNREACH,
    errno.ECONNRESET,
    errno.ECONNABORTED,
    errno.ENODEV,
    errno.ENXIO,
    errno.EMFILE,
    errno.ENFILE,
}
_TRANSIENT_SQLITE = {
    sqlite3.SQLITE_BUSY,
    sqlite3.SQLITE_LOCKED,
    sqlite3.SQLITE_IOERR,
    sqlite3.SQLITE_FULL,
    sqlite3.SQLITE_CANTOPEN,
    sqlite3.SQLITE_PROTOCOL,
}


def retryable_error(error):
    """Recognize storage failures without promoting validation errors to retries."""
    from utils.infinidysk_v2_migration import InfiniDyskV2Cancelled
    from utils.infinidysk_v2_pg_snapshot import InfiniDyskV2PostgresSnapshotError

    pending, seen, retryable = [error], set(), False
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if len(seen) > 16:
            return False
        if (
            isinstance(current, InfiniDyskV2Cancelled)
            or getattr(current, "cleanup_complete", None) is False
            or getattr(current, "stage_retry_safe", None) is False
        ):
            return False
        if isinstance(current, OSError) and current.errno in _TRANSIENT_ERRNOS:
            retryable = True
        if (
            isinstance(current, InfiniDyskV2PostgresSnapshotError)
            and getattr(current, "retry_errno", None) == errno.ENOSPC
        ):
            retryable = True
        code = getattr(current, "sqlite_errorcode", None)
        if (
            isinstance(current, sqlite3.Error)
            and type(code) is int
            and code & 255 in _TRANSIENT_SQLITE
        ):
            retryable = True
        for cause in (current.__cause__, current.__context__):
            if isinstance(cause, BaseException):
                pending.append(cause)
    return retryable


class StageRetry:
    def __init__(self, manager):
        self.manager = manager
        self._pending = {}

    def _live(self, job):
        manager = self.manager
        return (
            job.get("stage_retry_enabled") is True
            and job.get("worker_id") == manager._worker_id
            and manager._active_id == job.get("job_id")
            and manager._thread is not None
            and manager._thread.is_alive()
            and not manager._rollback_active
            and job.get("status") != "rolling_back"
            and not manager._possibly_exposed(job)
            and manager._owned_process_cleanup_complete(job)
            and not job.get("cancel_requested")
            and job.get("job_id") not in manager._cancel_requested
        )

    def public(self, job, state):
        with self.manager._lock:
            record = job.get("stage_retry")
            pending = self._pending.get(job.get("job_id"))
            allowed = bool(
                state == "stage_retry_required"
                and self._live(job)
                and pending
                and isinstance(record, dict)
                and record.get("state") == "waiting"
                and pending["attempt"] == record.get("attempt")
                and pending["operation"] == record.get("operation")
            )
            result = {"can_retry_stage": allowed}
            if isinstance(record, dict):
                result["stage_retry"] = {
                    key: record.get(key)
                    for key in (
                        "protocol",
                        "operation",
                        "stage",
                        "label",
                        "attempt",
                        "state",
                        "failed_at",
                        "message",
                    )
                }
            if allowed:
                result["confirmation"] = pending["confirmation"]
            return result

    def run(self, job, operation, callback):
        manager = self.manager
        if job.get("stage_retry_enabled") is not True or not self._live(job):
            return callback()
        if operation not in _OPERATIONS:
            raise ValueError("Unsupported DUMB migration retry step.")
        while True:
            manager._assert_job_owner(job)
            try:
                result = callback()
            except Exception as error:
                if not self._live(job) or not retryable_error(error):
                    raise
                with manager._lock:
                    manager._assert_job_owner(job)
                    label, fallback_stage = _OPERATIONS[operation]
                    attempt = (job.get("stage_retry") or {}).get("attempt", 0) + 1
                    event = threading.Event()
                    pending = {
                        "job": job,
                        "event": event,
                        "operation": operation,
                        "attempt": attempt,
                        "confirmation": f"RETRY INFINIDYSK V2 STAGE {job['job_id']} {secrets.token_hex(16)}",
                    }
                    record = {
                        "protocol": 1,
                        "operation": operation,
                        "stage": job.get("stage") or fallback_stage,
                        "label": label,
                        "attempt": attempt,
                        "state": "waiting",
                        "failed_at": int(time.time()),
                        "message": "An error accessing storage or the database stopped this step. Completed work is retained. Resolve the storage problem, then retry this step or restore the previous setup.",
                    }
                    self._pending[job["job_id"]] = pending
                    job.update(
                        stage_retry=record,
                        # Older controllers must still recognize an unfinished
                        # job after restart. The live manager projects the new
                        # public pause state from this existing active status.
                        status="running",
                        message=record["message"],
                        updated_at=int(time.time()),
                    )
                    manager._write(manager._job_path(job["job_id"]), job)
                try:
                    while not event.wait(0.25):
                        manager._assert_job_owner(job)
                    manager._assert_job_owner(job)
                finally:
                    with manager._lock:
                        if self._pending.get(job["job_id"]) is pending:
                            self._pending.pop(job["job_id"], None)
                continue
            with manager._lock:
                record = job.get("stage_retry") or {}
                if (
                    record.get("operation") == operation
                    and record.get("state") == "retrying"
                ):
                    record["state"] = "completed"
                    manager._write(manager._job_path(job["job_id"]), job)
            return result

    def retry(self, request):
        from utils.infinidysk_migration_admission import (
            INFINIDYSK_MIGRATION_ADMISSION_LOCK,
        )
        from utils.infinidysk_v2_migration import InfiniDyskV2MigrationError

        manager = self.manager
        with INFINIDYSK_MIGRATION_ADMISSION_LOCK, manager._lock:
            job = manager._read(manager._job_path(request.get("job_id")))
            public = self.public(job, manager._effective_status(job)) if job else {}
            phrase = request.get("confirmation")
            if (
                public.get("can_retry_stage") is not True
                or type(phrase) is not str
                or not phrase.isascii()
                or len(phrase) > 256
                or not secrets.compare_digest(phrase, public.get("confirmation", ""))
            ):
                raise InfiniDyskV2MigrationError(
                    "This DUMB step has no current retry authorization. Refresh status and use the exact confirmation."
                )
            pending = self._pending[job["job_id"]]
            owned = pending["job"]
            manager._assert_job_owner(owned)
            owned["stage_retry"]["state"] = "retrying"
            owned.update(
                status="running",
                message="Retrying the paused DUMB step. Completed work is retained.",
                updated_at=int(time.time()),
            )
            manager._write(manager._job_path(owned["job_id"]), owned)
            pending["event"].set()
            return {"job": manager._public_job(owned)}
