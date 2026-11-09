"""Restore a private logical backup in a disposable, network-isolated cluster."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2

from scripts.infinidysk_v2_sandbox import confine
from utils.infinidysk_v2_pg_snapshot import verify_restored_connection


def main():
    payload = json.loads(Path(sys.argv[1]).read_text())
    root = Path(payload["root"])
    tools = Path(payload["tools"])
    cluster, sockets = root / "cluster", root / "sockets"
    sockets.mkdir()
    for path in (root, sockets, root / "backup.dump"):
        os.chown(path, payload["uid"], payload["gid"])
    confine([str(root)])
    os.setgroups([])
    os.setgid(payload["gid"])
    os.setuid(payload["uid"])
    log = (root / "restore.log").open("wb")
    server = None

    def interrupted(*unused):
        raise InterruptedError("Restore verification interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    try:
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
            timeout=45,
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
        subprocess.run(
            [
                str(tools / "pg_restore"),
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "-h",
                str(sockets),
                "-U",
                "dumb_verifier",
                "-d",
                "verification",
                str(root / "backup.dump"),
            ],
            check=True,
            stdout=log,
            stderr=log,
            timeout=180,
        )
        connection = psycopg2.connect(
            host=str(sockets),
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
            log.close()


if __name__ == "__main__":
    main()
