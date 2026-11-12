"""Cancellable, bounded-output supervision for owned migration helpers.

Bulk work has no total-duration or automatic inactivity deadline. A quiet helper
can still be doing database or filesystem work; inactivity is an operator warning.
"""

import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time


class MigrationProcessError(RuntimeError):
    """A private helper failed without exposing command output or credentials."""


class MigrationProcessCleanupError(MigrationProcessError):
    """An owned process still needs recovery before admission can be released."""

    cleanup_complete = False

    def __init__(self, message, *, process_group=None):
        super().__init__(message)
        self.cleanup_evidence = {"process_group": process_group}


def _live_group(group):
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            # comm can contain spaces and parentheses; the fields follow its
            # final closing parenthesis. State, ppid and pgrp start the tail.
            fields = (entry / "stat").read_bytes().rsplit(b")", 1)[1].split()
            if int(fields[2]) == group and fields[0] not in {b"Z", b"X"}:
                members.append(int(entry.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (ValueError, IndexError):
            raise OSError(
                "Private process membership could not be inspected."
            ) from None
    return members


def migration_process_cleanup_complete(evidence):
    """Only observe a retained group; never kill a potentially reused PID."""
    if not isinstance(evidence, dict):
        return False
    group = evidence.get("process_group")
    if type(group) is not int or not 1 < group < 2**31:
        return False
    try:
        return not _live_group(group)
    except OSError:
        return False


def _signal_group(process, number):
    try:
        os.killpg(process.pid, number)
    except ProcessLookupError:
        pass


def _stop_owned_group(process):
    try:
        _reap_owned_group(process)
    except (OSError, subprocess.SubprocessError) as error:
        raise MigrationProcessCleanupError(
            "The private migration worker could not be confirmed stopped; recovery remains required.",
            process_group=process.pid,
        ) from error


def _reap_owned_group(process):
    # Every caller starts a fresh session. Never signal a shared service group.
    _signal_group(process, signal.SIGTERM)
    deadline = time.monotonic() + 20
    while _live_group(process.pid) and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.05)
    if _live_group(process.pid):
        _signal_group(process, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while _live_group(process.pid) and time.monotonic() < deadline:
            process.poll()
            time.sleep(0.05)
    if _live_group(process.pid):
        raise MigrationProcessCleanupError(
            "A private migration helper has not stopped; retain its recovery "
            "state and resolve the owned worker before starting another operation.",
            process_group=process.pid,
        )
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        raise MigrationProcessCleanupError(
            "A private migration helper could not be reaped; recovery remains required.",
            process_group=process.pid,
        ) from None


def _activity(paths):
    observed = []
    for path in paths:
        try:
            value = Path(path).lstat()
            if stat.S_ISREG(value.st_mode):
                observed.append(
                    (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
                )
            else:
                observed.append(None)
        except OSError:
            observed.append(None)
    return tuple(observed)


def run_migration_process(
    command,
    *,
    env=None,
    cwd=None,
    check_cancel=None,
    progress=None,
    activity_paths=(),
    idle_warning_seconds=1800,
    poll_interval=0.25,
    stdout_limit=1024 * 1024,
    stderr_limit=64 * 1024,
    popen=None,
    clock=None,
):
    """Run in an owned group; report quiet work, never infer failure from time."""
    if idle_warning_seconds <= 0 or not 0 < poll_interval <= 5:
        raise ValueError("Invalid migration progress interval.")
    if not 0 < stdout_limit <= 4 * 1024**2 or not 0 < stderr_limit <= 1024**2:
        raise ValueError("Invalid migration output budget.")
    check_cancel = check_cancel or (lambda: None)
    report = progress or (lambda value: None)
    now = clock or time.monotonic
    check_cancel()
    started = last_activity = now()
    previous_activity = _activity(activity_paths)
    last_report = float("-inf")
    last_warning = None
    output, errors = bytearray(), bytearray()
    process = None
    cleanup_attempted = False
    with selectors.DefaultSelector() as selector:
        try:
            process = (popen or subprocess.Popen)(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=cwd,
                start_new_session=True,
            )
            for stream, name in (
                (process.stdout, "stdout"),
                (process.stderr, "stderr"),
            ):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while True:
                check_cancel()
                current = now()
                measured = _activity(activity_paths)
                if measured != previous_activity:
                    previous_activity = measured
                    last_activity = current
                warning = current - last_activity >= idle_warning_seconds
                if current - last_report >= 2 or warning != last_warning:
                    report(
                        {
                            "elapsed_seconds": max(0, int(current - started)),
                            "idle_seconds": max(0, int(current - last_activity)),
                            "stall_warning": warning,
                        }
                    )
                    last_report, last_warning = current, warning
                events = selector.select(poll_interval)
                for key, _ in events:
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stdout":
                        if len(output) + len(chunk) > stdout_limit:
                            raise MigrationProcessError(
                                "A private migration helper exceeded its output budget."
                            )
                        output.extend(chunk)
                    else:
                        errors.extend(chunk)
                        if len(errors) > stderr_limit:
                            del errors[:-stderr_limit]
                returncode = process.poll()
                if returncode is not None:
                    cleanup_attempted = True
                    _stop_owned_group(process)
                    # Read everything already buffered by the exited helper.
                    # Descendants cannot keep these pipes open indefinitely.
                    for key in list(selector.get_map().values()):
                        while True:
                            try:
                                chunk = os.read(key.fd, 65536)
                            except BlockingIOError:
                                break
                            if not chunk:
                                break
                            if key.data == "stdout":
                                if len(output) + len(chunk) > stdout_limit:
                                    raise MigrationProcessError(
                                        "A private migration helper exceeded its output budget."
                                    )
                                output.extend(chunk)
                            else:
                                errors.extend(chunk)
                                if len(errors) > stderr_limit:
                                    del errors[:-stderr_limit]
                    if returncode:
                        raise MigrationProcessError(
                            "A private migration helper exited unsuccessfully."
                        )
                    return subprocess.CompletedProcess(
                        command, returncode, bytes(output), bytes(errors)
                    )
        finally:
            if process is not None:
                try:
                    if not cleanup_attempted:
                        _stop_owned_group(process)
                finally:
                    for stream in (process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()
