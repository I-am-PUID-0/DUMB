"""Verified PostgreSQL restore callback for a major-version source snapshot."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

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


def verify_postgres_restore(dump_path, source_binding, connection_settings):
    del connection_settings  # No source connection/credential is used by restore.
    if os.geteuid() != 0 or sys.platform != "linux":
        raise ValueError(
            "A contained PostgreSQL restore requires Linux controller privileges."
        )
    source = Path(dump_path)
    if source.is_symlink() or not source.is_file():
        raise ValueError("The retained PostgreSQL dump is not a direct file.")
    tools = postgres_tools()
    with tempfile.TemporaryDirectory(prefix="dumb-v2-pg-restore-") as scratch:
        root = Path(scratch)
        shutil.copyfile(source, root / "backup.dump")
        payload = {
            "root": str(root),
            "tools": str(tools),
            "uid": 1000,
            "gid": 1000,
            "source_binding": source_binding,
        }
        path = root / "payload.json"
        atomic_write_private_text(path, json.dumps(payload))
        process = subprocess.Popen(
            namespace_command(
                1000,
                1000,
                [
                    sys.executable,
                    str(project_path("scripts", "infinidysk_v2_pg_restore.py")),
                    str(path),
                ],
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            output, _ = process.communicate(timeout=300)
            if process.returncode:
                raise ValueError(
                    "The isolated PostgreSQL restore did not verify; no source database was changed."
                )
            result = json.loads(output)
            if (
                result.get("verified") is not True
                or result.get("source_binding")
                != source_binding.get("binding_fingerprint")
                or not result.get("restored_identity")
                or result.get("restored_identity")
                == source_binding.get("physical_identity")
            ):
                raise ValueError(
                    "The PostgreSQL restore supplied no verified snapshot binding."
                )
            return result
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
            raise ValueError(
                "The isolated PostgreSQL restore exceeded its bound."
            ) from None
        finally:
            # A helper failure must not strand a private PostgreSQL descendant.
            # The process owns a new session; this never targets managed services.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
