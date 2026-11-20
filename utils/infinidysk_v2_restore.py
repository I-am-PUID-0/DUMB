"""Verified PostgreSQL restore callback for a major-version source snapshot."""

import errno
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

from utils.infinidysk_v2_subprocess import (
    MigrationProcessCleanupError,
    MigrationProcessError,
    run_migration_process,
)
from utils.private_files import atomic_write_private_text
from utils.infinidysk_v2_pg_snapshot import InfiniDyskV2PostgresSnapshotError
from utils.runtime_paths import project_path
from scripts.infinidysk_v2_sandbox import namespace_command

RESTORE_STAGES = {
    "copying_dump": "copying the retained PostgreSQL backup into isolated storage",
    "initializing_database": "initializing the isolated PostgreSQL cluster",
    "starting_database": "starting the isolated PostgreSQL cluster",
    "creating_database": "creating the verification database with the source encoding and locale",
    "restoring_database": "restoring the retained PostgreSQL backup",
    "verifying_database": "comparing the restored database with the source snapshot",
    "verified": "completing the independent PostgreSQL verification",
}
RESTORE_FAILURE_CODES = {"storage", "permissions", "locale", "mismatch", "helper"}


def database_characteristics(binding):
    values = binding.get("database_characteristics")
    if (
        not isinstance(values, (list, tuple))
        or len(values) != 3
        or any(
            not isinstance(value, str) or not value or len(value) > 256
            for value in values
        )
    ):
        raise InfiniDyskV2PostgresSnapshotError(
            "The PostgreSQL snapshot has no valid source encoding and locale. Generate a fresh preview. "
            "DUMB did not modify the source database."
        )
    return tuple(values)


def restore_failure_code(error):
    # Only fixed codes cross the helper boundary, never stderr, SQL or data.
    if (
        getattr(error, "errno", None) in {errno.ENOSPC, errno.EDQUOT}
        or getattr(error, "pgcode", None) == "53100"
    ):
        return "storage"
    if getattr(error, "errno", None) in {errno.EACCES, errno.EPERM, errno.EROFS}:
        return "permissions"
    if getattr(error, "pgcode", None) in {"22023", "0A000"}:
        return "locale"
    if isinstance(error, InfiniDyskV2PostgresSnapshotError):
        return "mismatch"
    return "helper"


def _restore_status(root):
    try:
        descriptor = os.open(
            root / "copy-progress.json",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        )
        with os.fdopen(descriptor, "rb") as handle:
            value = os.fstat(handle.fileno())
            if not stat.S_ISREG(value.st_mode) or value.st_size > 4096:
                return {}
            state = json.loads(handle.read(4097))
        if not isinstance(state, dict) or state.get("stage") not in RESTORE_STAGES:
            return {}
        return {
            "stage": state["stage"],
            "failed": state.get("failed") is True,
            "failure_code": (
                state.get("failure_code")
                if state.get("failure_code") in RESTORE_FAILURE_CODES
                else "helper"
            ),
        }
    except (OSError, ValueError, TypeError):
        return {}


def _restore_failure(root, error):
    state = _restore_status(root)
    stage = state.get("stage")
    description = RESTORE_STAGES.get(
        stage, "entering the isolated PostgreSQL restore worker"
    )
    code = state.get("failure_code", "helper") if state.get("failed") else "helper"
    advice = {
        "storage": "Local verification storage has no available space or exceeded its quota.",
        "permissions": "Check controller access to the retained backup and private verification storage.",
        "locale": "The source database encoding or locale could not be created by the installed PostgreSQL tools.",
        "mismatch": "The independently restored database did not match the source snapshot; acceptance remains blocked.",
        "helper": "Check the installed PostgreSQL tools and local verification storage; keep the retained migration files.",
    }[code]
    logging.getLogger(__name__).error(
        "PostgreSQL restore verification failed: stage=%s code=%s exception=%s",
        stage,
        code,
        type(error).__name__,
    )
    failure = InfiniDyskV2PostgresSnapshotError(
        f"PostgreSQL migration stopped while {description}. {advice} DUMB did not modify the source database."
    )
    if code == "storage":
        # The helper exports a fixed code rather than raw driver diagnostics.
        # A new isolated restore can reuse the retained dump after freeing space.
        failure.retry_errno = errno.ENOSPC
    return failure


def postgres_tools():
    found = shutil.which("pg_config")
    if found:
        result = subprocess.run(
            [found, "--bindir"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            path = Path(result.stdout.strip())
            if all(
                (path / name).is_file()
                for name in ("initdb", "postgres", "pg_restore", "pg_ctl", "pg_dump")
            ):
                return path
    for path in sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True):
        if all(
            (path / name).is_file()
            for name in ("initdb", "postgres", "pg_restore", "pg_ctl", "pg_dump")
        ):
            return path
    raise ValueError(
        "Complete PostgreSQL client/server tools are required for isolated backup restoration."
    )


def _dump_identity(value):
    return [
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    ]


def verify_postgres_restore(
    dump_path,
    source_binding,
    connection_settings,
    *,
    check_cancel=None,
    progress=None,
    idle_warning_seconds=30 * 60,
):
    del connection_settings  # No source connection/credential is used by restore.
    if os.geteuid() != 0 or sys.platform != "linux":
        raise ValueError(
            "A contained PostgreSQL restore requires Linux controller privileges."
        )
    source = Path(dump_path)
    try:
        value = source.lstat()
    except OSError:
        raise ValueError("The retained PostgreSQL dump is not a direct file.") from None
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("The retained PostgreSQL dump is not a direct file.")
    database_characteristics(source_binding)
    tools = postgres_tools()
    # The child owns every potentially slow copy/restore/verification operation.
    # Keep its scratch if cleanup cannot prove that all owned children stopped.
    root = Path(tempfile.mkdtemp(prefix="dumb-v2-pg-restore-"))
    cleanup_complete = True
    report = progress or (lambda *args, **kwargs: None)

    def status(state):
        elapsed = int(state.get("elapsed_seconds", 0))
        restore_stage = _restore_status(root).get("stage")
        description = RESTORE_STAGES.get(
            restore_stage, "Restoring and validating the independent PostgreSQL backup"
        )
        message = (
            description[:1].upper() + description[1:] + " "
            f"({elapsed:,} seconds elapsed)."
        )
        if state.get("stall_warning"):
            message += (
                " No observable progress has been reported recently; the operation "
                "is still running and can be cancelled."
            )
        report(
            "postgres_restore_verification",
            message,
            restore_phase=restore_stage,
            elapsed_seconds=elapsed,
            idle_seconds=int(state.get("idle_seconds", 0)),
            stall_warning=state.get("stall_warning") is True,
        )

    try:
        payload = {
            "root": str(root),
            "tools": str(tools),
            "uid": 1000,
            "gid": 1000,
            "source_binding": source_binding,
            "dump_path": str(source.absolute()),
            "dump_identity": _dump_identity(value),
        }
        path = root / "payload.json"
        atomic_write_private_text(path, json.dumps(payload))
        # This private cluster must not inherit a source PGPORT, service,
        # password file or PGOPTIONS deadline from the controller environment.
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": str(root),
            "TMPDIR": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        completed = run_migration_process(
            namespace_command(
                1000,
                1000,
                [
                    sys.executable,
                    str(project_path("scripts", "infinidysk_v2_pg_restore.py")),
                    str(path),
                ],
            ),
            env=environment,
            check_cancel=check_cancel,
            progress=status,
            activity_paths=(root / "copy-progress.json", root / "restore-progress.log"),
            idle_warning_seconds=idle_warning_seconds,
        )
        result = json.loads(completed.stdout)
        if (
            not isinstance(result, dict)
            or result.get("verified") is not True
            or result.get("source_binding") != source_binding.get("binding_fingerprint")
            or not result.get("restored_identity")
            or result.get("restored_identity")
            == source_binding.get("physical_identity")
        ):
            raise ValueError(
                "The PostgreSQL restore supplied no verified snapshot binding."
            )
        return result
    except MigrationProcessCleanupError as error:
        cleanup_complete = False
        try:
            value = root.lstat()
            identity = [value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode)]
        except OSError:
            identity = None
        error.cleanup_evidence["restore_scratch"] = {
            "path": str(root),
            "identity": identity,
        }
        raise
    except MigrationProcessError as error:
        raise _restore_failure(root, error) from None
    finally:
        if cleanup_complete:
            shutil.rmtree(root)
