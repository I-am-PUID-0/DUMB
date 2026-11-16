"""Bounded metadata-scan reporting, separate from per-entry cancellation.

This publisher never coalesces recovery or source-mutation checkpoints. It is
used only by read-only eligibility/capacity walks before their result is used.
"""

from collections import deque
from contextlib import contextmanager
import math
import re
import secrets
import time

PHASES = {
    "reuse_eligibility",
    "source_data",
    "source_runtime",
    "legacy_blob_allocation",
}
STAGES = {
    "reuse_eligibility": "checking_reuse",
    "source_data": "checking_capacity",
    "source_runtime": "checking_capacity",
    "legacy_blob_allocation": "checking_blob_storage",
}
MAX_METRIC_AGE = 30


def _count(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def public_scan_progress(value, *, active, now=None):
    """Only fixed fields cross the API; stale/terminal estimates are omitted."""
    if not isinstance(value, dict):
        return None
    phase, identifier = value.get("phase"), value.get("scan_id")
    if (
        not isinstance(phase, str)
        or phase not in PHASES
        or not isinstance(identifier, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier)
        or value.get("estimate_scope") != "current_scan"
        or type(value.get("complete")) is not bool
    ):
        return None
    fields = (
        "entries_processed",
        "logical_bytes_processed",
        "elapsed_seconds",
        "updated_at",
    )
    if any(not _count(value.get(key)) for key in fields):
        return None
    result = {key: value[key] for key in fields}
    result.update(
        scan_id=identifier,
        phase=phase,
        estimate_scope="current_scan",
        complete=value["complete"],
    )
    total = value.get("entries_total")
    if not _count(total) or value.get("total_is_estimate") is not True:
        total = None
    result.update(
        entries_total=total,
        total_is_estimate=total is not None,
        rate_entries_per_second=None,
        estimated_remaining_seconds=None,
    )
    clock = time.time() if now is None else now
    rate, remaining = value.get("rate_entries_per_second"), value.get(
        "estimated_remaining_seconds"
    )
    fresh = 0 <= clock - value["updated_at"] <= MAX_METRIC_AGE
    if (
        active
        and not value["complete"]
        and fresh
        and total is not None
        and 0 < value["entries_processed"] < total
        and value["elapsed_seconds"] >= 10
        and type(rate) in (int, float)
        and math.isfinite(rate)
        and rate > 0
        and _count(remaining)
    ):
        result.update(
            rate_entries_per_second=rate, estimated_remaining_seconds=remaining
        )
    return result


class MetadataScanProgress:
    """Publish at most once a second, plus explicit phase boundaries.

    check_cancel remains a cheap per-entry checkpoint even when clocks do not
    advance. expected counts are prior measurements, never proof of completion.
    """

    def __init__(
        self,
        callback=None,
        *,
        check_cancel=None,
        prior_capacity=None,
        monotonic=None,
        wall_clock=None,
    ):
        self.callback = callback
        self.cancel = check_cancel or (lambda: None)
        self.monotonic = monotonic or time.monotonic
        self.wall_clock = wall_clock or time.time
        self.prior_capacity = prior_capacity if isinstance(prior_capacity, dict) else {}
        self.last_save = float("-inf")
        self.last_stage = None
        self.sequence = 0
        self.prefix = secrets.token_hex(8)

    def check_cancel(self):
        self.cancel()

    def _publish(self, stage, message, *, force=False, scan_progress=None):
        self.check_cancel()
        now = self.monotonic()
        if not force and stage == self.last_stage and now - self.last_save < 1:
            return
        if self.callback is not None:
            self.callback(stage, message, scan_progress=scan_progress)
        self.last_save, self.last_stage = now, stage

    def __call__(self, stage, message):
        self._publish(stage, message)

    def _expected(self, phase):
        source = self.prior_capacity.get("source") or {}
        if not isinstance(source, dict):
            return None
        if phase == "legacy_blob_allocation":
            blobs = self.prior_capacity.get("legacy_blobs") or {}
            value = blobs.get("entries") if isinstance(blobs, dict) else None
            return value if _count(value) else None
        role = "runtime_dir" if phase == "source_runtime" else "data_dir"
        roots = source.get("roots")
        if isinstance(roots, list):
            for root in roots:
                if (
                    isinstance(root, dict)
                    and root.get("role") == role
                    and _count(root.get("entries"))
                ):
                    return root["entries"]
        if (
            role == "data_dir"
            and source.get("root_count") == 1
            and _count(source.get("entries"))
        ):
            return source["entries"]
        return None

    @contextmanager
    def scan(self, phase, message):
        if phase not in PHASES:
            raise ValueError("Unsupported migration metadata scan phase.")
        self.sequence += 1
        session = _Scan(
            self,
            phase,
            message,
            f"{self.prefix}_{self.sequence}",
            self._expected(phase),
        )
        session.publish(force=True)
        yield session.update
        session.finish()


class _Scan:
    def __init__(self, owner, phase, message, identifier, expected):
        self.owner, self.phase, self.message, self.identifier = (
            owner,
            phase,
            message,
            identifier,
        )
        self.expected = expected
        self.started = owner.monotonic()
        self.entries = self.logical_bytes = 0
        self.samples = deque([(self.started, 0)], maxlen=11)
        self.last_sample = self.started
        self.complete = False

    def update(self, entries, logical_bytes):
        self.owner.check_cancel()
        self.entries, self.logical_bytes = entries, logical_bytes
        self.publish()

    def publish(self, *, force=False):
        now = self.owner.monotonic()
        if not force and now - self.owner.last_save < 1:
            return
        elapsed = max(0, now - self.started)
        if now - self.last_sample > MAX_METRIC_AGE:
            self.samples.clear()
        self.samples.append((now, self.entries))
        self.last_sample = now
        rate = remaining = None
        span = now - self.samples[0][0]
        measured = self.entries - self.samples[0][1]
        if (
            not self.complete
            and elapsed >= 10
            and span >= 2
            and measured > 0
            and self.expected is not None
            and 0 < self.entries < self.expected
        ):
            candidate = measured / span
            if math.isfinite(candidate) and candidate > 0:
                rate = candidate
                remaining = math.ceil((self.expected - self.entries) / candidate)
        state = dict(
            scan_id=self.identifier,
            phase=self.phase,
            entries_processed=self.entries,
            entries_total=self.expected,
            total_is_estimate=self.expected is not None,
            logical_bytes_processed=self.logical_bytes,
            elapsed_seconds=int(elapsed),
            rate_entries_per_second=rate,
            estimated_remaining_seconds=remaining,
            estimate_scope="current_scan",
            updated_at=int(self.owner.wall_clock()),
            complete=self.complete,
        )
        self.owner._publish(
            STAGES[self.phase], self.message, force=force, scan_progress=state
        )

    def finish(self):
        self.complete = True
        self.publish(force=True)


def metadata_progress(callback):
    """Keep the older internal two-argument progress callback compatible."""
    if isinstance(callback, MetadataScanProgress):
        return callback
    return MetadataScanProgress(
        (lambda stage, message, **details: callback(stage, message))
        if callback
        else None
    )
