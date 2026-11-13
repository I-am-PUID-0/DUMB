"""In-memory, reconnectable progress for read-only installation previews."""

from copy import deepcopy
import secrets
import threading
import time


class PreviewCancelled(RuntimeError):
    """The preview acknowledged cancellation at a filesystem checkpoint."""


class PreviewStatusError(RuntimeError):
    """The requested preview is no longer retained by this controller."""


class PreviewProgress:
    """A bounded status record; its event belongs only to this preview."""

    def __init__(self, owner, identifier):
        self.owner = owner
        self.identifier = identifier

    def checkpoint(self, *, force=False, **fields):
        self.owner.checkpoint(self.identifier, force=force, **fields)


class PreviewStatuses:
    def __init__(self, *, clock=time.time, ttl=900, maximum=32):
        self.clock = clock
        self.ttl = ttl
        self.maximum = maximum
        self._lock = threading.RLock()
        self._records = {}
        self._latest = None

    def _prune(self):
        now = self.clock()
        for identifier, record in list(self._records.items()):
            expires = record["public"]["expires_at"]
            if expires is not None and expires <= now:
                self._records.pop(identifier)
        finished = [
            identifier
            for identifier, record in self._records.items()
            if record["public"]["completed_at"] is not None
        ]
        while len(self._records) > self.maximum and finished:
            self._records.pop(finished.pop(0))
        if self._latest not in self._records:
            self._latest = next(reversed(self._records), None)

    def begin(self, destination, compression="none"):
        with self._lock:
            self._prune()
            identifier = secrets.token_hex(16)
            now = self.clock()
            public = {
                "preview_id": identifier,
                "kind": "backup",
                "destination_id": destination,
                "compression": compression,
                "status": "running",
                "stage": "preflight",
                "message": "Checking the backup destination and managed state.",
                "started_at": now,
                "updated_at": now,
                "last_progress_at": now,
                "completed_at": None,
                "elapsed_seconds": 0,
                "entries": 0,
                "logical_bytes": 0,
                "roots_completed": 0,
                "root_count": 0,
                "current_root": None,
                "cancel_requested": False,
                "result": None,
                "expires_at": None,
            }
            self._records[identifier] = {
                "public": public,
                "cancel": threading.Event(),
                "cancel_open": True,
                "last_report": time.monotonic(),
            }
            self._latest = identifier
            self._prune()
            return PreviewProgress(self, identifier)

    def checkpoint(self, identifier, *, force=False, **fields):
        with self._lock:
            record = self._records[identifier]
            if record["cancel"].is_set():
                raise PreviewCancelled
            now = time.monotonic()
            if not force and now - record["last_report"] < 0.5:
                return
            allowed = {
                "stage",
                "message",
                "entries",
                "logical_bytes",
                "roots_completed",
                "root_count",
                "current_root",
            }
            public = record["public"]
            public.update(
                {key: value for key, value in fields.items() if key in allowed}
            )
            public["updated_at"] = public["last_progress_at"] = self.clock()
            record["last_report"] = now

    def seal(self, identifier):
        """Resolve cancel-versus-success before releasing this preview's lane."""
        with self._lock:
            record = self._records[identifier]
            record["cancel_open"] = False
            if record["cancel"].is_set():
                raise PreviewCancelled

    def close_cancel_window(self, identifier):
        with self._lock:
            self._records[identifier]["cancel_open"] = False

    def finish(self, identifier, status, *, result=None):
        with self._lock:
            record = self._records[identifier]
            now = self.clock()
            messages = {
                "completed": "Preview complete. Review the estimate before starting a backup.",
                "cancelled": "Preview cancelled. The preview did not start a backup or stop services.",
                "failed": "The preview could not finish. It did not start a backup or stop services.",
            }
            public = record["public"]
            public.update(
                status=status,
                stage=status,
                message=messages[status],
                updated_at=now,
                last_progress_at=now,
                completed_at=now,
                result=deepcopy(result),
                expires_at=(result or {}).get("expires_at", now + self.ttl),
            )
            record["cancel_open"] = False
            self._prune()

    def snapshot(self, identifier=None):
        with self._lock:
            self._prune()
            selected = identifier or self._latest
            if selected is None:
                return None
            if selected not in self._records:
                raise PreviewStatusError(
                    "This preview is unavailable or expired; generate a fresh preview."
                )
            public = deepcopy(self._records[selected]["public"])
            end = public["completed_at"] or self.clock()
            public["elapsed_seconds"] = max(0, end - public["started_at"])
            return public

    def cancel(self, identifier):
        with self._lock:
            self._prune()
            if identifier not in self._records:
                raise PreviewStatusError(
                    "This preview is unavailable or expired; generate a fresh preview."
                )
            record = self._records[identifier]
            public = record["public"]
            if public["status"] == "running" and record["cancel_open"]:
                record["cancel"].set()
                public.update(
                    status="cancelling",
                    stage="cancelling",
                    message="Cancellation requested; waiting for the current filesystem check and temporary workspace cleanup.",
                    cancel_requested=True,
                    updated_at=self.clock(),
                )
            return self.snapshot(identifier)

    def invalidate_result(self, identifier):
        with self._lock:
            record = self._records.get(identifier)
            if record is not None and record["public"]["result"] is not None:
                record["public"].update(
                    result=None,
                    message="This preview was used or expired; generate a fresh preview before another backup.",
                    updated_at=self.clock(),
                )
