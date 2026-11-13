"""Resume accepted generation 2 without restoring or modifying retained v1 data."""

import copy
import hashlib
import os
from pathlib import Path
import stat
import secrets
import socket
import threading
import time

from utils.infinidysk_migration_admission import (
    INFINIDYSK_MIGRATION_ADMISSION_LOCK,
    InfiniDyskMigrationAdmissionError,
    v2_migration_owner,
)
from utils.infinidysk_v2_migration import (
    ACTIVE,
    RECOVERY,
    InfiniDyskV2MigrationError,
    _digest,
    _identity,
    _path,
)
from utils.infinidysk_v2_runtime import (
    AUTH_FILE,
    MARKER,
    installed_v2_version,
    native_api_request,
    native_auth,
    native_database_dependency_urls,
    resolve_v2_data_dir,
    runtime_dir,
)
from utils.infinidysk_v2_topology import (
    delivery_mount_ready,
    mount_records,
    selected_topology,
    validate_selected_owners,
)

_MESSAGES = {
    "target_changed": "The accepted generation 2 configuration, runtime or data identity changed; recovery remains held.",
    "receipt_changed": "The accepted generation 2 receipt could not be verified; recovery remains held.",
    "scope_changed": "The exact retained consumer scope or configuration changed; recovery remains held.",
    "target_unready": "The accepted generation 2 runtime and delivery mount are not ready; generation 2 data remains in place.",
    "target_start_failed": "The accepted generation 2 owner did not start; generation 2 data remains in place.",
    "owner_unregistered": "An unregistered delivery owner may still be live; duplicate startup is refused.",
    "consumer_start_failed": "A retained consumer did not start; later dependants remain pending.",
    "worker_changed": "The recovery worker no longer owns this operation; generation 2 state remains protected.",
    "recovery_failed": "Generation 2 recovery did not finish; existing generation 2 writes and retained v1 evidence remain protected.",
}


class ConsumerRecoveryError(InfiniDyskV2MigrationError):
    def __init__(self, code):
        self.reason_code = code if code in _MESSAGES else "recovery_failed"
        super().__init__(_MESSAGES[self.reason_code])


def _file_binding(path, limit, *, uid=None, private=False):
    path = _path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or (uid is not None and before.st_uid != uid)
            or before.st_nlink != 1
            or (private and stat.S_IMODE(before.st_mode) & 0o077)
            or before.st_size > limit
        ):
            raise ConsumerRecoveryError("target_changed")
        content = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if len(content) > limit or any(
            getattr(before, field) != getattr(after, field) for field in fields
        ):
            raise ConsumerRecoveryError("target_changed")
        return {
            "identity": {
                "device": before.st_dev,
                "inode": before.st_ino,
                "mode": stat.S_IFMT(before.st_mode),
            },
            "sha256": hashlib.sha256(content).hexdigest(),
        }


def _credential_binding(service, data_root, uid):
    native_auth(service, data_root)
    return _file_binding(
        Path(resolve_v2_data_dir(service, data_root)) / AUTH_FILE,
        8192,
        uid=uid,
        private=True,
    )


class InfiniDyskV2ConsumerRecovery:
    def __init__(self, manager):
        self.manager = manager

    def _save(self, job):
        job["updated_at"] = int(time.time())
        self.manager._write(self.manager._job_path(job["job_id"]), job)

    def _record(self, job):
        record = job.get("consumer_recovery")
        if (
            job.get("activation_exposed") is not True
            or not isinstance(record, dict)
            or record.get("schema") != 1
        ):
            raise ConsumerRecoveryError("receipt_changed")
        return record

    def _scope(self, job, names):
        executor, plan = self.manager.executor, job["plan"]
        if (
            not isinstance(names, list)
            or any(not isinstance(name, str) for name in names)
            or len(names) != len(set(names))
            or not set(names).issubset(executor._names(plan))
        ):
            raise ConsumerRecoveryError("scope_changed")
        service = self.manager._config().get("infinidysk") or {}
        ordered = executor._ordered_names(
            plan, executor._target_names(plan, names), service=service
        )
        owners = {plan["source"]["process_name"]} | {
            mount.get("process_name") for mount in plan["inventory"]["mounts"]
        }
        consumers = [name for name in ordered if name not in owners]
        bindings = []
        configuration = self.manager._config()
        for name in ordered:
            entries = []
            for key, section in configuration.items():
                if not isinstance(section, dict):
                    continue
                instances = section.get("instances")
                for entry in (
                    instances.values() if isinstance(instances, dict) else [section]
                ):
                    if isinstance(entry, dict) and entry.get("process_name") == name:
                        entries.append({"key": key, "service": entry})
            if len(entries) != 1:
                raise ConsumerRecoveryError("scope_changed")
            bindings.append({"name": name, "sha256": _digest(entries[0])})
        return consumers, bindings

    def _target_binding(self, job, service):
        try:
            return self._target_binding_unchecked(job, service)
        except ConsumerRecoveryError:
            raise
        except Exception:
            raise ConsumerRecoveryError("target_changed") from None

    def _target_binding_unchecked(self, job, service):
        plan, configuration = job["plan"], self.manager._config()
        if (
            service != configuration.get("infinidysk")
            or str(service.get("runtime_generation")) != "2"
            or _digest(self.manager._owned_for_job(configuration, job))
            != job.get("activation_fingerprint")
            or str(runtime_dir(service)) != plan["target"]["runtime_dir"]
            or resolve_v2_data_dir(service, configuration.get("data_root") or "/data")
            != plan["target"]["data_dir"]
            or not installed_v2_version(service)
        ):
            raise ConsumerRecoveryError("target_changed")
        data = _path(plan["target"]["data_dir"])
        runtime = _path(plan["target"]["runtime_dir"])
        database = _path(data / "infinidysk.sqlite")
        binary = _path(runtime / "infinidysk")
        identities = {
            "data": _identity(data),
            "database": _identity(database),
            "runtime": _identity(runtime),
            "binary": _identity(binary),
        }
        for key, required in (
            ("data", stat.S_IFDIR),
            ("database", stat.S_IFREG),
            ("runtime", stat.S_IFDIR),
            ("binary", stat.S_IFREG),
        ):
            if not identities[key] or identities[key]["mode"] != required:
                raise ConsumerRecoveryError("target_changed")
        instances = (configuration.get("rclone") or {}).get("instances") or {}
        topology = selected_topology(service, instances)
        if (
            not validate_selected_owners(service, instances)[0]
            or len(topology) != 1
            or topology[0]["path"] != job["plan"]["target"]["mount_path"]
        ):
            raise ConsumerRecoveryError("target_changed")
        data_root = configuration.get("data_root") or "/data"
        providers = native_database_dependency_urls(service, data_root)
        if providers != (f"sqlite:{database}",):
            raise ConsumerRecoveryError("target_changed")
        uid = int(configuration.get("puid") or 1000)
        return {
            **identities,
            "provider_sha256": _digest(providers),
            "runtime_provenance": _file_binding(runtime / MARKER, 8192),
            "configuration": job["activation_fingerprint"],
            "credentials": _credential_binding(
                service, configuration.get("data_root") or "/data", uid
            ),
        }

    def _receipt_path(self, job):
        return _path(
            Path(self.manager._workspace(job)["root"]) / "activation-receipt.json"
        )

    def _validate(self, job):
        try:
            record = self._record(job)
            if record.get("plan_fingerprint") != job.get(
                "plan_fingerprint"
            ) or not job.get("accepted_import"):
                raise ConsumerRecoveryError("receipt_changed")
            receipt = self.manager._read(self._receipt_path(job))
            if not receipt or _digest(receipt) != record.get("receipt_sha256"):
                raise ConsumerRecoveryError("receipt_changed")
            self.manager._validate_receipt(job, receipt)
            service = self.manager._config().get("infinidysk") or {}
            if self._target_binding(job, service) != record.get("target_binding"):
                raise ConsumerRecoveryError("target_changed")
            consumers, bindings = self._scope(job, record.get("retained_names"))
            if bindings != record.get("scope_bindings") or consumers != record.get(
                "consumers"
            ):
                raise ConsumerRecoveryError("scope_changed")
            return service, receipt, consumers
        except ConsumerRecoveryError:
            raise
        except Exception:
            raise ConsumerRecoveryError("target_changed") from None

    def expose(self, job, service, receipt):
        manager = self.manager
        with INFINIDYSK_MIGRATION_ADMISSION_LOCK, manager._lock:
            manager._assert_job_owner(job)
            manager._check_cancel(job)
            if (
                job.get("mode") != "cutover"
                or not job.get("accepted_import")
                or job.get("activation_exposed")
            ):
                raise ConsumerRecoveryError("receipt_changed")
            manager._validate_receipt(job, receipt)
            names = copy.deepcopy(
                (job.get("execution") or {}).get("original_running_names")
            )
            consumers, bindings = self._scope(job, names)
            target = self._target_binding(job, service)
            candidate = (job.get("snapshot") or {}).get("candidate_identity")
            if candidate != target["data"]:
                raise ConsumerRecoveryError("target_changed")
            private_receipt = copy.deepcopy(receipt)
            private_receipt.pop("native_report_private", None)
            manager._write(self._receipt_path(job), private_receipt)
            manager._check_cancel(job)
            job["consumer_recovery"] = {
                "schema": 1,
                "kind": "v2_activation",
                "ready": False,
                "plan_fingerprint": job["plan_fingerprint"],
                "receipt_sha256": _digest(private_receipt),
                "target_binding": target,
                "retained_names": names,
                "scope_bindings": bindings,
                "consumers": consumers,
                "pending": consumers,
                "resumed": [],
                "failed": [],
            }
            job["activation_exposed"] = True
            job["activation_committed"] = False
            self._save(job)

    def _ready(self, service):
        executor = self.manager.executor
        if not executor._running(service.get("process_name") or "InfiniDysk"):
            return False
        try:
            instances = (self.manager._config().get("rclone") or {}).get(
                "instances"
            ) or {}
            return native_api_request(service, "GET", "/healthz", timeout=2)[
                "status"
            ] == 200 and delivery_mount_ready(service, instances)
        except Exception:
            return False

    def commit(self, job, receipt):
        self._owner(job)
        service, accepted, _consumers = self._validate(job)
        if _digest(receipt) != _digest(accepted) or not self._ready(service):
            raise ConsumerRecoveryError("target_unready")
        with self.manager._lock:
            self._owner(job)
            record = self._record(job)
            record.update(kind="v2_consumers", ready=True)
            job["activation_committed"] = True
            self._save(job)

    def _start_prepared(self, name, code):
        executor = self.manager.executor
        if executor._running(name):
            return
        start = getattr(executor.handler, "start_prepared_process", None)
        if not callable(start):
            raise ConsumerRecoveryError(code)
        try:
            success, _error = start(name)
        except Exception:
            raise ConsumerRecoveryError(code) from None
        if not success:
            raise ConsumerRecoveryError(code)

    def resume(self, job):
        self._owner(job)
        service, _receipt, consumers = self._validate(job)
        if not job.get("activation_committed") or not self._ready(service):
            raise ConsumerRecoveryError("target_unready")
        record, executor = self._record(job), self.manager.executor
        record.update(
            failed=[],
            pending=[name for name in consumers if not executor._running(name)],
            resumed=[name for name in consumers if executor._running(name)],
            ready=True,
        )
        self._save(job)
        for name in consumers:
            self._owner(job)
            self._validate(job)
            if not self._ready(service):
                raise ConsumerRecoveryError("target_unready")
            try:
                self._start_prepared(name, "consumer_start_failed")
                self._owner(job)
                if not executor._running(name):
                    raise ConsumerRecoveryError("consumer_start_failed")
            except ConsumerRecoveryError as error:
                if error.reason_code != "consumer_start_failed":
                    raise
                record["failed"] = [
                    {
                        "process_name": name,
                        "reason_code": "consumer_start_failed",
                        "message": _MESSAGES["consumer_start_failed"],
                    }
                ]
                self._save(job)
                raise
            except Exception:
                record["failed"] = [
                    {
                        "process_name": name,
                        "reason_code": "consumer_start_failed",
                        "message": _MESSAGES["consumer_start_failed"],
                    }
                ]
                self._save(job)
                raise ConsumerRecoveryError("consumer_start_failed") from None
            record["pending"] = [
                pending for pending in record["pending"] if pending != name
            ]
            if name not in record["resumed"]:
                record["resumed"].append(name)
            self._save(job)
        return copy.deepcopy(record)

    def hold(self, job, error):
        service = self.manager._config().get("infinidysk") or {}
        ready = self._ready(service)
        with self.manager._lock:
            if self.manager._active_id is not None:
                try:
                    self._owner(job)
                except ConsumerRecoveryError:
                    return
            record = self._record(job)
            code = getattr(error, "reason_code", "recovery_failed")
            code = code if code in _MESSAGES else "recovery_failed"
            record["ready"] = ready
            record["kind"] = (
                "v2_consumers" if job.get("activation_committed") else "v2_activation"
            )
            job.update(
                status="recovery_required",
                stage=record["kind"],
                message="Generation 2 state is retained. Retry the accepted target and retained consumers, or explicitly finish once the target is ready.",
                error=_MESSAGES[code],
                recovery_kind=record["kind"],
                result={
                    "validated": True,
                    "execution_verified": True,
                    "v2_writes_preserved": True,
                },
            )
            self._save(job)

    def public(self, job):
        record = job.get("consumer_recovery") or {}
        exposed = job.get("activation_exposed") is True
        valid = exposed and record.get("schema") == 1
        held = (
            valid
            and self.manager._effective_status(job) in RECOVERY
            and self.manager._owned_process_cleanup_complete(job)
        )
        ready = False
        if valid and record.get("ready"):
            try:
                service = self.manager._config().get("infinidysk") or {}
                instances = (self.manager._config().get("rclone") or {}).get(
                    "instances"
                ) or {}
                ready = self.manager.executor._running(
                    service.get("process_name") or "InfiniDysk"
                ) and delivery_mount_ready(service, instances)
            except Exception:
                pass
        return {
            "recovery_kind": record.get("kind") if exposed else None,
            "consumer_recovery": (
                {
                    "failed": copy.deepcopy(record.get("failed") or []),
                    "pending": list(record.get("pending") or []),
                    "resumed": list(record.get("resumed") or []),
                    "ready": bool(ready),
                }
                if valid
                else None
            ),
            "can_retry_consumers": bool(held),
            "can_finish_consumer_recovery": bool(held and ready),
            "confirmation_texts": {
                "retry_consumers": f"RETRY CONSUMERS {job['job_id']}",
                "finish_consumer_recovery": f"FINISH CONSUMER RECOVERY {job['job_id']}",
            },
        }

    def _orphan_mount_guard(self, service):
        instances = (self.manager._config().get("rclone") or {}).get("instances") or {}
        owners = selected_topology(service, instances)
        if len(owners) != 1:
            raise ConsumerRecoveryError("target_changed")
        if self.manager.executor._running(owners[0].get("process_name")):
            return
        target = Path(service.get("mount_path") or "/mnt/debrid/infinidysk")
        try:
            if any(
                Path(row["path"]) == target or target in Path(row["path"]).parents
                for row in mount_records()
            ):
                raise ConsumerRecoveryError("owner_unregistered")
        except ConsumerRecoveryError:
            raise
        except Exception:
            raise ConsumerRecoveryError("owner_unregistered") from None

    def _orphan_listener_guard(self, service):
        try:
            native_api_request(service, "GET", "/healthz", timeout=2)
        except Exception:
            pass
        else:
            # Any HTTP response proves a listener, including auth/server errors.
            raise ConsumerRecoveryError("owner_unregistered")
        try:
            port = int(service.get("frontend_port") or 3000)
            if not 1 <= port <= 65535:
                raise ConsumerRecoveryError("target_changed")
            connection = socket.create_connection(("127.0.0.1", port), timeout=1)
        except ConnectionRefusedError:
            return
        except ConsumerRecoveryError:
            raise
        except Exception:
            raise ConsumerRecoveryError("owner_unregistered") from None
        else:
            connection.close()
            raise ConsumerRecoveryError("owner_unregistered")

    def _retry_target(self, job):
        service, receipt, _consumers = self._validate(job)
        executor = self.manager.executor
        name = job["plan"]["source"]["process_name"]
        if not executor._running(name):
            self._orphan_mount_guard(service)
            self._orphan_listener_guard(service)
            try:
                self._start_prepared(name, "target_start_failed")
            except Exception:
                raise ConsumerRecoveryError("target_start_failed") from None
        deadline = time.monotonic() + 120
        while True:
            self._owner(job)
            try:
                if (
                    native_api_request(service, "GET", "/healthz", timeout=2).get(
                        "status"
                    )
                    == 200
                ):
                    break
            except Exception:
                pass
            if time.monotonic() >= deadline or getattr(
                executor.handler, "shutting_down", False
            ):
                raise ConsumerRecoveryError("target_unready")
            time.sleep(0.25)
        instances = (self.manager._config().get("rclone") or {}).get("instances") or {}
        owners = selected_topology(service, instances)
        if len(owners) != 1:
            raise ConsumerRecoveryError("target_changed")
        for owner in owners:
            owner_name = owner.get("process_name")
            if owner["backend"] == "rclone" and not executor._running(owner_name):
                self._orphan_mount_guard(service)
                try:
                    self._start_prepared(owner_name, "target_start_failed")
                except Exception:
                    raise ConsumerRecoveryError("target_start_failed") from None
        try:
            executor._wait_delivery(service)
        except Exception:
            raise ConsumerRecoveryError("target_unready") from None
        self.commit(job, receipt)

    def _owner(self, job):
        if (
            self.manager._active_id != job["job_id"]
            or job.get("worker_id") != self.manager._worker_id
        ):
            raise ConsumerRecoveryError("worker_changed")
        latest = self.manager._read(self.manager._job_path(job["job_id"]))
        if (
            not latest
            or latest.get("activation_fingerprint") != job.get("activation_fingerprint")
            or latest.get("consumer_recovery", {}).get("target_binding")
            != self._record(job).get("target_binding")
            or latest.get("consumer_recovery", {}).get("worker_token")
            != self._record(job).get("worker_token")
        ):
            raise ConsumerRecoveryError("worker_changed")

    def _run(self, job, update_lock, finish):
        try:
            with v2_migration_owner("major"):
                self._owner(job)
                job.update(
                    status="running",
                    stage="v2_consumer_recovery",
                    message="Verifying the accepted generation 2 target; existing writes remain in place.",
                )
                self._save(job)
                if finish:
                    service, receipt, consumers = self._validate(job)
                    if not self._ready(service):
                        raise ConsumerRecoveryError("target_unready")
                    self.commit(job, receipt)
                    record = self._record(job)
                    record.update(
                        ready=True,
                        pending=[
                            name
                            for name in consumers
                            if not self.manager.executor._running(name)
                        ],
                        resumed=[
                            name
                            for name in consumers
                            if self.manager.executor._running(name)
                        ],
                        pending_acknowledged=True,
                    )
                else:
                    self._retry_target(job)
                    self.resume(job)
                self._owner(job)
                _service, receipt, _consumers = self._validate(job)
                self.manager._write(
                    Path(self.manager._workspace(job)["receipt_path"]),
                    {**receipt, "generation_2_started": True},
                )
                restart = (
                    getattr(self.manager.process_handler, "startup_phase", None)
                    == "recovery_required"
                )
                job.update(
                    status="completed",
                    stage="completed",
                    message=(
                        "Consumer recovery finished; restart DUMB to resume normal background workers. Existing generation 2 writes remain in place."
                        if restart
                        else "Generation 2 target verified; consumer recovery finished. Existing generation 2 writes and retained v1 evidence remain in place."
                    ),
                    error=None,
                    result={
                        "validated": True,
                        "execution_verified": True,
                        "v2_writes_preserved": True,
                        "restart_required": restart,
                        "pending_consumers_acknowledged": bool(
                            self._record(job).get("pending_acknowledged")
                        ),
                    },
                )
                self._save(job)
        except Exception as error:
            with self.manager._lock:
                try:
                    self._owner(job)
                    owned = True
                except ConsumerRecoveryError:
                    owned = False
            if owned:
                self.hold(job, error)
        finally:
            with self.manager._lock:
                if (
                    self.manager._active_id == job["job_id"]
                    and self.manager._thread is threading.current_thread()
                ):
                    self.manager._active_id = None
                update_lock.release()

    def start(self, request, finish=False):
        manager = self.manager
        update_lock = None
        try:
            with INFINIDYSK_MIGRATION_ADMISSION_LOCK, manager._lock:
                from utils.infinidysk_migration_admission import (
                    infinidysk_external_mutation_active,
                    infinidysk_namespace_migration_active,
                    infinidysk_postgres_migration_active,
                    infinidysk_namespace_recovery_pending,
                    infinidysk_postgres_recovery_pending,
                )

                with v2_migration_owner("major"):
                    if (
                        infinidysk_external_mutation_active()
                        or infinidysk_namespace_migration_active()
                        or infinidysk_postgres_migration_active()
                        or infinidysk_namespace_recovery_pending()
                        or infinidysk_postgres_recovery_pending()
                    ):
                        raise InfiniDyskV2MigrationError(
                            "Another managed mutation owns migration admission."
                        )
                job = manager._read(manager._job_path(request.get("job_id")))
                if not job or not self.public(job)["can_retry_consumers"]:
                    raise ConsumerRecoveryError("receipt_changed")
                phrase = (
                    "FINISH CONSUMER RECOVERY " if finish else "RETRY CONSUMERS "
                ) + job["job_id"]
                if request.get("confirmation") != phrase:
                    raise InfiniDyskV2MigrationError(
                        "Confirm the exact generation 2 consumer recovery action."
                    )
                if finish and request.get("acknowledge_pending_consumers") is not True:
                    raise InfiniDyskV2MigrationError(
                        "Explicitly acknowledge the retained pending consumers before finishing recovery."
                    )
                if any(
                    other["job_id"] != job["job_id"]
                    and manager._effective_status(other) in ACTIVE | RECOVERY
                    for other in manager._jobs()
                ):
                    raise InfiniDyskV2MigrationError(
                        "Another migration owns lifecycle admission."
                    )
                if manager._active_id is not None or (
                    manager._thread is not None and manager._thread.is_alive()
                ):
                    raise InfiniDyskV2MigrationError(
                        "A migration worker is already active."
                    )
                self._validate(job)
                if finish and not self.public(job)["can_finish_consumer_recovery"]:
                    raise ConsumerRecoveryError("target_unready")
                update_lock = getattr(manager.updater, "updating", None)
                if update_lock is None or not update_lock.acquire(blocking=False):
                    update_lock = None
                    raise InfiniDyskV2MigrationError("A service update is active.")
                manager._active_id = job["job_id"]
                manager._cancel_requested.discard(job["job_id"])
                job.update(
                    status="queued",
                    stage="v2_consumer_recovery",
                    worker_id=manager._worker_id,
                    cancel_requested=False,
                )
                self._record(job)["worker_token"] = secrets.token_hex(16)
                self._save(job)
                manager._thread = threading.Thread(
                    target=self._run,
                    args=(job, update_lock, finish),
                    name="infinidysk-v2-consumer-recovery",
                    daemon=True,
                )
                manager._thread.start()
                update_lock = None
                return {"job": manager._public_job(job)}
        except InfiniDyskMigrationAdmissionError:
            raise InfiniDyskV2MigrationError(
                "Another managed mutation owns migration admission."
            ) from None
        finally:
            if update_lock is not None:
                with manager._lock:
                    manager._active_id = None
                    update_lock.release()
