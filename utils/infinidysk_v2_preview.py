"""One reconnectable, metadata-only migration preview per controller."""

from copy import deepcopy
import hashlib
import json
import logging
import re
import secrets
import threading
import time
import traceback

from utils.infinidysk_v2_scan_progress import MetadataScanProgress, public_scan_progress

ACTIVE = {"queued", "running"}
MAX_REQUEST_ALIASES = 128
STRINGS = {"mode", "mount_backend", "import_strategy", "backup_destination_id"}
BOOLEANS = {
    "use_default_backup_destination",
    "reclaim_source_space",
    "offload_legacy_blobs",
    "omit_queued_downloads",
}


class PreviewCancelled(RuntimeError):
    """A metadata scan acknowledged its own cancellation."""


class PreviewTasks:
    def __init__(self, manager):
        self.manager = manager
        self.path = manager.state_root / "preview-task.json"
        self.thread = None
        self.current = None
        self.busy = None
        self.cancelled = threading.Event()
        self.last_save = 0.0

    def _error(self, message):
        from utils.infinidysk_v2_migration import InfiniDyskV2MigrationError

        return InfiniDyskV2MigrationError(message)

    def _read(self):
        return self.manager._read(self.path)

    def _save(self, task):
        task["updated_at"] = int(time.time())
        self.manager._write(self.path, task)

    def _public(self, task):
        if task is None:
            return None
        public = deepcopy(task)
        public.pop("worker_id", None)
        public.pop("request_aliases", None)
        active = (
            task.get("status") in ACTIVE
            and self.busy == task.get("task_id")
            and task.get("worker_id") == self.manager._worker_id
        )
        if task.get("status") in ACTIVE and not active:
            public.update(
                status="interrupted",
                stage="interrupted",
                message="DUMB restarted before this preview finished. Generate a new preview.",
                error="The interrupted preview did not stop services or change source files.",
                preview=None,
            )
        scan = public_scan_progress(task.get("scan_progress"), active=active)
        if scan is not None:
            public["scan_progress"] = scan
        else:
            public.pop("scan_progress", None)
        public["can_cancel"] = active and not task.get("cancel_requested", False)
        public["preview_available"] = False
        public["preview_unavailable_reason"] = None
        preview = public.get("preview")
        if public.get("status") == "completed" and isinstance(preview, dict):
            token = preview["preview_token"]
            saved = self.manager._read(
                self.manager.state_root
                / "previews"
                / f"{hashlib.sha256(token.encode()).hexdigest()}.json"
            )
            reason = (
                "expired"
                if preview["expires_at"] < int(time.time())
                else "consumed" if not saved or saved.get("used_for_start") else None
            )
            public["preview_unavailable_reason"] = reason
            public["preview_available"] = reason is None
            if reason:
                public["preview"] = None
        return public

    def status(self, identifier=None):
        with self.manager._lock:
            if identifier is not None and not re.fullmatch(
                r"[0-9a-f]{32}", str(identifier)
            ):
                raise self._error("Invalid migration preview task identity.")
            task = self._read()
            if identifier is not None and (
                not task or task.get("task_id") != identifier
            ):
                raise self._error(
                    "This preview task is no longer retained. Refresh preview status."
                )
            return {"protocol": 1, "task": self._public(task)}

    def require_idle(self):
        if self.busy is not None:
            raise self._error(
                "A migration preview is already scanning. Wait for it or cancel it first."
            )

    def run_sync(self, options):
        with self.manager._lock:
            self.require_idle()
            if self.manager.has_blocking_job():
                raise self._error(
                    "Finish migration or recovery before generating another preview."
                )
            self.busy = "synchronous"
        try:
            return self.manager._compute_preview(options)
        finally:
            with self.manager._lock:
                self.busy = None

    def start(self, request):
        options = {
            key: value for key, value in request.items() if key in STRINGS | BOOLEANS
        }
        if any(
            type(value) is not str or len(value) > 4096
            for key, value in options.items()
            if key in STRINGS
        ):
            raise self._error("Preview selections must contain supported text values.")
        if any(
            type(value) is not bool for key, value in options.items() if key in BOOLEANS
        ):
            raise self._error(
                "Preview storage choices must be explicit boolean values."
            )
        fingerprint = hashlib.sha256(
            json.dumps(options, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        request_id = request.get("request_id")
        if request_id is not None and (
            not isinstance(request_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id)
        ):
            raise self._error("Invalid migration preview request identity.")
        with self.manager._lock:
            previous = self._read()
            if (
                request_id is not None
                and previous
                and (
                    previous.get("request_id") == request_id
                    or request_id in previous.get("request_aliases", [])
                )
            ):
                if previous.get("options_fingerprint") != fingerprint:
                    raise self._error(
                        "This preview request identity belongs to different selections."
                    )
                return {"protocol": 1, "task": self._public(previous)}
            if self.busy is not None:
                if (
                    previous
                    and self.busy == previous.get("task_id")
                    and previous.get("options_fingerprint") == fingerprint
                ):
                    if request_id is not None:
                        aliases = self.current.setdefault("request_aliases", [])
                        if request_id not in aliases:
                            if len(aliases) >= MAX_REQUEST_ALIASES:
                                raise self._error(
                                    "This preview has too many pending requests. Wait for it to finish before generating a fresh preview."
                                )
                            aliases.append(request_id)
                        # Update the live object too: subsequent progress and
                        # completion writes must retain every accepted retry ID.
                        self._save(self.current)
                        previous = self.current
                    return {"protocol": 1, "task": self._public(previous)}
                self.require_idle()
            if self.manager.has_blocking_job():
                raise self._error(
                    "Finish migration or recovery before generating another preview."
                )
            identifier = secrets.token_hex(16)
            now = int(time.time())
            task = dict(
                task_id=identifier,
                request_id=request_id,
                request_aliases=[],
                worker_id=self.manager._worker_id,
                status="queued",
                stage="queued",
                message="Preview queued. Services keep running; no source files are changed.",
                created_at=now,
                updated_at=now,
                options=options,
                options_fingerprint=fingerprint,
                cancel_requested=False,
                error=None,
                preview=None,
            )
            self.current, self.busy = task, identifier
            self.cancelled.clear()
            try:
                self._save(task)
                self.thread = threading.Thread(
                    target=self._run,
                    args=(task,),
                    name=f"infinidysk-preview-{identifier}",
                    daemon=True,
                )
                self.thread.start()
            except Exception:
                self.busy = None
                task.update(
                    status="failed",
                    stage="failed",
                    error="The preview worker could not start. Retry the preview.",
                )
                self._save(task)
                raise self._error(task["error"]) from None
            return {"protocol": 1, "task": self._public(task)}

    def checkpoint(self, task, stage, message, **detail):
        if self.cancelled.is_set():
            raise PreviewCancelled()
        now = time.monotonic()
        if (
            stage == task.get("stage")
            and now - self.last_save < 1
            and "scan_progress" not in detail
        ):
            return
        with self.manager._lock:
            if self.cancelled.is_set():
                raise PreviewCancelled()
            task.update(status="running", stage=stage, message=message)
            if "scan_progress" in detail:
                task["scan_progress"] = detail["scan_progress"]
            self._save(task)
            self.last_save = now

    def _run(self, task):
        from utils.infinidysk_v2_migration import InfiniDyskV2MigrationError

        def check_cancel():
            if self.cancelled.is_set():
                raise PreviewCancelled()

        reporter = MetadataScanProgress(
            lambda stage, message, **detail: self.checkpoint(
                task, stage, message, **detail
            ),
            check_cancel=check_cancel,
        )
        try:
            preview = self.manager._compute_preview(task["options"], progress=reporter)
            with self.manager._lock:
                if self.cancelled.is_set():
                    raise PreviewCancelled()
                task.update(
                    status="completed",
                    stage="completed",
                    preview=preview,
                    message="Preview complete. Review the result before testing or migrating.",
                )
        except PreviewCancelled:
            task.update(
                status="cancelled",
                stage="cancelled",
                preview=None,
                message="Preview cancelled. Services and source files were left unchanged.",
            )
        except BaseException as error:
            if not isinstance(error, InfiniDyskV2MigrationError):
                frames = [
                    f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}:{frame.name}"
                    for frame in traceback.extract_tb(error.__traceback__)
                ]
                logging.getLogger(__name__).error(
                    "Migration preview task %s failed (%s); frames=%s. Check source and backup storage availability.",
                    task["task_id"],
                    type(error).__name__,
                    frames,
                )
            task.update(
                status="failed",
                stage="failed",
                preview=None,
                error=(
                    str(error)
                    if isinstance(error, InfiniDyskV2MigrationError)
                    else "The preview could not finish. Check source and backup storage availability, then retry."
                ),
                message="Preview failed. No migration was started.",
            )
        finally:
            with self.manager._lock:
                try:
                    self._save(task)
                finally:
                    self.busy = None

    def cancel(self, identifier):
        with self.manager._lock:
            task = self.status(identifier)["task"]
            if not task["can_cancel"]:
                return self.status(identifier)
            self.cancelled.set()
            self.current["cancel_requested"] = True
            self._save(self.current)
            return self.status(identifier)
