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
from utils.infinidysk_v2_pg_snapshot import (
    InfiniDyskV2PostgresSnapshotError,
    create_postgres_snapshot,
    inspect_postgres_source,
)
from utils.infinidysk_v2_restore import postgres_tools, verify_postgres_restore
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


def verify_import_inventory(source, destination, report):
    """Compare preserved public file identities and history, not just health."""
    match = _SUMMARY.search(report)
    if not match or "Files not carried over:" in report:
        raise ContainedImportError(
            "The native report is incomplete or contains skipped media; v1 was retained."
        )
    with sqlite3.connect(f"file:{quote(str(source))}?mode=ro", uri=True) as old:
        files = old.execute(
            "SELECT Id, Name FROM DavItems WHERE Type=2 AND Path LIKE '/content/%'"
        ).fetchall()
        history = old.execute("SELECT Id FROM HistoryItems").fetchall()
    with sqlite3.connect(f"file:{quote(str(destination))}?mode=ro", uri=True) as new:
        if new.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ContainedImportError("Native database integrity did not validate.")
        items = {
            _uuid(row[0]): row[1] for row in new.execute("SELECT id,name FROM items")
        }
        actual_history = {
            _uuid(row[0]) for row in new.execute("SELECT id FROM history")
        }
    if (
        int(match[2]) != len(files)
        or int(match[4]) != len(history)
        or int(match[5]) != 0
    ):
        raise ContainedImportError(
            "The native import totals differ from the retained source."
        )
    if any(items.get(_uuid(identifier)) != name for identifier, name in files):
        raise ContainedImportError(
            "Existing media identities or names were not preserved."
        )
    if any(_uuid(row[0]) not in actual_history for row in history):
        raise ContainedImportError("Existing history identities were not preserved.")
    return {
        "files": len(files),
        "history": len(history),
        "skipped_files": 0,
        "failed_files": 0,
        "native_report_sha256": hashlib.sha256(report.encode()).hexdigest(),
    }


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
        timeout=300,
    ):
        self.service, self.candidate, self.private = (
            service,
            Path(candidate),
            Path(private),
        )
        self.uid, self.gid, self.timeout = uid, gid, timeout
        self.process = None
        self.stopped = False
        self.output = None
        self.port = 17831  # A fresh private network namespace has no other listener.
        self.payload_path = self.private / "candidate-payload.json"
        self.temporary = self.candidate / ".dumb-import-tmp"
        self.temporary.mkdir(mode=0o700)
        os.chown(self.temporary, uid, gid)
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
        atomic_write_private_text(self.payload_path, json.dumps(self.payload))
        path = self.private / "candidate.log"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.output = os.fdopen(descriptor, "wb")
        try:
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
            self.output.close()
            self.payload_path.unlink(missing_ok=True)
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
        deadline = time.monotonic() + self.timeout
        last = None
        while time.monotonic() < deadline:
            reply = self.probe("/api/v1/upgrade/status")
            if reply and reply["status"] == 200:
                state = reply["body"]
                if state.get("state") == "failed":
                    raise ContainedImportError(
                        "The isolated native import failed; provider access may be required. No original data was changed."
                    )
                stage = (state.get("progress") or {}).get("stage")
                if stage != last:
                    progress(
                        "importing",
                        f"Contained native stage: {stage or state.get('state', 'starting')}",
                    )
                    last = stage
                if state.get("state") == ("done" if migrate else "none"):
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
                        return report["body"]["report"]
                    return ""
            time.sleep(0.25)
        raise ContainedImportError(
            "The contained import did not finish within its bound; original v1 data was retained."
        )

    def __exit__(self, *unused):
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
                    raise ContainedImportError(
                        "The native candidate still has live writers; its data was retained."
                    )
                time.sleep(0.05)
            self.stopped = self.process.poll() is not None


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
        return process is not None and process.poll() is None

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

    def _quiesce(self, plan, progress):
        was_running = [name for name in self._names(plan) if self._running(name)]
        progress(
            "quiescing",
            "Stopping the identified source, mount and managed consumers.",
            original_running_names=was_running,
        )
        for name in reversed(was_running):
            self.handler.stop_process(name)
            if self._running(name):
                raise ContainedImportError(
                    "A planned process did not stop; no native import was started."
                )
        for mount in plan["inventory"]["mounts"]:
            self._assert_unmounted(mount["path"])
        return was_running

    def _restore_original(self, plan, names):
        source = plan["source"]["process_name"]
        mounts = {item.get("process_name") for item in plan["inventory"]["mounts"]}
        owners = [name for name in names if name == source or name in mounts]
        consumers = [name for name in names if name not in owners]
        for name in owners:
            success, _ = self.handler.start_process(name)
            if not success:
                raise ContainedImportError(
                    "A retained managed owner did not restart; recovery evidence was preserved."
                )
        if any(name in mounts for name in owners) or consumers:
            self._wait_delivery(plan["source"]["service"])
        for name in consumers:
            success, _ = self.handler.start_process(name)
            if not success:
                raise ContainedImportError(
                    "A retained consumer did not restart; recovery evidence was preserved."
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

    def _execute(self, plan, workspace, progress, *, cutover):
        uid, gid = self._execution_ids()
        was_running = self._quiesce(plan, progress)
        postgres_snapshot = None
        if plan["source"].get("provider") in {"postgres", "postgresql"}:
            postgres_snapshot = create_postgres_snapshot(
                plan,
                workspace,
                verify_restore=verify_postgres_restore,
                pg_dump=postgres_tools() / "pg_dump",
                progress=progress,
            )
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
            "Running the native importer with external network and original writes denied.",
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
        ) as process:
            report_text = process.wait_import(plan["mode"] == "migrate", progress)
        temporary = candidate / ".dumb-import-tmp"
        if temporary.exists():
            shutil.rmtree(temporary)
        if plan["mode"] == "migrate":
            report = verify_import_inventory(
                snapshot["source_database"],
                candidate / "infinidysk.sqlite",
                report_text,
            )
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
            "plan_fingerprint": plan["plan_fingerprint"],
            "execution_verified": True,
            "isolation_verified": True,
            "source_untouched": True,
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
        os.rename(candidate, target)
        final = copy.deepcopy(plan["target"]["service"])
        final.update(branch_enabled=False, commit_sha="")
        success, _ = configure_v2_runtime(
            self.handler, plan["source"]["process_name"], final
        )
        if not success:
            raise ContainedImportError(
                "The accepted native runtime could not be prepared for its selected mount."
            )
        workspace["activate_config"](final)
        name = plan["source"]["process_name"]
        success, _ = self.handler.start_process(name)
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
                success, _ = self.handler.start_process(owner_name)
                if not success:
                    raise ContainedImportError(
                        "The selected rclone owner did not start; consumers remain stopped."
                    )
        self._wait_delivery(final)
        # Re-enable only prior consumers after the selected owner is mounted.
        mount_names = {item.get("process_name") for item in plan["inventory"]["mounts"]}
        for previous in was_running:
            if previous == name or previous in mount_names:
                continue
            success, _ = self.handler.start_process(previous)
            if not success:
                raise ContainedImportError(
                    "A managed consumer did not recover after native activation."
                )
        receipt["generation_2_started"] = True
        return receipt

    def rehearse(self, plan, workspace, progress):
        return self._execute(plan, workspace, progress, cutover=False)

    def cutover(self, plan, workspace, progress):
        return self._execute(plan, workspace, progress, cutover=True)

    def rollback(self, job, progress):
        plan = job["plan"]
        progress(
            "rolling_back",
            "Stopping the selected native owner before restoring retained v1 configuration.",
        )
        for name in reversed(self._names(plan)):
            if self._running(name):
                self.handler.stop_process(name)
                if self._running(name):
                    raise ContainedImportError(
                        "A native consumer did not stop; original configuration was not restored."
                    )
        self._assert_unmounted(plan["target"]["mount_path"])
        restore = job.get("restore_config")
        if not callable(restore):
            raise ContainedImportError(
                "The controller rollback checkpoint is unavailable."
            )
        restore()
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
        self._restore_original(
            plan, [name for name in dict.fromkeys(names) if name in allowed]
        )
        return {"generation_2_stopped": True, "source_restored": True}
