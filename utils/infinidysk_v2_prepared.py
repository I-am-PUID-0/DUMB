"""Durable review and activation of one accepted native conversion.

A prepared candidate is intentionally stopped. Its job continues to own lifecycle
admission across controller restarts until explicit activation or v1 restoration.
"""

import copy
from pathlib import Path
import threading
import time

from utils.infinidysk_migration_admission import INFINIDYSK_MIGRATION_ADMISSION_LOCK
from utils.infinidysk_v2_runtime import installed_v2_version, runtime_dir


class PreparedMigration:
    def __init__(self, manager):
        self.manager = manager

    @staticmethod
    def _refuse(message):
        from utils.infinidysk_v2_migration import InfiniDyskV2MigrationError

        raise InfiniDyskV2MigrationError(message)

    def _binding(self, job, workspace):
        from utils.infinidysk_v2_migration import _digest, _identity

        manager = self.manager
        plan = job["plan"]
        manager._assert_job_owner(job)
        manager._assert_stopped(plan)
        if _digest(manager._owned_for_job(manager._config(), job)) != _digest(
            manager._owned_for_job(plan["config_snapshot"], job)
        ):
            self._refuse(
                "Migration configuration changed while the prepared database was paused. Restore v1 before preparing again."
            )
        candidate = Path(workspace["candidate_data"])
        if _identity(candidate) != job["snapshot"]["candidate_identity"]:
            self._refuse(
                "The prepared database directory was replaced. Keep the recovery archive and restore v1."
            )
        service = plan["target"]["service"]
        if not installed_v2_version(service):
            self._refuse(
                "The pinned native runtime changed. Keep the recovery archive and restore v1."
            )
        self.manager.executor._assert_unmounted(plan["target"]["mount_path"])
        return {
            "job_id": job["job_id"],
            "plan_fingerprint": job["plan_fingerprint"],
            "snapshot_fingerprint": job["snapshot"]["snapshot_fingerprint"],
            "accepted_import": _digest(job["accepted_import"]),
            "owned_config": _digest(manager._owned_for_job(manager._config(), job)),
            "candidate_identity": _identity(candidate),
            "candidate_fingerprint": manager._fingerprint(
                job, candidate, scratch_root=workspace["root"]
            ),
            "runtime_fingerprint": manager._fingerprint(
                job, runtime_dir(service), scratch_root=workspace["root"]
            ),
            "target_identity": _identity(plan["target"]["data_dir"]),
            "target_parent_identity": _identity(candidate.parent),
            "mount_identity": _identity(plan["target"]["mount_path"]),
            "omission_proof": copy.deepcopy(job.get("omission_proof_ref")),
            "queue_input": _digest(job.get("queue_input")),
        }

    def record(self, job, workspace, receipt):
        if receipt.get("candidate_prepared") is not True:
            self._refuse(
                "Native preparation did not confirm its stopped converted database."
            )
        self.manager._validate_receipt(job, receipt)
        job["prepared_binding"] = self._binding(job, workspace)
        self.manager._write(Path(workspace["receipt_path"]), receipt)

    def verify(self, job, workspace):
        manager = self.manager
        receipt = manager._read(Path(workspace["receipt_path"]))
        if (
            not isinstance(receipt, dict)
            or receipt.get("candidate_prepared") is not True
        ):
            self._refuse(
                "The prepared conversion receipt is unavailable. Restore v1 before preparing again."
            )
        manager._validate_receipt(job, receipt)
        if job.get("prepared_binding") != self._binding(job, workspace):
            self._refuse(
                "The prepared database, runtime or migration proof changed. Keep recovery files and restore v1."
            )
        # Reuse the accepted conversion. Revalidate retained originals/archive,
        # never rerun capture, staging or native conversion.
        manager._accept(job, copy.deepcopy(receipt), persist_proof=False)
        return receipt

    def start(self, request):
        manager = self.manager
        with INFINIDYSK_MIGRATION_ADMISSION_LOCK, manager._lock:
            job = manager._read(manager._job_path(request.get("job_id")))
            if (
                not job
                or job.get("status") != "prepared"
                or job.get("mode") != "prepare"
                or job.get("activation_exposed")
            ):
                self._refuse("There is no stopped prepared database to activate.")
            if request.get("confirmation") != f"ACTIVATE INFINIDYSK V2 {job['job_id']}":
                self._refuse("Confirm activation of this exact prepared database.")
            report = job.get("import_report") or {}
            if report.get("review_required") and (
                (request.get("acknowledgements") or {}).get("skipped_media") is not True
                or request.get("skip_review_digest") != report.get("review_digest")
            ):
                self._refuse(
                    "Review and acknowledge the skipped items from this prepared conversion before activation."
                )
            from utils.infinidysk_v2_migration import ACTIVE, RECOVERY, PREPARED

            if any(
                item.get("job_id") != job["job_id"]
                and manager._effective_status(item) in ACTIVE | RECOVERY | PREPARED
                for item in manager._jobs()
            ) or (manager._cleanup is not None and manager._cleanup.has_blocking_job()):
                self._refuse("Another migration owns recovery admission.")
            job.update(
                status="queued",
                stage="activating",
                worker_id=manager._worker_id,
                updated_at=int(time.time()),
            )
            manager._active_id = job["job_id"]
            manager._thread = threading.Thread(
                target=manager._run,
                args=(job,),
                kwargs={"activating": True},
                name=f"infinidysk-activate-{job['job_id'][:8]}",
                daemon=True,
            )
            manager._write(manager._job_path(job["job_id"]), job)
            try:
                manager._thread.start()
            except Exception:
                job.update(status="prepared", stage="awaiting_activation")
                manager._active_id = None
                manager._write(manager._job_path(job["job_id"]), job)
                raise
            return {"job": manager._public_job(job)}


def defer_cleanup(manager, job):
    """Optional deletion never delays application resumption or changes its result."""
    from utils.infinidysk_v2_blob_staging import ExternalBlobStaging

    terminal = {"completed", "rehearsed", "failed_restored", "cancelled", "rolled_back"}
    job_id = job["job_id"]

    class CleanupManager:
        _rollback_active = False

        def __getattr__(self, name):
            return getattr(manager, name)

        def _assert_job_owner(self, unused):
            with manager._lock:
                current = manager._read(manager._job_path(job_id))
                if not current or current.get("status") not in terminal:
                    raise ValueError(
                        "Optional staging cleanup no longer owns this finished job."
                    )

        def _progress(self, *args, **kwargs):
            self._assert_job_owner(None)

        def _recovery_progress(self, *args, **kwargs):
            self._assert_job_owner(None)

        def _write(self, path, payload):
            if Path(path) == manager._job_path(job_id):
                # The cleanup worker only owns its staging record, never the job
                # state, activation result or later recovery journal.
                with manager._lock:
                    self._assert_job_owner(None)
                    current = manager._read(Path(path))
                    current["snapshot"]["external_blob_staging"] = copy.deepcopy(
                        payload["snapshot"]["external_blob_staging"]
                    )
                    if (
                        payload["snapshot"]["external_blob_staging"].get("state")
                        == "cleaned"
                    ):
                        current.pop("staging_cleanup_required", None)
                    manager._write(Path(path), current)
            else:
                manager._write(path, payload)

    with manager._lock:
        current = manager._read(manager._job_path(job_id))
        if not current or current.get("status") not in terminal:
            return
        current["staging_cleanup_required"] = True
        manager._write(manager._job_path(job_id), current)

    def run():
        controller = CleanupManager()
        try:
            controller._assert_job_owner(None)
            private = manager._read(manager._job_path(job_id))
            ExternalBlobStaging(controller).cleanup(private)
        except Exception:
            # All remaining inputs and recovery archives stay retained. A later
            # reviewed cleanup can retry; service success is already durable.
            pass

    threading.Thread(
        target=run, name=f"infinidysk-cleanup-{job_id[:8]}", daemon=True
    ).start()
