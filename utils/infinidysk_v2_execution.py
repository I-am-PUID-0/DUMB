"""Contained native import execution and explicit original-data rollback."""

from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.parse import quote
import uuid

from utils.infinidysk_v2_credentials import (
    legacy_sab_environment,
    source_sab_key,
    validate_target_sab_override,
    verify_imported_sab_key,
)
from utils.infinidysk_v2_fuse import (
    safe_native_preparation_error,
    validate_fuse_prerequisites,
)
from utils.infinidysk_v2_runtime import (
    build_v2_environment,
    configure_v2_runtime,
    install_v2_runtime,
    native_api_request,
    native_auth,
    runtime_dir,
)
from utils.private_files import atomic_write_private_text
from utils.infinidysk_v2_rehearsal_cleanup import discard_rehearsal_candidate
from utils.infinidysk_v2_nntp import NntpBridge, NntpBridgeError, read_source_providers
from utils.infinidysk_v2_pg_snapshot import (
    InfiniDyskV2PostgresSnapshotError,
    create_postgres_snapshot,
    inspect_postgres_source,
)
from utils.infinidysk_v2_restore import postgres_tools, verify_postgres_restore
from utils.infinidysk_v2_subprocess import MigrationProcessCleanupError
from utils.installation_backup_lifecycle import BackupLifecycleError, order_resume_rows
from utils.infinidysk_v2_topology import (
    delivery_mount_ready,
    mount_records,
    selected_topology,
)
from utils.runtime_paths import project_path
from scripts.infinidysk_v2_sandbox import namespace_command


class ContainedImportError(ValueError):
    pass


def _native_group_has_live_members(group):
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    with os.scandir("/proc") as entries:
        count = 0
        for entry in entries:
            if not entry.name.isdigit():
                continue
            count += 1
            if count > 16384:
                raise ContainedImportError(
                    "The native process-group check exceeds its bound."
                )
            try:
                fields = (
                    Path(entry.path, "stat").read_bytes().rpartition(b")")[2].split()
                )
            except FileNotFoundError:
                continue
            if len(fields) < 3:
                raise ContainedImportError(
                    "Native process-group shutdown could not be verified."
                )
            if int(fields[2]) == group and fields[0] not in {b"Z", b"X"}:
                return True
    return False


_SUMMARY = re.compile(
    r"Imported (\d+) directories, (\d+) files, (\d+) NZBs, (\d+) history rows, (\d+) queued jobs"
)


def _uuid(value):
    return (
        uuid.UUID(bytes=bytes(value)).hex
        if isinstance(value, (bytes, bytearray))
        else uuid.UUID(str(value)).hex
    )


def verify_import_inventory(source, destination, report, *, source_blob_root=None):
    from utils.infinidysk_v2_import_report import (
        ImportReportError,
        verify_import_inventory as verify,
    )

    try:
        return verify(source, destination, report, source_blob_root=source_blob_root)
    except ImportReportError as error:
        refused = ContainedImportError(str(error))
        refused.import_report = error.import_report
        raise refused from error


class NativeCandidate:
    """Own and reap the entire contained candidate process group."""

    def __init__(
        self,
        service,
        candidate,
        private,
        uid,
        gid,
        timezone,
        legacy_env,
        *,
        timeout=30 * 60,
        check_cancel=None,
        record_report=None,
    ):
        self.service, self.candidate, self.private = (
            service,
            Path(candidate),
            Path(private),
        )
        self.uid, self.gid, self.timeout = uid, gid, timeout
        self.check_cancel = check_cancel or (lambda: None)
        self.record_report = record_report or (lambda *args: None)
        self.process = None
        self.stopped = False
        self.output = None
        self.port = 17831  # A fresh private network namespace has no other listener.
        self.payload_path = self.private / "candidate-payload.json"
        self.temporary = self.candidate / ".dumb-import-tmp"
        self.temporary.mkdir(mode=0o700)
        os.chown(self.temporary, uid, gid)
        self.nntp_bridge = None
        self.native_catalog = None
        database = self.candidate / "db.sqlite"
        if database.is_file():
            providers = read_source_providers(database, legacy_env)
            if providers:
                self.nntp_bridge = NntpBridge(providers, self.temporary, uid, gid)
        environment = build_v2_environment(service, uid=uid, gid=gid)
        environment.update(
            {
                key: str(value)
                for key, value in legacy_env.items()
                if key
                in {
                    "DATABASE_PROVIDER",
                    "FRONTEND_BACKEND_API_KEY",
                    "CATEGORIES",
                    "TRUSTED_PROXY_CIDRS",
                    "WEBDAV_USER",
                    "WEBDAV_PASSWORD",
                }
            }
        )
        environment.update(legacy_sab_environment(legacy_env))
        environment.update(
            {
                "INFINIDYSK_LISTEN": f"127.0.0.1:{self.port}",
                "INFINIDYSK__MOUNT__FUSE": "false",
                "INFINIDYSK__MOUNT__DIR": str(self.temporary / "mount"),
                "INFINIDYSK__ARR_INSTANCES": "[]",
                "INFINIDYSK__ARR__QUEUE_MANAGEMENT": "false",
                "INFINIDYSK__IMPORTS__LIBRARY_DIRS": "[]",
            }
        )
        self.payload = {
            "data_dir": str(candidate),
            "tmp_dir": str(self.temporary),
            "proof_path": str(self.temporary / "isolation.json"),
            "uid": uid,
            "gid": gid,
            "port": self.port,
            "tz": timezone or "UTC",
            "environment": environment,
            "binary": str(runtime_dir(service) / "infinidysk"),
        }

    def __enter__(self):
        try:
            if self.nntp_bridge is not None:
                self.nntp_bridge.start()
                self.payload["nntp_bridge"] = self.nntp_bridge.payload
            atomic_write_private_text(self.payload_path, json.dumps(self.payload))
            path = self.private / "candidate.log"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.output = os.fdopen(descriptor, "wb")
            self.process = subprocess.Popen(
                namespace_command(
                    self.uid,
                    self.gid,
                    [
                        sys.executable,
                        str(project_path("scripts", "infinidysk_v2_sandbox.py")),
                        str(self.payload_path),
                    ],
                ),
                stdout=self.output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            return self
        except BaseException:
            if self.output is not None:
                self.output.close()
            self.payload_path.unlink(missing_ok=True)
            if self.nntp_bridge is not None:
                self.nntp_bridge.close()
            raise

    def probe(self, path):
        if self.process.poll() is not None:
            raise ContainedImportError(
                "The contained importer exited; inspect its private report."
            )
        result = subprocess.run(
            [
                "nsenter",
                f"--net=/proc/{self.process.pid}/ns/net",
                "--",
                sys.executable,
                str(project_path("scripts", "infinidysk_v2_sandbox.py")),
                str(self.payload_path),
                "probe",
                "GET",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if result.returncode:
            return None
        return json.loads(result.stdout)

    def wait_import(self, migrate, progress):
        started = time.monotonic()
        last_activity = started
        last_state = None
        last_published = float("-inf")
        native_progress = {}
        stage = "starting"
        while True:
            self.check_cancel()
            now = time.monotonic()
            bridge = getattr(self, "nntp_bridge", None)
            activity = getattr(bridge, "last_activity", None)
            if isinstance(activity, (int, float)) and activity <= now:
                last_activity = max(last_activity, activity)
            try:
                reply = self.probe("/api/v1/upgrade/status")
            except (OSError, subprocess.TimeoutExpired):
                if self.process.poll() is not None:
                    raise ContainedImportError(
                        "The contained importer exited; inspect its private report."
                    ) from None
                # A bounded status request can fail while the importer is busy.
                # Missing observations and polling heartbeats are not progress.
                reply = None
            state = None
            changed = False
            if reply and reply["status"] == 200:
                state = reply["body"]
                if state.get("state") == "failed":
                    raise ContainedImportError(
                        "The isolated native import failed; provider access may be required. No original data was changed."
                    )
                native_progress = state.get("progress") or {}
                signature = json.dumps(
                    {
                        "state": state.get("state"),
                        "progress": {
                            key: native_progress.get(key)
                            for key in (
                                "stage",
                                "done",
                                "total",
                                "skipped",
                                "processed",
                                "files_done",
                                "files_total",
                            )
                        },
                    },
                    sort_keys=True,
                )
                changed = signature != last_state
                if changed:
                    last_activity = now
                    last_state = signature
                stage = str(
                    native_progress.get("stage") or state.get("state") or "starting"
                )
                if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]{0,63}", stage):
                    stage = "importing"
            if changed or now - last_published >= 2:
                safe = {"stage": stage, "elapsed_seconds": int(now - started)}
                for public, alternatives in (
                    ("processed", ("processed", "done", "current", "files_done")),
                    ("total", ("total", "files_total")),
                ):
                    for key in alternatives:
                        value = native_progress.get(key)
                        if type(value) is int and 0 <= value <= 2**63 - 1:
                            safe[public] = value
                            break
                if bridge is not None:
                    stats = bridge.stats
                    safe["provider_connections"] = stats["active_connections"]
                    safe["provider_requests"] = stats["commands"]
                inactive = now - last_activity
                if inactive >= self.timeout:
                    message = (
                        f"No observable native progress or provider reads for {int(inactive)} seconds. "
                        "The importer is still running; review its progress or use Cancel migration to stop it and restore v1."
                    )
                    if state is None:
                        message += " Native progress status is currently unavailable."
                elif state is None:
                    message = (
                        "Native progress status is currently unavailable; the importer is still running. "
                        "DUMB is waiting for another bounded status check."
                    )
                else:
                    message = f"Contained native stage: {stage}"
                progress("importing", message, import_progress=safe)
                last_published = now
            if state and state.get("state") == ("done" if migrate else "none"):
                proof = json.loads(Path(self.payload["proof_path"]).read_text())
                actual_namespace = os.readlink(f"/proc/{self.process.pid}/ns/net")
                if (
                    proof.get("pid") != self.process.pid
                    or proof.get("landlock_abi", 0) < 3
                    or proof.get("external_network") is not False
                    or actual_namespace != proof.get("network_namespace")
                    or actual_namespace == os.readlink("/proc/self/ns/net")
                ):
                    raise ContainedImportError(
                        "The native importer confinement proof did not validate."
                    )
                if migrate:
                    report = self.probe("/api/v1/upgrade/report")
                    if (
                        not report
                        or report["status"] != 200
                        or not isinstance(report["body"].get("report"), str)
                    ):
                        raise ContainedImportError(
                            "The authenticated native import report is unavailable."
                        )
                    self.record_report(report["body"]["report"])
                    if bridge is not None:
                        catalog = self.probe("/api/v1/catalog")
                        if not catalog or catalog["status"] != 200:
                            raise ContainedImportError(
                                "The native provider catalog is unavailable; original provider settings cannot be restored safely."
                            )
                        self.native_catalog = catalog["body"]
                    return report["body"]["report"]
                return ""
            time.sleep(0.25)

    def __exit__(self, *unused):
        try:
            try:
                if self.process is not None:
                    try:
                        os.killpg(self.process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(self.process.pid, signal.SIGKILL)
                        self.process.wait(timeout=5)
                    # Terminate descendants, then prove no writer survived the signal.
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            finally:
                if self.output:
                    self.output.close()
                self.payload_path.unlink(missing_ok=True)
            if self.process is not None:
                deadline = time.monotonic() + 3
                while _native_group_has_live_members(self.process.pid):
                    if time.monotonic() >= deadline:
                        raise MigrationProcessCleanupError(
                            "The native candidate still has live writers; its data was retained.",
                            process_group=self.process.pid,
                        )
                    time.sleep(0.05)
                self.stopped = self.process.poll() is not None
        except (OSError, subprocess.SubprocessError, ContainedImportError):
            if self.process is not None:
                raise MigrationProcessCleanupError(
                    "Native helper termination could not be confirmed; retain recovery state.",
                    process_group=self.process.pid,
                ) from None
            raise
        finally:
            bridge = getattr(self, "nntp_bridge", None)
            if bridge is not None:
                bridge.close()


class InfiniDyskV2Executor:
    supported_source_providers = ("sqlite", "postgres")

    def __init__(self, process_handler, config_manager):
        self.handler, self.config = process_handler, config_manager

    def _execution_ids(self):
        uid_value, gid_value = self.config.get("puid"), self.config.get("pgid")
        uid = int(1000 if uid_value is None else uid_value)
        gid = int(1000 if gid_value is None else gid_value)
        if not 0 < uid < 2**31 or not 0 < gid < 2**31:
            raise ContainedImportError(
                "Contained migration requires non-root PUID and PGID."
            )
        return uid, gid

    def preflight(self, plan):
        blockers = []
        try:
            uid, gid = self._execution_ids()
        except (TypeError, ValueError):
            blockers.append(
                "Contained migration requires configured non-root PUID and PGID."
            )
        if not blockers and plan.get("target", {}).get("mount_backend") == "fuse":
            valid, reason = validate_fuse_prerequisites(uid)
            if not valid:
                blockers.append(reason)
        if sys.platform != "linux" or os.geteuid() != 0 or not shutil.which("nsenter"):
            blockers.append(
                "Contained native import requires Linux controller namespaces and nsenter."
            )
        else:
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.syscall(444, None, 0, 1) < 3:
                blockers.append(
                    "Contained native import requires Landlock ABI 3 or newer."
                )
        if not blockers:
            try:
                self._retained_order(plan)
            except (ContainedImportError, OSError, ValueError) as error:
                blockers.append(
                    str(error)
                    if isinstance(error, ContainedImportError)
                    else "Managed startup dependencies could not be verified before migration."
                )
        if not blockers:
            try:
                probe = subprocess.run(
                    namespace_command(
                        uid,
                        gid,
                        [
                            sys.executable,
                            str(project_path("scripts", "infinidysk_v2_sandbox.py")),
                            "--check",
                            str(uid),
                            str(gid),
                        ],
                    ),
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                )
                proof = json.loads(probe.stdout)
                if (
                    proof.get("landlock_abi", 0) < 3
                    or proof.get("uid") != uid
                    or proof.get("gid") != gid
                    or proof.get("external_network") is not False
                    or proof.get("network_namespace")
                    == os.readlink("/proc/self/ns/net")
                ):
                    raise ValueError("Confinement proof did not match")
            except (OSError, ValueError, subprocess.SubprocessError):
                blockers.append(
                    "A real private user/network/mount namespace and loopback confinement probe failed; no source was stopped."
                )
        if (
            not blockers
            and plan.get("mode") == "migrate"
            and plan["source"].get("provider") == "sqlite"
        ):
            try:
                read_source_providers(
                    Path(plan["source"]["data_dir"]) / "db.sqlite",
                    plan["source"]["service"].get("env"),
                )
            except NntpBridgeError as error:
                blockers.append(str(error))
            except (OSError, sqlite3.Error):
                blockers.append(
                    "Captured provider configuration could not be read safely before stopping v1."
                )
        result = {"blockers": blockers}
        if plan["source"].get("provider") in {"postgres", "postgresql"}:
            try:
                postgres_tools()
                result["source_binding"] = inspect_postgres_source(plan)
            except (InfiniDyskV2PostgresSnapshotError, ValueError, OSError):
                blockers.append(
                    "The PostgreSQL source identity/TLS/schema or matching isolated restore tools could not be verified."
                )
        return result

    def _names(self, plan):
        result = [plan["source"]["process_name"]]
        result += [
            item["process_name"]
            for item in plan["inventory"]["mounts"]
            if item.get("process_name")
        ]
        result += [item["process_name"] for item in plan["inventory"]["consumers"]]
        return list(dict.fromkeys(result))

    def _running(self, name):
        process = self.handler.process_names.get(self.handler._prefixed_name(name))
        if process is not None and process.poll() is None:
            return True
        external = getattr(self.handler, "external_processes", {})
        return any(
            self._pid_alive(external.get(key))
            for key in {name, self.handler._prefixed_name(name)}
        )

    @staticmethod
    def _pid_alive(pid):
        if type(pid) is not int or pid <= 0:
            return False
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            return state not in {"Z", "X"}
        except FileNotFoundError:
            return False

    def _ordered_names(self, plan, names, *, service=None):
        """Order only the reviewed retained set; never start extra dependencies."""
        configuration = copy.deepcopy(getattr(self.config, "config", None))
        if not isinstance(configuration, dict):
            raise ContainedImportError(
                "The managed lifecycle configuration is unavailable."
            )
        if service is not None:
            configuration["infinidysk"] = copy.deepcopy(service)
        configured = {}
        for key, section in configuration.items():
            if not isinstance(section, dict):
                continue
            instances = section.get("instances")
            entries = instances.values() if isinstance(instances, dict) else [section]
            for entry in entries:
                if isinstance(entry, dict) and isinstance(
                    entry.get("process_name"), str
                ):
                    configured.setdefault(entry["process_name"], []).append(
                        (key, entry)
                    )
        rows = []
        for name in names:
            if not isinstance(name, str) or len(configured.get(name, [])) != 1:
                raise ContainedImportError(
                    "A retained migration process no longer has one configured identity."
                )
            key, entry = configured[name][0]
            # Infer dependencies for the retained running set in a planning copy.
            # Owned activation may have disabled a previous owner for rollback.
            entry["enabled"] = True
            rows.append({"key": key, "name": name})
        owners = {plan["source"]["process_name"]}
        owners.update(item.get("process_name") for item in plan["inventory"]["mounts"])
        rows.sort(key=lambda row: row["name"] not in owners)
        from utils.dependency_map import build_conditional_dependency_map

        dependencies = build_conditional_dependency_map(
            lambda key: configuration.get(key) or {}
        )
        try:
            ordered = [
                row["name"]
                for row in order_resume_rows(rows, configuration, dependencies)
            ]
        except BackupLifecycleError as error:
            raise ContainedImportError(str(error)) from None
        seen_consumer = False
        for name in ordered:
            if name in owners:
                if seen_consumer:
                    raise ContainedImportError(
                        "A delivery owner depends on a stopped consumer; review configured startup waits before migration."
                    )
            else:
                seen_consumer = True
        return ordered

    def _target_names(self, plan, names):
        mounts = {item.get("process_name") for item in plan["inventory"]["mounts"]}
        owners = [plan["source"]["process_name"]]
        instances = (self.config.get("rclone") or {}).get("instances") or {}
        for owner in selected_topology(plan["target"]["service"], instances):
            if owner["backend"] != "rclone":
                continue
            name = owner.get("process_name")
            if not name or name not in mounts:
                raise ContainedImportError(
                    "The selected delivery owner is outside the reviewed migration scope."
                )
            owners.append(name)
        return list(
            dict.fromkeys(
                owners
                + [name for name in names if name not in owners and name not in mounts]
            )
        )

    def _stop_retained(self, name):
        process = self.handler.process_names.get(self.handler._prefixed_name(name))
        external = getattr(self.handler, "external_processes", {})
        pids = {getattr(process, "pid", None)}
        pids.update(
            external.get(key) for key in {name, self.handler._prefixed_name(name)}
        )
        pids = {pid for pid in pids if type(pid) is int and pid > 0}
        result = self.handler.stop_process(name)
        if isinstance(result, tuple) and result[0] is False:
            raise ContainedImportError(
                "A retained managed process refused to stop; migration remains paused."
            )
        try:
            registered_running = self._running(name)
        except (OSError, ValueError, IndexError):
            raise MigrationProcessCleanupError(
                "Retained writer termination could not be verified; recovery remains held."
            ) from None
        if registered_running:
            raise ContainedImportError(
                "A planned process did not stop; further migration changes remain blocked."
            )
        for pid in pids:
            group_alive = getattr(self.handler, "_process_group_alive", lambda _: False)
            try:
                deadline = time.monotonic() + 5
                while group_alive(pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                group_live = group_alive(pid)
                pid_live = self._pid_alive(pid)
            except (OSError, ValueError, IndexError):
                raise MigrationProcessCleanupError(
                    "Retained writer termination could not be verified; recovery remains held."
                ) from None
            if pid_live or group_live:
                raise MigrationProcessCleanupError(
                    "A retained managed writer or child group remains live; recovery must confirm termination before restarting services.",
                    process_group=pid if group_live else None,
                )

    def _start_retained(self, name, message):
        if self._running(name):
            return
        success, _ = self.handler.start_process(name)
        if not success:
            raise ContainedImportError(message)

    def _assert_unmounted(self, path):
        target = Path(os.path.normpath(path))
        if any(
            Path(item["path"]) == target or target in Path(item["path"]).parents
            for item in mount_records()
        ):
            raise ContainedImportError(
                "A selected mount or child mount remains active; no forced detach was attempted."
            )

    def _wait_delivery(self, service, *, timeout=120):
        deadline = time.monotonic() + timeout
        instances = (self.config.get("rclone") or {}).get("instances") or {}
        while time.monotonic() < deadline:
            if delivery_mount_ready(service, instances):
                return
            if getattr(self.handler, "shutting_down", False):
                raise ContainedImportError(
                    "DUMB is shutting down before delivery became ready."
                )
            time.sleep(0.25)
        raise ContainedImportError(
            "The selected delivery mount did not become ready; consumers remain stopped."
        )

    def _retained_order(self, plan):
        was_running = self._ordered_names(
            plan,
            [name for name in self._names(plan) if self._running(name)],
            service=plan["source"]["service"],
        )
        # Validate the selected target graph before stopping any original service.
        self._ordered_names(
            plan,
            self._target_names(plan, was_running),
            service=plan["target"]["service"],
        )
        return was_running

    def _quiesce(self, plan, progress):
        was_running = self._retained_order(plan)
        progress(
            "quiescing",
            "Stopping the identified source, mount and managed consumers.",
            original_running_names=was_running,
        )
        for name in reversed(was_running):
            self._stop_retained(name)
        for mount in plan["inventory"]["mounts"]:
            self._assert_unmounted(mount["path"])
        return was_running

    def _restore_original(self, plan, names):
        names = self._ordered_names(plan, names, service=plan["source"]["service"])
        source = plan["source"]["process_name"]
        mounts = {item.get("process_name") for item in plan["inventory"]["mounts"]}
        owners = [name for name in names if name == source or name in mounts]
        consumers = [name for name in names if name not in owners]
        for name in owners:
            self._start_retained(
                name,
                "A retained managed owner did not restart; recovery evidence was preserved.",
            )
        if any(name in mounts for name in owners) or consumers:
            self._wait_delivery(plan["source"]["service"])
        for name in consumers:
            self._start_retained(
                name,
                "A retained consumer did not restart; recovery evidence was preserved.",
            )

    def _own_candidate(self, candidate, uid, gid):
        # Only the newly copied candidate tree is re-owned. Links are never
        # traversed and originals/rollback snapshots retain their owners.
        for directory, folders, files in os.walk(candidate, followlinks=False):
            for entry in [
                Path(directory),
                *(Path(directory, name) for name in folders + files),
            ]:
                if not entry.is_symlink():
                    os.chown(entry, uid, gid, follow_symlinks=False)

    def _discard_rehearsal_candidate(self, plan, workspace, snapshot):
        try:
            discard_rehearsal_candidate(
                plan, workspace, snapshot, self._assert_unmounted
            )
        except (ValueError, OSError, sqlite3.Error) as error:
            raise ContainedImportError(
                f"Disposable rehearsal cleanup stopped; retain the candidate for review: {error}"
            ) from error

    @staticmethod
    def _postgres_snapshot(plan, workspace, progress):
        # Keep trusted cancellation/progress failures recognizable across the
        # legacy three-argument restore callback and snapshot error redaction.
        callback_error = None
        cancel = workspace.get("check_cancel") or (lambda: None)

        def guarded(callback, *args, **kwargs):
            nonlocal callback_error
            try:
                return callback(*args, **kwargs)
            except Exception as error:
                callback_error = error
                raise

        def check_cancel():
            return guarded(cancel)

        def report(*args, **kwargs):
            return guarded(progress, *args, **kwargs)

        def verify_restore(dump, binding, settings):
            return verify_postgres_restore(
                dump, binding, settings, check_cancel=check_cancel, progress=report
            )

        try:
            return create_postgres_snapshot(
                plan,
                workspace,
                verify_restore=verify_restore,
                pg_dump=postgres_tools() / "pg_dump",
                progress=report,
                check_cancel=check_cancel,
            )
        except Exception as error:
            if getattr(error, "cleanup_complete", True) is False:
                raise
            if callback_error is not None:
                raise callback_error
            raise

    def _execute(self, plan, workspace, progress, *, cutover):
        uid, gid = self._execution_ids()
        was_running = self._quiesce(plan, progress)
        postgres_snapshot = None
        if plan["source"].get("provider") in {"postgres", "postgresql"}:
            postgres_snapshot = self._postgres_snapshot(plan, workspace, progress)
        snapshot = workspace["capture_source"](postgres_snapshot=postgres_snapshot)
        if (snapshot.get("source_schema") or {}).get("oidc_configured"):
            raise ContainedImportError(
                "The captured v1 OIDC configuration requires a separately verified authentication policy before import; v1 was retained."
            )
        candidate = Path(workspace["candidate_data"])
        private = Path(workspace["root"])
        if postgres_snapshot is not None:
            # Native source() prefers the retained contract when DATABASE_PROVIDER
            # is absent. Adapt ONLY the private candidate: the verified staged
            # database is SQLite, and no original PostgreSQL endpoint is needed.
            contract = candidate / "db-contract.json"
            value = json.loads(contract.read_text()) if contract.is_file() else {}
            value["provider"] = "sqlite"
            atomic_write_private_text(contract, json.dumps(value))
        self._own_candidate(candidate, uid, gid)
        service = copy.deepcopy(plan["target"]["service"])
        service.update(v2_data_dir=str(candidate), branch_enabled=False, commit_sha="")
        source_key = None
        if plan["mode"] == "migrate":
            source_key = source_sab_key(
                snapshot["source_database"], plan["source"]["service"].get("env") or {}
            )
            validate_target_sab_override(source_key, service.get("env") or {})
        success, _ = install_v2_runtime(
            self.handler, plan["source"]["process_name"], service
        )
        if not success:
            raise ContainedImportError(
                "The pinned native runtime could not be installed safely."
            )
        native_auth(service, create=True, uid=uid, gid=gid, allow_v1_import=True)
        progress(
            "rehearsing",
            "Running the isolated native importer. Only read-only requests to captured NNTP providers are permitted; original library writes and other network access remain denied.",
        )
        with NativeCandidate(
            service,
            candidate,
            private,
            uid,
            gid,
            plan["source"].get("timezone"),
            {
                **(plan["source"]["service"].get("env") or {}),
                "DATABASE_PROVIDER": "sqlite",
            },
            check_cancel=workspace.get("check_cancel"),
            record_report=workspace.get("record_import_report"),
        ) as process:
            child = getattr(process, "process", None)
            if type(getattr(child, "pid", None)) is int:
                progress(
                    "importing",
                    "The contained native importer is running.",
                    native_process_group=child.pid,
                )
            report_text = process.wait_import(plan["mode"] == "migrate", progress)
        credentials = {}
        if source_key is not None:
            credentials = verify_imported_sab_key(
                candidate / "infinidysk.sqlite", source_key, service.get("env") or {}
            )
        bridge = getattr(process, "nntp_bridge", None)
        if isinstance(bridge, NntpBridge):
            bridge.restore_native_providers(
                candidate / "infinidysk.sqlite",
                process.native_catalog,
                native_stopped=process.stopped,
            )
        progress(
            "validating",
            "Native import finished. Verifying the complete report, database inventory and retained source before acceptance.",
        )
        record_report = workspace.get("record_import_report", lambda *args: None)
        record_report(report_text)
        temporary = candidate / ".dumb-import-tmp"
        if temporary.exists():
            shutil.rmtree(temporary)
        if plan["mode"] == "migrate":
            blob_root = candidate / "blobs"
            if blob_root.is_symlink():
                from utils.infinidysk_v2_inventory import (
                    InventoryError,
                    validate_data_link,
                )

                try:
                    validate_data_link(
                        candidate, {"path": "blobs", "link": os.readlink(blob_root)}
                    )
                    blob_root = blob_root.resolve(strict=True)
                    blob_root.relative_to(candidate)
                except (OSError, ValueError, RuntimeError, InventoryError):
                    raise ContainedImportError(
                        "The staged legacy blob alias leaves its verified candidate scope."
                    ) from None
            try:
                report = verify_import_inventory(
                    snapshot["source_database"],
                    candidate / "infinidysk.sqlite",
                    report_text,
                    source_blob_root=blob_root,
                )
            except ContainedImportError as error:
                diagnostics = getattr(error, "import_report", None)
                if diagnostics is not None:
                    record_report(None, diagnostics)
                raise
            record_report(None, report.get("import_report"))
        else:
            with sqlite3.connect(
                f"file:{quote(str(candidate / 'infinidysk.sqlite'))}?mode=ro", uri=True
            ) as database:
                if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ContainedImportError(
                        "The fresh native database did not validate."
                    )
            report = {"skipped_files": 0, "failed_files": 0, "files": 0, "history": 0}
        receipt = {
            **credentials,
            "plan_fingerprint": plan["plan_fingerprint"],
            "execution_verified": True,
            "isolation_verified": True,
            "source_untouched": not plan.get("options", {}).get("reclaim_source_space"),
            "archive_recovery_verified": bool(
                plan.get("options", {}).get("reclaim_source_space")
            ),
            "import_complete": True,
            "inventory_verified": True,
            "oidc_policy_verified": True,
            "report": report,
            "native_report_private": report_text,
        }
        # The manager independently verifies original hashes and this receipt.
        workspace["accept_import"](receipt)
        if not cutover:
            if getattr(process, "stopped", False) is not True:
                raise ContainedImportError(
                    "Native candidate shutdown is unverified; its data was retained."
                )
            restore_source = workspace.get("restore_source")
            if callable(restore_source):
                restore_source(self._assert_unmounted)
            self._restore_original(plan, was_running)
            receipt["source_restored"] = True
            progress(
                "source_restored",
                "The original source and consumers were restored before disposable rehearsal cleanup.",
            )
            progress(
                "discarding_rehearsal",
                "Removing only the validated disposable rehearsal candidate; retained backups stay protected.",
            )
            if not plan.get("options", {}).get("reclaim_source_space"):
                self._discard_rehearsal_candidate(plan, workspace, snapshot)
            receipt["candidate_discarded"] = True
            return receipt
        target = Path(plan["target"]["data_dir"])
        if target.exists():
            if target.is_symlink() or any(target.iterdir()) or os.path.ismount(target):
                raise ContainedImportError(
                    "The native data destination changed; no existing data was replaced."
                )
            target.rmdir()
        from utils.infinidysk_v2_space_control import identity, rename_exclusive

        parent_identity = identity(candidate.parent)
        rename_exclusive(
            candidate,
            target,
            snapshot["candidate_identity"],
            parent_identity,
            parent_identity,
        )
        final = copy.deepcopy(plan["target"]["service"])
        final.update(branch_enabled=False, commit_sha="")
        success, reason = configure_v2_runtime(
            self.handler, plan["source"]["process_name"], final
        )
        if not success:
            raise ContainedImportError(
                "The accepted native runtime could not be prepared: "
                + safe_native_preparation_error(reason)
                + "."
            )
        workspace["activate_config"](final)
        workspace["expose_activation"](final, receipt)
        name = plan["source"]["process_name"]
        success, _ = self.handler.start_prepared_process(name)
        if not success:
            raise ContainedImportError(
                "The selected native runtime did not start; original v1 data is retained."
            )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                health = native_api_request(final, "GET", "/healthz")
                if health["status"] == 200:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(1)
        else:
            raise ContainedImportError(
                "Native application readiness did not stabilize."
            )
        instances = (self.config.get("rclone") or {}).get("instances") or {}
        for owner in selected_topology(final, instances):
            owner_name = owner.get("process_name")
            if (
                owner["backend"] == "rclone"
                and owner_name
                and not self._running(owner_name)
            ):
                success, _ = self.handler.start_prepared_process(owner_name)
                if not success:
                    raise ContainedImportError(
                        "The selected rclone owner did not start; consumers remain stopped."
                    )
        self._wait_delivery(final)
        workspace["commit_activation"](receipt)
        workspace["resume_consumers"](receipt)
        receipt["generation_2_started"] = True
        return receipt

    def rehearse(self, plan, workspace, progress):
        return self._execute(plan, workspace, progress, cutover=False)

    def cutover(self, plan, workspace, progress):
        return self._execute(plan, workspace, progress, cutover=True)

    def rollback(self, job, progress):
        plan = job["plan"]
        names = (job.get("execution") or {}).get("original_running_names")
        if not isinstance(names, list):
            names = (
                [plan["source"]["process_name"]]
                if plan["source"].get("process_identity")
                else []
            )
            names += [
                item["process_name"]
                for item in plan["inventory"]["mounts"]
                if item.get("mounted")
            ]
            names += [
                item["process_name"]
                for item in plan["inventory"]["consumers"]
                if item.get("process_identity")
            ]
        allowed = set(self._names(plan))
        if any(not isinstance(name, str) or name not in allowed for name in names):
            raise ContainedImportError(
                "The retained migration process scope changed; preserve recovery state before starting services."
            )
        names = self._ordered_names(
            plan, list(dict.fromkeys(names)), service=plan["source"]["service"]
        )
        progress(
            "rolling_back",
            "Stopping the selected native owner before restoring retained v1 configuration.",
        )
        live_names = self._ordered_names(
            plan, [name for name in self._names(plan) if self._running(name)]
        )
        for name in reversed(live_names):
            self._stop_retained(name)
        self._assert_unmounted(plan["target"]["mount_path"])
        restore = job.get("restore_config")
        if not callable(restore):
            raise ContainedImportError(
                "The controller rollback checkpoint is unavailable."
            )
        progress(
            "restoring_source", "Restoring the retained v1 configuration and data."
        )
        restore()
        restore_source = job.get("restore_source")
        if callable(restore_source):
            restore_source(self._assert_unmounted)
        self._restore_original(plan, names)
        return {"generation_2_stopped": True, "source_restored": True}
