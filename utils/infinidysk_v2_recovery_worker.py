"""Keep long archive restoration outside the initiating HTTP request."""

import threading


def start_rollback_worker(manager, job, update_lock, operation):
    """Dispatch an already admitted rollback; operation owns its final receipt."""

    def perform():
        try:
            operation(job, update_lock)
        except Exception:
            # The operation persists recovery_required and releases its locks.
            # Its sanitized job error is polled by the UI; do not print private
            # exception context from a background thread.
            return

    with manager._lock:
        previous = manager._thread
        worker = threading.Thread(
            target=perform,
            name=f"infinidysk-v2-recovery-{job['job_id']}",
            daemon=True,
        )
        manager._thread = worker
        try:
            worker.start()
        except Exception as error:
            manager._thread = previous
            message = (
                "Recovery could not start. Your migration backup and existing "
                "files were kept. Retry recovery."
            )
            job.update(
                status="recovery_required", stage="recovery_required", error=message
            )
            try:
                manager._write(manager._job_path(job["job_id"]), job)
            finally:
                manager._active_id = None
                manager._rollback_active = False
                update_lock.release()
            raise RuntimeError(message) from error
        return {"job": manager._public_job(job)}
