"""Restore a private logical backup in a disposable, network-isolated cluster."""

import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2

from scripts.infinidysk_v2_sandbox import confine
from utils.infinidysk_v2_pg_snapshot import verify_restored_connection
from utils.private_files import atomic_write_private_text


def _dump_identity(value):
    return [
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    ]


def _copy_dump(source, destination, expected, progress):
    """Copy in the owned child; a blocked source read remains cancellable."""
    descriptor = os.open(
        source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    )
    with os.fdopen(descriptor, "rb") as incoming:
        before = os.fstat(incoming.fileno())
        if not stat.S_ISREG(before.st_mode) or _dump_identity(before) != expected:
            raise ValueError("The retained PostgreSQL dump changed before copying.")
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as outgoing:
            copied = 0
            progress(copied)
            published = time.monotonic()
            while chunk := incoming.read(1024 * 1024):
                outgoing.write(chunk)
                copied += len(chunk)
                now = time.monotonic()
                if now - published >= 1:
                    progress(copied)
                    published = now
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if _dump_identity(os.fstat(incoming.fileno())) != expected:
            raise ValueError("The retained PostgreSQL dump changed while copying.")
        progress(copied)


def main():
    payload = json.loads(Path(sys.argv[1]).read_text())
    root = Path(payload["root"])
    tools = Path(payload["tools"])
    cluster, sockets = root / "cluster", root / "sockets"
    server = None
    log = None

    def interrupted(*unused):
        raise InterruptedError("Restore verification interrupted")

    def activity(stage, copied=None):
        state = {"stage": stage}
        if copied is not None:
            state["copied_bytes"] = copied
        atomic_write_private_text(
            root / "copy-progress.json",
            json.dumps(state),
            owner=(payload["uid"], payload["gid"]),
        )

    signal.signal(signal.SIGTERM, interrupted)
    try:
        # Source reads remain allowed, while every write is already confined
        # to this disposable root before copying potentially slow storage.
        confine([str(root)])
        _copy_dump(
            payload["dump_path"],
            root / "backup.dump",
            payload["dump_identity"],
            lambda copied: activity("copying_dump", copied),
        )
        sockets.mkdir()
        for path in (root, sockets, root / "backup.dump"):
            os.chown(path, payload["uid"], payload["gid"])
        os.setgroups([])
        os.setgid(payload["gid"])
        os.setuid(payload["uid"])
        log = (root / "restore.log").open("wb")
        activity("initializing_database")
        # Bulk operations have no elapsed deadline. The controller supervises
        # this session, reports quiet periods and can cancel its entire group.
        subprocess.run(
            [
                str(tools / "initdb"),
                "-D",
                str(cluster),
                "--auth=trust",
                "--no-locale",
                "--encoding=UTF8",
                "--username=dumb_verifier",
            ],
            check=True,
            stdout=log,
            stderr=log,
        )
        server = subprocess.Popen(
            [
                str(tools / "postgres"),
                "-D",
                str(cluster),
                "-h",
                "",
                "-k",
                str(sockets),
                "-p",
                "5432",
                "-c",
                "dynamic_shared_memory_type=mmap",
                "-c",
                "max_parallel_workers=0",
            ],
            stdout=log,
            stderr=log,
        )
        connection = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                connection = psycopg2.connect(
                    host=str(sockets),
                    port=5432,
                    user="dumb_verifier",
                    dbname="postgres",
                    connect_timeout=1,
                )
                break
            except psycopg2.Error:
                if server.poll() is not None:
                    raise RuntimeError("Private PostgreSQL verifier exited") from None
                time.sleep(0.1)
        if connection is None:
            raise RuntimeError("Private PostgreSQL verifier did not become ready")
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE DATABASE verification TEMPLATE template0 LC_COLLATE 'C' LC_CTYPE 'C'"
            )
        connection.close()
        activity("restoring_database")
        # Only pg_restore progress is observed, not the server's background log.
        with (root / "restore-progress.log").open("wb") as restore_progress:
            subprocess.run(
                [
                    str(tools / "pg_restore"),
                    "--verbose",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                    "-h",
                    str(sockets),
                    "-p",
                    "5432",
                    "-U",
                    "dumb_verifier",
                    "-d",
                    "verification",
                    str(root / "backup.dump"),
                ],
                check=True,
                stdout=restore_progress,
                stderr=restore_progress,
            )
        activity("verifying_database")
        connection = psycopg2.connect(
            host=str(sockets),
            port=5432,
            user="dumb_verifier",
            dbname="verification",
            connect_timeout=5,
        )
        try:
            result = verify_restored_connection(connection, payload["source_binding"])
        finally:
            connection.close()
        if result.get("verified") is not True:
            raise RuntimeError(
                "Restored PostgreSQL data did not match its source snapshot"
            )
        activity("verified")
        print(json.dumps(result))
    finally:
        try:
            if server is not None:
                try:
                    subprocess.run(
                        [
                            str(tools / "pg_ctl"),
                            "-D",
                            str(cluster),
                            "-m",
                            "immediate",
                            "-w",
                            "-t",
                            "10",
                            "stop",
                        ],
                        stdout=log,
                        stderr=log,
                        timeout=15,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
        finally:
            if log is not None:
                log.close()


if __name__ == "__main__":
    main()
