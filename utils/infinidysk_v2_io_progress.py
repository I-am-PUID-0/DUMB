"""Measured current-phase I/O progress, independent from integrity decisions."""

from collections import deque
from inspect import signature
import math
import re
import secrets
import time

PHASES = {
    "source_fingerprint": ("snapshotting", "bytes"),
    "archive_inventory": ("archive_inventory", "entries"),
    "archive_write": ("archiving", "bytes"),
    "archive_readback": ("verifying_archive", "bytes"),
    "archive_recheck": ("verifying_archive", "bytes"),
    "archive_restore": ("restoring_archive", "bytes"),
    "restore_metadata": ("restoring_metadata", "entries"),
    "restore_verify": ("verifying_restored_source", "bytes"),
    "blob_copy": ("staging_blobs", "bytes"),
    "blob_metadata": ("preparing_blob_staging", "entries"),
    "blob_verify_staged": ("verifying_blob_staging", "bytes"),
    "blob_verify_source": ("verifying_blob_staging", "entries"),
    "blob_release": ("releasing_local_blobs", "entries"),
    "blob_verify_input": ("verifying_blob_input", "bytes"),
    "blob_cleanup": ("cleaning_blob_staging", "entries"),
}
MAX_AGE = 30


def _count(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def public_io_progress(value, *, active, stage, now=None):
    """Expose only bounded counters; observing status never refreshes progress."""
    if not isinstance(value, dict):
        return None
    phase, identifier = value.get("phase"), value.get("phase_id")
    if (
        not isinstance(phase, str)
        or phase not in PHASES
        or not isinstance(identifier, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier)
        or value.get("unit") != PHASES[phase][1]
        or any(
            not _count(value.get(key))
            for key in ("processed", "elapsed_seconds", "updated_at")
        )
        or type(value.get("complete")) is not bool
        or type(value.get("total_is_estimate")) is not bool
    ):
        return None
    total = value.get("total")
    if total is not None and not _count(total):
        return None
    result = {
        key: value[key]
        for key in (
            "phase_id",
            "phase",
            "unit",
            "processed",
            "total_is_estimate",
            "elapsed_seconds",
            "updated_at",
            "complete",
        )
    }
    result.update(total=total, rate_per_second=None, estimated_remaining_seconds=None)
    clock = time.time() if now is None else now
    live = (
        active
        and stage == PHASES[phase][0]
        and not value["complete"]
        and 0 <= clock - value["updated_at"] <= MAX_AGE
        and value["elapsed_seconds"] >= 10
        and (total is None or value["processed"] <= total)
    )
    rate = value.get("rate_per_second")
    if (
        live
        and type(rate) in (int, float)
        and 0 < rate <= 2**63 - 1
        and math.isfinite(rate)
    ):
        result["rate_per_second"] = rate
        remaining = value.get("estimated_remaining_seconds")
        if (
            total is not None
            and 0 < value["processed"] < total
            and _count(remaining)
            and remaining > 0
        ):
            result["estimated_remaining_seconds"] = remaining
    return result


class IOProgress:
    """Bounded reporting for one pass; cancellation remains a per-call check."""

    def __init__(
        self,
        report,
        phase,
        *,
        total=None,
        total_is_estimate=False,
        check=None,
        publish_check=None,
        monotonic=None,
        wall_clock=None,
        message=None,
    ):
        if phase not in PHASES or (total is not None and not _count(total)):
            raise ValueError("Invalid migration I/O phase or total.")
        self.report = report or (lambda *args, **kwargs: None)
        if report is not None:
            try:
                parameters = signature(report)
            except (TypeError, ValueError):
                pass
            else:
                try:
                    parameters.bind("stage", "message", io_progress={})
                except TypeError:
                    # Older internal archive callbacks accepted two positional
                    # arguments. Inspect the signature, never swallow an error
                    # raised by the callback itself.
                    self.report = lambda stage, message, **detail: report(
                        stage, message
                    )
        self.phase = phase
        self.stage, self.unit = PHASES[phase]
        self.total, self.total_is_estimate = total, bool(total_is_estimate)
        self.check = check or (lambda: None)
        self.publish_check = publish_check or (lambda: None)
        self.clock = monotonic or time.monotonic
        self.wall = wall_clock or time.time
        self.message = message or "Processing the current migration phase."
        self.phase_id = secrets.token_hex(12)
        self.started = self.clock()
        self.last_save = float("-inf")
        self.last_sample = self.started
        self.samples = deque([(self.started, 0)], maxlen=11)
        self.processed = 0
        self.complete = False
        self.check()
        self._publish(force=True)

    def update(self, processed):
        self.check()
        if not _count(processed) or processed < self.processed:
            raise ValueError("Migration I/O counters must increase within a phase.")
        self.processed = processed
        self._publish()

    def add(self, amount):
        if not _count(amount):
            raise ValueError("Migration I/O byte counts must be nonnegative.")
        self.update(self.processed + amount)

    def _publish(self, *, force=False):
        now = self.clock()
        if not force and now - self.last_save < 1:
            return
        self.publish_check()
        if now - self.last_sample > MAX_AGE:
            self.samples.clear()
        self.samples.append((now, self.processed))
        self.last_sample = now
        elapsed = max(0, now - self.started)
        span = now - self.samples[0][0]
        measured = self.processed - self.samples[0][1]
        rate = remaining = None
        if (
            not self.complete
            and elapsed >= 10
            and span >= 2
            and measured > 0
            and (self.total is None or self.processed <= self.total)
        ):
            candidate = measured / span
            if math.isfinite(candidate) and candidate > 0:
                rate = candidate
                if self.total is not None and self.processed < self.total:
                    remaining = math.ceil((self.total - self.processed) / rate)
        value = {
            "phase_id": self.phase_id,
            "phase": self.phase,
            "unit": self.unit,
            "processed": self.processed,
            "total": self.total,
            "total_is_estimate": self.total_is_estimate,
            "elapsed_seconds": int(elapsed),
            "updated_at": int(self.wall()),
            "rate_per_second": rate,
            "estimated_remaining_seconds": remaining,
            "complete": self.complete,
        }
        self.report(self.stage, self.message, io_progress=value)
        self.last_save = now

    def finish(self):
        self.check()
        self.complete = True
        self._publish(force=True)


class ProgressReader:
    """Count bytes returned by an existing reader without a second read."""

    def __init__(self, handle, progress):
        self.handle, self.progress = handle, progress

    def read(self, size=-1):
        self.progress.check()
        data = self.handle.read(size)
        self.progress.add(len(data))
        return data


class ProgressWriter:
    """Count bytes accepted by the existing synchronous archive writer."""

    def __init__(self, handle, progress):
        self.handle, self.progress = handle, progress

    def write(self, data):
        self.progress.check()
        amount = self.handle.write(data)
        self.progress.add(len(data) if amount is None else amount)
        return amount
