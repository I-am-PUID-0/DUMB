"""Verified PostgreSQL restore callback for a major-version source snapshot."""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

from utils.infinidysk_v2_subprocess import (
    MigrationProcessCleanupError,
    run_migration_process,
)
from utils.private_files import atomic_write_private_text
from utils.runtime_paths import project_path
from scripts.infinidysk_v2_sandbox import namespace_command


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
    tools = postgres_tools()
    # The child owns every potentially slow copy/restore/verification operation.
    # Keep its scratch if cleanup cannot prove that all owned children stopped.
    root = Path(tempfile.mkdtemp(prefix="dumb-v2-pg-restore-"))
    cleanup_complete = True
    report = progress or (lambda *args, **kwargs: None)

    def status(state):
        elapsed = int(state.get("elapsed_seconds", 0))
        message = (
            "Restoring and validating the independent PostgreSQL backup "
            f"({elapsed:,} seconds elapsed)."
        )
        if state.get("stall_warning"):
            message += (
                " No observable progress has been reported recently; the operation "
                "is still running and can be cancelled."
            )
        report("postgres_restore_verification", message)

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
    finally:
        if cleanup_complete:
            shutil.rmtree(root)
