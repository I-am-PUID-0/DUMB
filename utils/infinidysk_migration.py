"""Opt-in migration state for the NzbDAV to InfiniDysk product cutover."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
from collections.abc import Iterable
from pathlib import Path

from utils.config_loader import CONFIG_MANAGER
from utils.core_services import has_core_service
from utils.media_protection import build_adapter
from utils.logger import redact_sensitive_log_data
from utils.nzbdav_settings import (
    _arr_req,
    _arr_url,
    _get_lidarr_rootfolder_payload,
    _parse_arr_api_key,
    defer_nzbdav_runtime_integrations,
)
from utils.prowlarr_settings import _prowlarr_req
from utils.symlink_repair import (
    backup_symlink_manifest,
    repair_symlinks,
    restore_symlink_manifest,
)

STATE_VERSION = 3
DEFAULT_STATE_PATH = Path("/config/migrations/infinidysk.json")
LEGACY_REPOSITORIES = {("nzbdav", "nzbdav"), ("nzbdav-dev", "nzbdav")}
LEGACY_PATH_MARKERS = ("nzbdav",)
ATTACHED_SERVICE_KEYS = {
    "radarr",
    "sonarr",
    "lidarr",
    "whisparr",
    "seerr",
    "neutarr",
    "profilarr",
    "prowlarr",
    "rclone",
}
ARR_SERVICE_API = {
    "radarr": ("v3", "movie"),
    "sonarr": ("v3", "series"),
    "lidarr": ("v1", "artist"),
    "whisparr": ("v3", "series"),
}
ARR_EDITOR_API = {
    "radarr": ("movie/editor", "movieIds"),
    "sonarr": ("series/editor", "seriesIds"),
    "lidarr": ("artist/editor", "artistIds"),
    "whisparr": ("series/editor", "seriesIds"),
}
MEDIA_SERVICE_KEYS = ("plex", "jellyfin", "emby")
INFINIDYSK_CORE_CONSUMER_KEYS = (
    "rclone",
    "radarr",
    "sonarr",
    "lidarr",
    "whisparr",
    "neutarr",
    "profilarr",
    "seerr",
)
PREFLIGHT_TTL_SECONDS = 30 * 60
ARR_INVENTORY_TIMEOUT_SECONDS = 120
ARR_EDITOR_BATCH_SIZE = 500
ARR_EDITOR_SPLIT_MIN_BATCH_SIZE = 8
ARR_FALLBACK_PROGRESS_INTERVAL = 5
QUIESCE_TIMEOUT_SECONDS = 60 * 60
QUIESCE_POLL_SECONDS = 5
ACTIVE_JOB_STATUSES = {"queued", "running", "rolling_back"}
NAMESPACE_PATH_MAPPINGS = (
    ("/mnt/debrid/nzbdav-symlinks", "/mnt/debrid/infinidysk-symlinks"),
    ("/mnt/debrid/nzbdav", "/mnt/debrid/infinidysk"),
    ("/data/nzbdav", "/data/infinidysk"),
    ("/log/nzbdav.log", "/log/infinidysk.log"),
    ("/nzbdav", "/infinidysk"),
)


class InfiniDyskMigrationError(RuntimeError):
    pass


class NamespaceMoveError(RuntimeError):
    def __init__(self, message: str, rollback_errors: list[str] | None = None):
        super().__init__(message)
        self.rollback_errors = list(rollback_errors or [])


def _contains_legacy_name(value: object) -> bool:
    return isinstance(value, str) and bool(re.search(r"\bnzbdav\b", value, re.I))


def _replace_display_name(value: str) -> str:
    return re.sub(r"\bnzbdav\b", "InfiniDysk", value, flags=re.I)


def _replace_namespace_text(value: str) -> str:
    """Rewrite a legacy path/token without changing unrelated substrings."""
    if not isinstance(value, str):
        return value
    rewritten = value
    for source, destination in NAMESPACE_PATH_MAPPINGS:
        if rewritten == source or rewritten.startswith(f"{source}/"):
            rewritten = destination + rewritten[len(source) :]
            break
    if rewritten.startswith("/"):
        rewritten = re.sub(r"nzbdav", "infinidysk", rewritten, flags=re.I)
    return rewritten


def _replace_legacy_token(value: str, display: bool = False) -> str:
    replacement = "InfiniDysk" if display else "infinidysk"
    return re.sub(r"\bnzbdav\b", replacement, value, flags=re.I)


def _rewrite_config_namespace(value, key: str = ""):
    if isinstance(value, dict):
        return {
            child_key: _rewrite_config_namespace(child, str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_config_namespace(child, key) for child in value]
    if not isinstance(value, str):
        return value
    rewritten = _replace_namespace_text(value)
    if key == "mount_name":
        rewritten = _replace_legacy_token(rewritten)
    elif "category" in key.lower() or key.lower() in {"tag", "tags"}:
        rewritten = _replace_legacy_token(rewritten)
    elif key in {"core_service", "core_services", "key_type"}:
        rewritten = ",".join(
            "infinidysk" if part.strip().lower() == "nzbdav" else part
            for part in rewritten.split(",")
        )
    return rewritten


def _config_fingerprint(config: dict) -> str:
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_error_detail(error: Exception) -> str:
    detail = ""
    if isinstance(error, urllib.error.HTTPError):
        api_name = str(getattr(error, "migration_api_name", "HTTP API") or "HTTP API")
        method = str(getattr(error, "migration_method", "") or "").upper()
        endpoint = str(getattr(error, "migration_endpoint", "") or "")
        response_detail = _http_error_response_detail(error)
        operation = " ".join(part for part in (api_name, method, endpoint) if part)
        detail = f"{operation} returned HTTP {error.code} {error.reason or ''}".strip()
        if response_detail:
            detail = f"{detail}: {response_detail}"
    if not detail:
        detail = str(error or "Migration failed")
    detail = " ".join(redact_sensitive_log_data(detail).split())
    return (detail or error.__class__.__name__)[:500]


def _http_error_response_detail(error: urllib.error.HTTPError) -> str:
    """Return a bounded, redacted API error message without response internals."""

    body = str(getattr(error, "body", "") or "").strip()
    if not body:
        return ""
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        if body.startswith("<"):
            return ""
        return " ".join(body.split())[:300]
    if isinstance(payload, dict):
        values = []
        for key in ("message", "errorMessage", "description", "title", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
            elif isinstance(value, list):
                values.extend(str(item).strip() for item in value if str(item).strip())
        return "; ".join(dict.fromkeys(values))[:300]
    if isinstance(payload, list):
        return "; ".join(str(item).strip() for item in payload if str(item).strip())[
            :300
        ]
    return str(payload)[:300]


def _attach_http_error_context(
    error: urllib.error.HTTPError,
    *,
    api_name: str,
    method: str,
    url: str,
) -> None:
    parsed = urllib.parse.urlsplit(url)
    endpoint = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "", "")
    )
    setattr(error, "migration_api_name", api_name)
    setattr(error, "migration_method", method.upper())
    setattr(error, "migration_endpoint", endpoint)


def _migration_arr_req(
    url: str,
    key: str,
    method: str = "GET",
    data: dict | None = None,
):
    """Use a catalog-sized timeout for every guarded Arr migration request."""
    try:
        return _arr_req(
            url,
            key,
            method,
            data,
            timeout=ARR_INVENTORY_TIMEOUT_SECONDS,
        )
    except urllib.error.HTTPError as error:
        _attach_http_error_context(error, api_name="Arr API", method=method, url=url)
        raise


def _migration_prowlarr_req(
    url: str,
    key: str,
    method: str = "GET",
    data: dict | None = None,
):
    try:
        return _prowlarr_req(
            url,
            key,
            method,
            data,
            timeout=ARR_INVENTORY_TIMEOUT_SECONDS,
        )
    except urllib.error.HTTPError as error:
        _attach_http_error_context(
            error, api_name="Prowlarr API", method=method, url=url
        )
        raise


class InfiniDyskMigrationManager:
    def __init__(self, state_path: str | os.PathLike | None = None):
        configured = os.environ.get("DUMB_INFINIDYSK_MIGRATION_STATE")
        self.state_path = Path(state_path or configured or DEFAULT_STATE_PATH)
        self._lock = threading.RLock()
        self._job_lock = threading.RLock()
        self._active_job_thread = None
        self._worker_id = secrets.token_hex(16)
        self._playback_override = {
            "job_id": None,
            "available": False,
            "requested": False,
            "media_servers": [],
        }

    def _reset_playback_override(self, job_id: str | None = None) -> None:
        with self._job_lock:
            self._playback_override = {
                "job_id": job_id,
                "available": False,
                "requested": False,
                "media_servers": [],
            }

    def _set_playback_override_availability(
        self, job_id: str | None, media_servers: list[str]
    ) -> None:
        if not job_id:
            return
        with self._job_lock:
            if self._playback_override.get("job_id") != job_id:
                return
            self._playback_override["media_servers"] = list(
                dict.fromkeys(str(name) for name in media_servers if name)
            )
            self._playback_override["available"] = bool(media_servers) and not bool(
                self._playback_override.get("requested")
            )

    def _playback_stop_requested(self, job_id: str | None) -> bool:
        if not job_id:
            return False
        with self._job_lock:
            return bool(
                self._playback_override.get("job_id") == job_id
                and self._playback_override.get("requested")
            )

    def _playback_override_media_servers(self, job_id: str | None) -> list[str]:
        if not job_id:
            return []
        with self._job_lock:
            if self._playback_override.get("job_id") != job_id:
                return []
            return list(self._playback_override.get("media_servers") or [])

    def _finish_playback_stop_request(self, job_id: str | None) -> None:
        if not job_id:
            return
        with self._job_lock:
            if self._playback_override.get("job_id") != job_id:
                return
            self._playback_override["available"] = False
            self._playback_override["requested"] = False
            self._playback_override["media_servers"] = []

    def _load_state(self) -> dict:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _sidecar_path(self, suffix: str) -> Path:
        return self.state_path.with_name(f"{self.state_path.stem}.{suffix}.json")

    def _load_sidecar(self, suffix: str, legacy_key: str) -> dict:
        path = self._sidecar_path(suffix)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        state = self._load_state()
        legacy = state.get(legacy_key)
        if not isinstance(legacy, dict):
            return {}
        try:
            self._compact_legacy_state(state)
        except OSError:
            # A read-only or temporarily unavailable state directory must not hide
            # an otherwise readable legacy job/preflight record. The next read can
            # retry the layout migration without changing the migration payload.
            pass
        return legacy

    def _compact_legacy_state(self, state: dict) -> dict:
        compact = copy.deepcopy(state)
        changed = False
        for legacy_key, suffix in (("preflight", "preflight"), ("job", "job")):
            payload = compact.get(legacy_key)
            if not isinstance(payload, dict):
                continue
            self._write_private_json(self._sidecar_path(suffix), payload)
            compact.pop(legacy_key, None)
            changed = True
        if changed:
            compact["state_version"] = STATE_VERSION
            self._save_state(compact)
        return compact

    def _save_sidecar(self, suffix: str, legacy_key: str, payload: dict) -> None:
        self._write_private_json(self._sidecar_path(suffix), payload)
        # Older builds embedded the complete Arr/media inventory and the live job
        # in one state file. Remove that legacy copy after the sidecar is durable so
        # frequent progress polls/updates never parse or rewrite a huge inventory.
        state = self._load_state()
        if legacy_key in state:
            state.pop(legacy_key, None)
            self._save_state(state)

    def _load_preflight(self) -> dict:
        return self._load_sidecar("preflight", "preflight")

    def _save_preflight(self, preflight: dict) -> None:
        self._save_sidecar("preflight", "preflight", preflight)

    def _load_job(self) -> dict:
        return self._load_sidecar("job", "job")

    def _save_state(self, payload: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=".infinidysk-", suffix=".tmp", dir=self.state_path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.state_path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def _save_job(self, job: dict) -> None:
        with self._job_lock:
            job["updated_at"] = int(time.time())
            self._save_sidecar("job", "job", copy.deepcopy(job))

    def _update_job(
        self,
        job: dict,
        *,
        status: str | None = None,
        stage: str,
        message: str,
        percent: int,
        result: dict | None = None,
        error: str | None = None,
        detail: dict | None = None,
    ) -> None:
        previous_stage = job.get("stage")
        if status:
            job["status"] = status
        event = {
            "at": int(time.time()),
            "stage": stage,
            "message": message,
            "percent": max(0, min(100, int(percent))),
        }
        if detail is not None:
            event["detail"] = copy.deepcopy(detail)
        job["stage"] = stage
        job["message"] = message
        job["progress"] = event["percent"]
        if detail is not None:
            job["detail"] = copy.deepcopy(detail)
        elif stage != previous_stage:
            job.pop("detail", None)
        job["events"] = [*(job.get("events") or []), event][-100:]
        if result is not None:
            job["result"] = result
        if error is not None:
            job["error"] = error
        self._save_job(job)

    @staticmethod
    def _public_recovery(state: dict, fallback_error: object = None) -> dict:
        status = str(state.get("status") or "")
        return {
            "cause": _safe_error_detail(
                RuntimeError(str(state.get("last_error") or fallback_error or ""))
            ),
            "rollback_errors": [
                _safe_error_detail(RuntimeError(str(item)))
                for item in state.get("rollback_errors") or []
            ],
            "backup_bundle_path": str(state.get("backup_bundle_path") or ""),
            "config_backup_path": str(state.get("config_backup_path") or ""),
            "manual_restore_required": status == "rollback_attention_required",
        }

    def get_job(self, job_id: str | None = None) -> dict | None:
        with self._job_lock:
            job = self._load_job()
            if not isinstance(job, dict):
                return None
            if job_id and not secrets.compare_digest(
                str(job.get("job_id") or ""), str(job_id)
            ):
                return None
            if job.get("status") in ACTIVE_JOB_STATUSES and not secrets.compare_digest(
                str(job.get("worker_id") or ""), self._worker_id
            ):
                job["status"] = "interrupted"
                job["stage"] = "interrupted"
                job["message"] = (
                    "DUMB restarted while the namespace migration was active. "
                    "Inspect the retained backup bundle and service paths before retrying."
                )
                job["error"] = job["message"]
                job["progress"] = int(job.get("progress") or 0)
                self._save_job(job)
            if job.get("status") in {
                "failed_rolled_back",
                "rollback_attention_required",
            } and not ((job.get("result") or {}).get("recovery")):
                state = self._load_state()
                job["result"] = {
                    "status": job.get("status"),
                    "recovery": self._public_recovery(
                        state, job.get("error") or job.get("message")
                    ),
                }
                self._save_job(job)
            playback_override = self._playback_override
            if job.get("status") in ACTIVE_JOB_STATUSES and secrets.compare_digest(
                str(job.get("job_id") or ""),
                str(playback_override.get("job_id") or ""),
            ):
                job["playback_override_available"] = bool(
                    playback_override.get("available")
                )
                job["playback_stop_requested"] = bool(
                    playback_override.get("requested")
                )
                job["active_media_servers"] = list(
                    playback_override.get("media_servers") or []
                )
            return copy.deepcopy(job)

    def request_playback_stop(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if not job or job.get("status") not in ACTIVE_JOB_STATUSES:
            raise InfiniDyskMigrationError(
                "The InfiniDysk namespace migration is not active."
            )
        if job.get("stage") != "quiescing":
            raise InfiniDyskMigrationError(
                "Playback can be stopped only while the migration is waiting for safe cutover conditions."
            )
        with self._job_lock:
            override = self._playback_override
            if override.get("job_id") != job_id or not override.get("available"):
                raise InfiniDyskMigrationError(
                    "No active media-server playback is currently available to stop."
                )
            override["requested"] = True
            override["available"] = False
        return self.get_job(job_id) or job

    def start_full_namespace_job(
        self,
        preflight_token: str,
        rename_attached_services: bool,
        process_handler,
        logger,
        updater,
    ) -> dict:
        with self._lock:
            current = self.get_job()
            if current and current.get("status") in ACTIVE_JOB_STATUSES:
                raise InfiniDyskMigrationError(
                    "An InfiniDysk namespace migration is already active."
                )
            state = self._load_state()
            preflight = self._load_preflight()
            now = int(time.time())
            if not preflight_token or not secrets.compare_digest(
                str(preflight.get("token") or ""), str(preflight_token)
            ):
                raise InfiniDyskMigrationError(
                    "Run the namespace preflight again before applying the migration."
                )
            if int(preflight.get("expires_at") or 0) < now:
                raise InfiniDyskMigrationError(
                    "The namespace preflight expired. Run it again before applying."
                )
            if preflight.get("blockers"):
                raise InfiniDyskMigrationError(
                    "The namespace preflight still has blockers. Resolve them and run it again."
                )
            if preflight.get("config_fingerprint") != _config_fingerprint(
                CONFIG_MANAGER.config
            ):
                raise InfiniDyskMigrationError(
                    "DUMB configuration changed after preflight. Run the preflight again."
                )
            job = {
                "job_id": secrets.token_hex(16),
                "status": "queued",
                "stage": "queued",
                "message": "Waiting for the guarded namespace migration worker.",
                "progress": 0,
                "events": [],
                "created_at": now,
                "updated_at": now,
                "worker_pid": os.getpid(),
                "worker_id": self._worker_id,
                "result": None,
                "error": None,
            }
            self._update_job(
                job,
                status="queued",
                stage="queued",
                message="Waiting for the guarded namespace migration worker.",
                percent=0,
            )
            self._reset_playback_override(job["job_id"])
            thread = threading.Thread(
                target=self._run_full_namespace_job,
                args=(
                    job,
                    preflight_token,
                    rename_attached_services,
                    process_handler,
                    logger,
                    updater,
                ),
                daemon=True,
                name=f"infinidysk-migration-{job['job_id'][:8]}",
            )
            self._active_job_thread = thread
            thread.start()
            return copy.deepcopy(job)

    def _run_full_namespace_job(
        self,
        job: dict,
        preflight_token: str,
        rename_attached_services: bool,
        process_handler,
        logger,
        updater,
    ) -> None:
        update_lock = getattr(updater, "updating", None)
        acquired = update_lock is None or update_lock.acquire(blocking=False)
        if not acquired:
            self._update_job(
                job,
                status="failed",
                stage="blocked",
                message="A service update is already active. Retry after it finishes.",
                percent=0,
                error="A service update is already active.",
            )
            return
        try:
            result = self.apply_full_namespace(
                preflight_token,
                rename_attached_services,
                process_handler,
                logger,
                job_id=job["job_id"],
                progress_callback=lambda stage, message, percent, status="running", detail=None: self._update_job(
                    job,
                    status=status,
                    stage=stage,
                    message=message,
                    percent=percent,
                    detail=detail,
                ),
            )
            self._update_job(
                job,
                status="completed",
                stage="completed",
                message=result.get("message") or "Namespace migration completed.",
                percent=100,
                result=result,
            )
        except Exception as error:
            state = self._load_state()
            rollback_status = str(state.get("status") or "")
            status = (
                rollback_status
                if rollback_status
                in {"failed_rolled_back", "rollback_attention_required"}
                else "failed"
            )
            recovery = None
            if status in {"failed_rolled_back", "rollback_attention_required"}:
                recovery = self._public_recovery(state, error)
            self._update_job(
                job,
                status=status,
                stage=status,
                message=str(error),
                percent=(
                    100
                    if status in {"failed_rolled_back", "rollback_attention_required"}
                    else int(job.get("progress") or 0)
                ),
                error=str(error),
                result=(
                    {"status": status, "recovery": recovery}
                    if recovery is not None
                    else None
                ),
            )
        finally:
            self._reset_playback_override()
            if update_lock is not None:
                update_lock.release()

    def _backup_config(self, now: int) -> Path:
        backup_dir = self.state_path.parent / "infinidysk-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(backup_dir, 0o700)
        backup_path = backup_dir / (
            f"dumb_config-before-cutover-{now}-{time.time_ns()}.json"
        )
        try:
            payload = Path(CONFIG_MANAGER.file_path).read_bytes()
            fd, temp_path = tempfile.mkstemp(
                prefix=".dumb-config-", suffix=".tmp", dir=backup_dir
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temp_path, 0o600)
                os.replace(temp_path, backup_path)
            except Exception:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise InfiniDyskMigrationError(
                "The InfiniDysk cutover was not started because its configuration backup could not be saved."
            ) from exc
        return backup_path

    @staticmethod
    def _service_config(config: dict) -> dict:
        value = config.get("infinidysk") or config.get("nzbdav")
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _attached_services(config: dict) -> list[dict]:
        attached = []
        for service_key in sorted(ATTACHED_SERVICE_KEYS):
            service = config.get(service_key)
            instances = service.get("instances") if isinstance(service, dict) else None
            if not isinstance(instances, dict):
                continue
            for instance_name, instance in instances.items():
                if not isinstance(instance, dict):
                    continue
                process_name = str(instance.get("process_name") or "")
                if _contains_legacy_name(instance_name) or _contains_legacy_name(
                    process_name
                ):
                    attached.append(
                        {
                            "service_key": service_key,
                            "instance_name": instance_name,
                            "process_name": process_name,
                            "suggested_instance_name": _replace_display_name(
                                str(instance_name)
                            ),
                            "suggested_process_name": _replace_display_name(
                                process_name
                            ),
                        }
                    )
        return attached

    @staticmethod
    def _legacy_paths(config: dict) -> list[dict]:
        found = []

        def walk(value, path):
            if isinstance(value, dict):
                for key, child in value.items():
                    if not path and str(key).lower() == "zurg":
                        continue
                    walk(child, [*path, str(key)])
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    walk(child, [*path, str(index)])
            elif (
                isinstance(value, str)
                and value.startswith("/")
                and any(marker in value.lower() for marker in LEGACY_PATH_MARKERS)
            ):
                found.append({"config_path": ".".join(path), "value": value})

        walk(config, [])
        return found

    @staticmethod
    def _update_process_references(config: dict, renamed: dict[str, str]) -> int:
        if not renamed:
            return 0
        changed = 0

        sidebar = ((config.get("dumb") or {}).get("ui") or {}).get("sidebar") or {}
        service_order = sidebar.get("service_order")
        if isinstance(service_order, list):
            for index, value in enumerate(service_order):
                if isinstance(value, str) and value in renamed:
                    service_order[index] = renamed[value]
                    changed += 1
        shortcuts = sidebar.get("service_shortcuts")
        if isinstance(shortcuts, dict):
            for combo, value in shortcuts.items():
                if isinstance(value, str) and value in renamed:
                    shortcuts[combo] = renamed[value]
                    changed += 1

        notifications = (config.get("dumb") or {}).get("notifications") or {}
        destinations = notifications.get("destinations")
        if isinstance(destinations, list):
            for destination in destinations:
                service_names = (
                    destination.get("service_names")
                    if isinstance(destination, dict)
                    else None
                )
                if not isinstance(service_names, list):
                    continue
                for index, value in enumerate(service_names):
                    if isinstance(value, str) and value in renamed:
                        service_names[index] = renamed[value]
                        changed += 1
        return changed

    @staticmethod
    def _process_running(process_handler, process_name: str) -> bool:
        if process_handler is None or not process_name:
            return False
        internal_name = process_name
        prefixed = getattr(process_handler, "_prefixed_name", None)
        if callable(prefixed):
            internal_name = prefixed(process_name)
        process = (getattr(process_handler, "process_names", {}) or {}).get(
            internal_name
        )
        return bool(process and getattr(process, "poll", lambda: None)() is None)

    @staticmethod
    def _enabled_instances(config: dict, keys: Iterable[str]) -> list[dict]:
        enabled = []
        for service_key in dict.fromkeys(keys):
            service = config.get(service_key)
            instances = service.get("instances") if isinstance(service, dict) else None
            if not isinstance(instances, dict):
                continue
            for instance_name, instance in instances.items():
                if not isinstance(instance, dict) or not instance.get("enabled"):
                    continue
                enabled.append(
                    {
                        "service_key": service_key,
                        "instance_name": instance_name,
                        "process_name": str(
                            instance.get("process_name") or instance_name
                        ),
                        "config": instance,
                    }
                )
        return enabled

    @staticmethod
    def _linked_instances(config: dict, keys: Iterable[str]) -> list[dict]:
        linked = []
        for target in InfiniDyskMigrationManager._enabled_instances(config, keys):
            service_key = target["service_key"]
            instance = target["config"]
            linked_by_core = has_core_service(instance, "infinidysk")
            linked_by_rclone_type = service_key == "rclone" and str(
                instance.get("key_type") or ""
            ).strip().lower() in {"infinidysk", "nzbdav"}
            if not (linked_by_core or linked_by_rclone_type):
                continue
            linked.append(target)
        return linked

    @staticmethod
    def _linked_service_inventory(config: dict, process_handler) -> list[dict]:
        return [
            {
                "service_key": item["service_key"],
                "instance_name": str(item["instance_name"]),
                "process_name": item["process_name"],
                "running": InfiniDyskMigrationManager._process_running(
                    process_handler, item["process_name"]
                ),
            }
            for item in InfiniDyskMigrationManager._linked_instances(
                config, INFINIDYSK_CORE_CONSUMER_KEYS
            )
        ]

    @staticmethod
    def _legacy_core_service_references(config: dict) -> list[dict]:
        references = []
        for service_key in INFINIDYSK_CORE_CONSUMER_KEYS:
            service = config.get(service_key)
            instances = service.get("instances") if isinstance(service, dict) else None
            if not isinstance(instances, dict):
                continue
            for instance_name, instance in instances.items():
                if not isinstance(instance, dict):
                    continue
                fields = ["core_service", "core_services"]
                if service_key == "rclone":
                    fields.append("key_type")
                for field in fields:
                    value = instance.get(field)
                    values = value if isinstance(value, list) else [value]
                    if any(
                        isinstance(item, str) and _contains_legacy_name(item)
                        for item in values
                    ):
                        references.append(
                            {
                                "service_key": service_key,
                                "instance_name": str(instance_name),
                                "field": field,
                            }
                        )
        return references

    @staticmethod
    def _namespace_filesystem_plan(config: dict) -> tuple[list[dict], list[str]]:
        actions = []
        blockers = []
        mappings = list(NAMESPACE_PATH_MAPPINGS)
        allowed_roots = {
            "/data",
            "/mnt/debrid",
            "/log",
            "/cache",
            "/config",
            "/nzbdav",
        }
        allowed_roots.update(f"/{key.lower()}" for key in config if key != "dumb")
        allowed_roots.update(
            str(Path(source).parent) for source, _ in NAMESPACE_PATH_MAPPINGS
        )

        for entry in InfiniDyskMigrationManager._legacy_paths(config):
            source = str(entry.get("value") or "")
            destination = _replace_namespace_text(source)
            if not source or source == destination or "{" in source:
                continue
            if source.startswith("/config/symlink-repair/snapshots/"):
                continue
            if not any(
                source == root or source.startswith(f"{root}/")
                for root in allowed_roots
            ):
                blockers.append(
                    f"Legacy path {source} is outside DUMB-managed roots and cannot be moved automatically. Change it to a managed path or use the compatibility cutover."
                )
                continue
            mappings.append((source, destination))

        unique_mappings = []
        seen_sources = set()
        for source, destination in sorted(mappings, key=lambda item: len(item[0])):
            if source in seen_sources:
                continue
            if any(
                source.startswith(f"{parent_source}/")
                for parent_source, _ in unique_mappings
            ):
                continue
            seen_sources.add(source)
            unique_mappings.append((source, destination))

        for source, destination in unique_mappings:
            source_path = Path(source)
            destination_path = Path(destination)
            if not os.path.lexists(source_path):
                continue
            source_type = (
                "symlink"
                if source_path.is_symlink()
                else "directory" if source_path.is_dir() else "file"
            )
            destination_state = "absent"
            if os.path.lexists(destination_path):
                destination_state = (
                    "symlink"
                    if destination_path.is_symlink()
                    else "directory" if destination_path.is_dir() else "file"
                )
                empty_directory = (
                    destination_path.is_dir()
                    and not destination_path.is_symlink()
                    and not any(destination_path.iterdir())
                )
                compatible_link = (
                    destination_path.is_symlink()
                    and source == "/nzbdav"
                    and os.path.realpath(destination_path) == "/data/infinidysk"
                )
                if not (empty_directory or compatible_link):
                    blockers.append(
                        f"Destination {destination} already exists and is not an empty managed directory."
                    )
            if source != "/nzbdav":
                try:
                    source_device = os.stat(source_path.parent).st_dev
                    destination_parent = destination_path.parent
                    while (
                        not destination_parent.exists()
                        and destination_parent != Path("/")
                    ):
                        destination_parent = destination_parent.parent
                    destination_device = os.stat(destination_parent).st_dev
                    if source_device != destination_device:
                        blockers.append(
                            f"{source} and {destination} are on different filesystems; atomic rollback-safe rename is unavailable."
                        )
                except OSError as error:
                    blockers.append(
                        f"Could not validate filesystem placement for {source}: {error}"
                    )
            actions.append(
                {
                    "source": source,
                    "destination": destination,
                    "source_type": source_type,
                    "destination_state": destination_state,
                    "mounted": bool(os.path.ismount(source)),
                }
            )
        return actions, blockers

    @staticmethod
    def _arr_target_snapshot(
        target: dict, process_handler
    ) -> tuple[dict | None, list[str]]:
        blockers = []
        key = target["service_key"]
        instance = target["config"]
        process_name = target["process_name"]
        if not InfiniDyskMigrationManager._process_running(
            process_handler, process_name
        ):
            return None, [
                f"{process_name} must be running so DUMB can inventory its queue and paths."
            ]
        port = instance.get("port") or instance.get("host_port")
        token = _parse_arr_api_key(str(instance.get("config_file") or ""))
        if not port or not token:
            return None, [
                f"{process_name} is missing a usable port or API key for guarded path updates."
            ]
        host = f"http://127.0.0.1:{int(port)}"
        api_version, item_endpoint = ARR_SERVICE_API[key]
        endpoint = "queue"
        try:
            queue = (
                _migration_arr_req(
                    _arr_url(
                        host,
                        api_version,
                        "queue?page=1&pageSize=1000&includeUnknownMovieItems=true&includeUnknownSeriesItems=true",
                    ),
                    token,
                    "GET",
                )
                or {}
            )
            queue_records = (
                queue.get("records") if isinstance(queue, dict) else queue
            ) or []
            total_records = (
                int(queue.get("totalRecords") or len(queue_records))
                if isinstance(queue, dict)
                else len(queue_records)
            )
            endpoint = "root folders"
            roots = (
                _migration_arr_req(
                    _arr_url(host, api_version, "rootfolder"), token, "GET"
                )
                or []
            )
            endpoint = item_endpoint
            items = (
                _migration_arr_req(
                    _arr_url(host, api_version, item_endpoint), token, "GET"
                )
                or []
            )
            endpoint = "download clients"
            clients = (
                _migration_arr_req(
                    _arr_url(host, api_version, "downloadclient"), token, "GET"
                )
                or []
            )
            endpoint = "tags"
            tags = (
                _migration_arr_req(_arr_url(host, api_version, "tag"), token, "GET")
                or []
            )
        except Exception as error:
            detail = _safe_error_detail(error)
            timeout_hint = (
                f" after {ARR_INVENTORY_TIMEOUT_SECONDS} seconds"
                if isinstance(error, (TimeoutError, OSError))
                and "timed out" in detail.lower()
                else ""
            )
            return None, [
                f"{process_name} API inventory failed while reading {endpoint}{timeout_hint}: {detail}."
            ]
        return (
            {
                "service_key": key,
                "instance_name": target["instance_name"],
                "process_name": process_name,
                "host": host,
                "api_version": api_version,
                "item_endpoint": item_endpoint,
                "api_key": token,
                "roots": roots,
                "items": items,
                "clients": clients,
                "tags": tags,
                "queue_count": total_records,
            },
            blockers,
        )

    @staticmethod
    def _arr_snapshot(
        config: dict, process_handler
    ) -> tuple[list[dict], list[str], list[dict]]:
        snapshots = []
        blockers = []
        discovery = []
        for target in InfiniDyskMigrationManager._enabled_instances(
            config, set(ARR_SERVICE_API)
        ):
            configured_link = has_core_service(target["config"], "infinidysk")
            snapshot, target_blockers = InfiniDyskMigrationManager._arr_target_snapshot(
                target, process_handler
            )
            if snapshot is None:
                blockers.extend(target_blockers)
                discovery.append(
                    {
                        "service_key": target["service_key"],
                        "instance_name": str(target["instance_name"]),
                        "process_name": target["process_name"],
                        "included": False,
                        "reasons": [
                            "Inventory failed, so DUMB could not determine whether this enabled Arr references the legacy namespace."
                        ],
                    }
                )
                continue

            desired = InfiniDyskMigrationManager._desired_arr_snapshot(snapshot)
            counts = InfiniDyskMigrationManager._arr_change_counts(snapshot, desired)
            reasons = []
            if configured_link:
                reasons.append("Configured core_service linkage")
            labels = {
                "roots": "legacy root-folder reference(s)",
                "items": "legacy item-path reference(s)",
                "clients": "legacy download-client/category reference(s)",
                "tags": "legacy Arr tag(s)",
            }
            reasons.extend(
                f"{count} {labels[key]}" for key, count in counts.items() if count
            )
            included = bool(configured_link or any(counts.values()))
            discovery.append(
                {
                    "service_key": target["service_key"],
                    "instance_name": str(target["instance_name"]),
                    "process_name": target["process_name"],
                    "included": included,
                    "reasons": reasons
                    or [
                        "No configured InfiniDysk linkage or live legacy path, category, or tag reference was found."
                    ],
                }
            )
            if included:
                snapshot["discovery_reasons"] = reasons
                snapshots.append(snapshot)
        return snapshots, blockers, discovery

    @staticmethod
    def _prowlarr_snapshot(
        config: dict, process_handler
    ) -> tuple[list[dict], list[str]]:
        snapshots = []
        blockers = []
        service = config.get("prowlarr")
        instances = service.get("instances") if isinstance(service, dict) else None
        if not isinstance(instances, dict):
            return snapshots, blockers
        for instance_name, instance in instances.items():
            if not isinstance(instance, dict) or not instance.get("enabled"):
                continue
            process_name = str(instance.get("process_name") or "Prowlarr")
            if not InfiniDyskMigrationManager._process_running(
                process_handler, process_name
            ):
                blockers.append(
                    f"{process_name} must be running so DUMB can inventory its Arr application connections and tags."
                )
                continue
            port = instance.get("port") or instance.get("host_port")
            token = _parse_arr_api_key(str(instance.get("config_file") or ""))
            if not port or not token:
                blockers.append(
                    f"{process_name} is missing a usable port or API key for guarded Prowlarr updates."
                )
                continue
            host = f"http://127.0.0.1:{int(port)}"
            try:
                applications = (
                    _migration_prowlarr_req(f"{host}/api/v1/applications", token, "GET")
                    or []
                )
                tags = _migration_prowlarr_req(f"{host}/api/v1/tag", token, "GET") or []
            except Exception as error:
                blockers.append(
                    f"{process_name} API inventory failed: {_safe_error_detail(error)}."
                )
                continue
            snapshot = {
                "service_key": "prowlarr",
                "instance_name": str(instance_name),
                "process_name": process_name,
                "host": host,
                "api_key": token,
                "applications": applications,
                "tags": tags,
            }
            blockers.extend(
                InfiniDyskMigrationManager._prowlarr_namespace_conflicts(snapshot)
            )
            snapshots.append(snapshot)
        return snapshots, blockers

    @staticmethod
    def _prowlarr_namespace_conflicts(snapshot: dict) -> list[str]:
        """Reject an ambiguous Prowlarr rename before any namespace paths move."""

        desired = InfiniDyskMigrationManager._desired_prowlarr_snapshot(snapshot)
        process_name = str(snapshot.get("process_name") or "Prowlarr")
        conflicts = []
        for key, field, label in (
            ("tags", "label", "tag label"),
            ("applications", "name", "application name"),
        ):
            original_records = snapshot.get(key) or []
            desired_records = desired.get(key) or []
            by_value: dict[str, list[tuple[dict, dict]]] = {}
            for original, target in zip(original_records, desired_records):
                value = str(target.get(field) or "").strip().casefold()
                if value:
                    by_value.setdefault(value, []).append((original, target))
            for value, records in by_value.items():
                if len(records) < 2 or not any(
                    original.get(field) != target.get(field)
                    for original, target in records
                ):
                    continue
                record_ids = ", ".join(
                    str(original.get("id"))
                    for original, _target in records
                    if original.get("id") is not None
                )
                conflicts.append(
                    f"{process_name} already has multiple records that would use the "
                    f"{label} '{value}' after migration"
                    f"{f' (IDs {record_ids})' if record_ids else ''}. Resolve the "
                    "legacy/canonical duplicate in Prowlarr, then run preflight again."
                )
        return conflicts

    @staticmethod
    def _media_snapshot(
        config: dict, process_handler, logger
    ) -> tuple[list[dict], list[str]]:
        snapshots = []
        blockers = []
        configured_media = []
        for key in MEDIA_SERVICE_KEYS:
            service = config.get(key)
            if not isinstance(service, dict) or not service.get("enabled"):
                continue
            configured_media.append(key)
            process_name = str(service.get("process_name") or key.title())
            if not InfiniDyskMigrationManager._process_running(
                process_handler, process_name
            ):
                blockers.append(
                    f"{process_name} must be running so DUMB can inventory and update its library paths."
                )
                continue
            adapter = build_adapter(key, process_name, logger)
            activity = adapter.activity()
            if activity.get("state") == "unknown":
                blockers.append(
                    f"{process_name} is {activity.get('state', 'unknown')}: {activity.get('reason') or 'activity could not be verified'}."
                )
                continue
            try:
                libraries = adapter.library_paths()
            except Exception:
                blockers.append(
                    f"{process_name} library inventory failed. Configure its DUMB media-protection API credential and retry."
                )
                continue
            snapshots.append(
                {
                    "service_key": key,
                    "process_name": process_name,
                    "libraries": libraries,
                    "activity": activity,
                    "external_api_only": False,
                }
            )

        if not configured_media:
            dumb = config.get("dumb") or {}
            plex_address = str(dumb.get("plex_address") or "").strip()
            plex_token = str(dumb.get("plex_token") or "").strip()
            if plex_address and plex_token:
                process_name = "External Plex"
                adapter = build_adapter("plex", process_name, logger)
                try:
                    identity = adapter.server_identity()
                    if not str(identity.get("machine_identifier") or ""):
                        raise RuntimeError(
                            "Plex did not return a stable machine identifier"
                        )
                    activity = adapter.activity()
                    if activity.get("state") == "unknown":
                        raise RuntimeError(
                            activity.get("reason")
                            or "Plex activity could not be verified"
                        )
                    libraries = adapter.library_paths()
                except Exception as error:
                    blockers.append(
                        "External Plex connection failed. Verify dumb.plex_address and dumb.plex_token: "
                        f"{_safe_error_detail(error)}."
                    )
                else:
                    snapshots.append(
                        {
                            "service_key": "plex",
                            "process_name": process_name,
                            "libraries": libraries,
                            "activity": activity,
                            "identity": identity,
                            "external_api_only": True,
                        }
                    )
            elif plex_address or plex_token:
                blockers.append(
                    "External Plex discovery requires both dumb.plex_address and dumb.plex_token."
                )
        return snapshots, blockers

    @staticmethod
    def _infinidysk_active_reads(
        config: dict, process_handler
    ) -> tuple[int | None, str | None]:
        service = InfiniDyskMigrationManager._service_config(config)
        process_name = str(service.get("process_name") or "InfiniDysk")
        if not service.get(
            "enabled"
        ) or not InfiniDyskMigrationManager._process_running(
            process_handler, process_name
        ):
            return 0, None
        api_key = str((service.get("env") or {}).get("FRONTEND_BACKEND_API_KEY") or "")
        if not api_key:
            try:
                from utils import nzbdav_db

                api_key = str(nzbdav_db.get_config_value("api.key") or "")
            except Exception:
                api_key = ""
        if not api_key:
            return (
                None,
                "InfiniDysk active reads could not be checked because its API key is unavailable.",
            )
        port = int(service.get("backend_port") or 8080)
        request = __import__(
            "utils.url_security", fromlist=["safe_request"]
        ).safe_request(
            f"http://127.0.0.1:{port}/api/get-overview-stats?window=1h&sections=window,detail",
            headers={"Accept": "application/json", "x-api-key": api_key},
            method="GET",
        )
        try:
            safe_urlopen = __import__(
                "utils.url_security", fromlist=["safe_urlopen"]
            ).safe_urlopen
            with safe_urlopen(request, timeout=5) as response:
                payload = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
            active = int(((payload.get("tiles") or {}).get("activeReads")) or 0)
            return active, None
        except Exception:
            return (
                None,
                "InfiniDysk active reads could not be checked. Verify its backend is healthy.",
            )

    @staticmethod
    def _public_preflight(preflight: dict) -> dict:
        return {
            "token": preflight.get("token"),
            "created_at": preflight.get("created_at"),
            "expires_at": preflight.get("expires_at"),
            "ready": not bool(preflight.get("blockers")),
            "blockers": list(preflight.get("blockers") or []),
            "pending_conditions": list(preflight.get("pending_conditions") or []),
            "warnings": list(preflight.get("warnings") or []),
            "quiesce_timeout_seconds": QUIESCE_TIMEOUT_SECONDS,
            "filesystem": list(preflight.get("filesystem") or []),
            "arr_services": [
                {
                    "process_name": item.get("process_name"),
                    "discovery_reasons": list(item.get("discovery_reasons") or []),
                    "queue_count": item.get("queue_count", 0),
                    "root_changes": sum(
                        1
                        for root in item.get("roots") or []
                        if _replace_namespace_text(str(root.get("path") or ""))
                        != str(root.get("path") or "")
                    ),
                    "item_changes": sum(
                        1
                        for record in item.get("items") or []
                        if _replace_namespace_text(str(record.get("path") or ""))
                        != str(record.get("path") or "")
                    ),
                    "client_changes": sum(
                        1
                        for client in item.get("clients") or []
                        if InfiniDyskMigrationManager._rewrite_external_value(client)
                        != client
                    ),
                    "tag_changes": sum(
                        1
                        for tag in item.get("tags") or []
                        if InfiniDyskMigrationManager._rewrite_external_value(tag)
                        != tag
                    ),
                }
                for item in preflight.get("arr") or []
            ],
            "arr_discovery": [
                {
                    "service_key": item.get("service_key"),
                    "instance_name": item.get("instance_name"),
                    "process_name": item.get("process_name"),
                    "included": bool(item.get("included")),
                    "reasons": list(item.get("reasons") or []),
                }
                for item in preflight.get("arr_discovery") or []
            ],
            "prowlarr_services": [
                {
                    "process_name": item.get("process_name"),
                    "application_changes": sum(
                        1
                        for application in item.get("applications") or []
                        if InfiniDyskMigrationManager._rewrite_external_value(
                            application
                        )
                        != application
                    ),
                    "tag_changes": sum(
                        1
                        for tag in item.get("tags") or []
                        if InfiniDyskMigrationManager._rewrite_external_value(tag)
                        != tag
                    ),
                }
                for item in preflight.get("prowlarr") or []
            ],
            "linked_services": [
                {
                    "service_key": item.get("service_key"),
                    "instance_name": item.get("instance_name"),
                    "process_name": item.get("process_name"),
                    "running": bool(item.get("running")),
                }
                for item in preflight.get("linked_services") or []
            ],
            "media_servers": [
                {
                    "process_name": item.get("process_name"),
                    "server_name": (item.get("identity") or {}).get("name"),
                    "server_version": (item.get("identity") or {}).get("version"),
                    "external_api_only": bool(item.get("external_api_only")),
                    "library_changes": sum(
                        1
                        for library in item.get("libraries") or []
                        for path in library.get("paths") or []
                        if _replace_namespace_text(str(path)) != str(path)
                    ),
                }
                for item in preflight.get("media") or []
            ],
            "active_reads": preflight.get("active_reads"),
        }

    def preflight(
        self, process_handler=None, logger=None, now: int | None = None
    ) -> dict:
        with self._lock:
            config = CONFIG_MANAGER.config
            service = self._service_config(config)
            if not service:
                raise InfiniDyskMigrationError(
                    "InfiniDysk service configuration is not available."
                )
            if not self._legacy_paths(config):
                raise InfiniDyskMigrationError(
                    "No legacy NzbDAV namespace paths were found to migrate."
                )
            now = int(now or time.time())
            filesystem, blockers = self._namespace_filesystem_plan(config)
            linked_services = self._linked_service_inventory(config, process_handler)
            arr, arr_blockers, arr_discovery = self._arr_snapshot(
                config, process_handler
            )
            known_linked = {
                str(item.get("process_name") or "") for item in linked_services
            }
            for item in arr_discovery:
                process_name = str(item.get("process_name") or "")
                if not item.get("included") or process_name in known_linked:
                    continue
                linked_services.append(
                    {
                        "service_key": item.get("service_key"),
                        "instance_name": item.get("instance_name"),
                        "process_name": process_name,
                        "running": self._process_running(process_handler, process_name),
                    }
                )
                known_linked.add(process_name)
            prowlarr, prowlarr_blockers = self._prowlarr_snapshot(
                config, process_handler
            )
            media, media_blockers = self._media_snapshot(
                config, process_handler, logger
            )
            blockers.extend(arr_blockers)
            blockers.extend(prowlarr_blockers)
            blockers.extend(media_blockers)
            active_reads, active_error = self._infinidysk_active_reads(
                config, process_handler
            )
            if active_error:
                blockers.append(active_error)
            pending_conditions = [
                f"{item['process_name']} has {int(item.get('queue_count') or 0)} queued item(s). DUMB will stop queue producers, wait for this queue to drain, and hold the Arr stopped once it is empty."
                for item in arr
                if int(item.get("queue_count") or 0) > 0
            ]
            pending_conditions.extend(
                (
                    f"{item['process_name']} currently has media activity. DUMB will wait for it to become idle and guard Plex scans through the API; pause Autoscan and other external scan producers because DUMB cannot stop an external Plex process."
                    if item.get("external_api_only")
                    else f"{item['process_name']} currently has media activity. DUMB will wait for it to become idle, guard scans, and then hold the server stopped."
                )
                for item in media
                if (item.get("activity") or {}).get("state") == "busy"
            )
            if active_reads:
                external_plex = any(item.get("external_api_only") for item in media)
                pending_conditions.append(
                    (
                        f"InfiniDysk has {active_reads} active read(s). DUMB will wait for them to drain after guarding external Plex; pause Autoscan and other external readers because DUMB cannot stop them."
                        if external_plex
                        else f"InfiniDysk has {active_reads} active read(s). DUMB will wait for them to drain after stopping affected media servers."
                    )
                )
            preflight = {
                "token": secrets.token_urlsafe(32),
                "created_at": now,
                "expires_at": now + PREFLIGHT_TTL_SECONDS,
                "config_fingerprint": _config_fingerprint(config),
                "filesystem": filesystem,
                "linked_services": linked_services,
                "arr": arr,
                "arr_discovery": arr_discovery,
                "prowlarr": prowlarr,
                "media": media,
                "active_reads": active_reads,
                "blockers": blockers,
                "pending_conditions": pending_conditions,
                "warnings": [
                    "Historical snapshot filenames are retained; future snapshots use the InfiniDysk name.",
                    "The migration changes paths but does not start a media-library scan automatically.",
                    "At apply time DUMB automatically stops linked request/search producers first, waits up to one hour for Arr queues and playback to drain, and holds each safe service stopped through the cutover.",
                    *(
                        [
                            "External Plex was inferred from dumb.plex_address and dumb.plex_token. DUMB can guard scans and migrate library paths through Plex's API, but it cannot stop the external Plex process or pause Autoscan."
                        ]
                        if any(item.get("external_api_only") for item in media)
                        else []
                    ),
                ],
            }
            state = self._load_state()
            state.pop("preflight", None)
            state.update(
                {
                    "state_version": STATE_VERSION,
                    "status": state.get("status") or "pending",
                }
            )
            try:
                self._save_preflight(preflight)
                self._save_state(state)
            except OSError as exc:
                raise InfiniDyskMigrationError(
                    "The namespace preflight could not be saved."
                ) from exc
            return self._public_preflight(preflight)

    def status(self, config: dict | None = None, now: int | None = None) -> dict:
        with self._lock:
            runtime_config = getattr(CONFIG_MANAGER, "config", None)
            config = config if isinstance(config, dict) else runtime_config
            config = config if isinstance(config, dict) else {}
            state = self._load_state()
            if any(isinstance(state.get(key), dict) for key in ("preflight", "job")):
                try:
                    state = self._compact_legacy_state(state)
                except OSError:
                    # Status remains available from the legacy file when a
                    # read-only/transient storage condition prevents compaction.
                    pass
            now = int(now or time.time())
            service = self._service_config(config)
            repo = (
                str(service.get("repo_owner") or "").strip().lower(),
                str(service.get("repo_name") or "").strip().lower(),
            )
            attached = self._attached_services(config)
            legacy_paths = self._legacy_paths(config)
            legacy_brand = _contains_legacy_name(service.get("process_name"))
            legacy_repository = repo in LEGACY_REPOSITORIES
            legacy_identity = bool(
                config.get("nzbdav")
                or (
                    config is runtime_config
                    and bool(
                        getattr(
                            CONFIG_MANAGER,
                            "uses_legacy_infinidysk_identity",
                            lambda: False,
                        )()
                    )
                )
            )
            enabled = bool(service.get("enabled"))
            state_completed = state.get("status") == "completed"
            canonical_identity = (
                not legacy_brand and not legacy_repository and not legacy_identity
            )
            identity_completed = canonical_identity and (
                state_completed or bool(legacy_paths)
            )
            namespace_completed = identity_completed and not legacy_paths
            compatibility_completed = identity_completed and bool(legacy_paths)
            snoozed_until = int(state.get("snoozed_until") or 0)
            eligible = bool(service) and (
                legacy_brand
                or legacy_repository
                or legacy_identity
                or bool(legacy_paths)
                or bool(attached)
            )
            return {
                "state_version": STATE_VERSION,
                "eligible": eligible,
                "enabled": enabled,
                "notice_due": (
                    eligible and not namespace_completed and snoozed_until <= now
                ),
                "status": (
                    "completed"
                    if namespace_completed
                    else (
                        "compatibility_completed"
                        if compatibility_completed
                        else "pending" if eligible else "not_needed"
                    )
                ),
                "snoozed_until": snoozed_until or None,
                "completed_at": state.get("completed_at"),
                "selected_mode": state.get("selected_mode"),
                "rename_attached_services": bool(
                    state.get("rename_attached_services", False)
                ),
                "legacy": {
                    "repository": legacy_repository,
                    "brand": legacy_brand,
                    "service_key": legacy_identity,
                    "paths": legacy_paths,
                    "attached_services": attached,
                },
                "modes": [
                    {
                        "id": "retain_legacy_namespace",
                        "recommended": True,
                        "available": True,
                        "title": "Switch to InfiniDysk and keep existing paths",
                        "description": "Use the InfiniDysk repository and name while retaining current mount paths, symlink roots, categories, and media-server libraries.",
                    },
                    {
                        "id": "full_namespace",
                        "recommended": False,
                        "available": True,
                        "title": "Migrate the complete InfiniDysk namespace",
                        "description": "Move managed paths and update Arr and media-server references through the guarded cutover workflow.",
                    },
                ],
                "docs_url": "https://dumbarr.com/services/core/infinidysk/#migration-from-nzbdav",
            }

    def remind_later(self, days: int = 7, now: int | None = None) -> dict:
        if days < 1 or days > 90:
            raise InfiniDyskMigrationError("Reminder days must be between 1 and 90.")
        with self._lock:
            now = int(now or time.time())
            state = self._load_state()
            state.update(
                {
                    "state_version": STATE_VERSION,
                    "status": (
                        "completed" if state.get("status") == "completed" else "snoozed"
                    ),
                    "snoozed_at": now,
                    "snoozed_until": now + (days * 86400),
                }
            )
            try:
                self._save_state(state)
            except OSError as exc:
                raise InfiniDyskMigrationError(
                    "The InfiniDysk reminder could not be saved."
                ) from exc
            return self.status(now=now)

    def apply_brand_cutover(self, rename_attached_services: bool = True) -> dict:
        """Persist the non-path cutover. Runtime restart/install is a separate step."""
        with self._lock:
            config = CONFIG_MANAGER.config
            service = self._service_config(config)
            if not service:
                raise InfiniDyskMigrationError(
                    "InfiniDysk service configuration is not available."
                )

            current_status = self.status(config)
            if current_status.get("status") in {"completed", "compatibility_completed"}:
                state = self._load_state()
                if state.get("selected_mode") != "retain_legacy_namespace":
                    raise InfiniDyskMigrationError(
                        "The complete InfiniDysk namespace migration is already recorded."
                    )
                return {
                    "status": "completed",
                    "selected_mode": "retain_legacy_namespace",
                    "restart_required": False,
                    "process_name": "InfiniDysk",
                    "changed_processes": [],
                    "changed_references": 0,
                    "retained_namespace": True,
                    "config_backup_path": state.get("config_backup_path"),
                    "message": "The InfiniDysk compatibility cutover is already complete.",
                }
            if not current_status.get("eligible"):
                raise InfiniDyskMigrationError(
                    "No legacy NzbDAV identity or namespace was found to migrate."
                )

            now = int(time.time())
            backup_path = self._backup_config(now)
            backup = copy.deepcopy(config)
            legacy_identity = CONFIG_MANAGER.uses_legacy_infinidysk_identity()
            previous_service_name = str(service.get("process_name") or "")
            service["repo_owner"] = "infinidysk"
            service["repo_name"] = "infinidysk"
            service["process_name"] = "InfiniDysk"
            changed_processes = []
            renamed_processes = (
                {previous_service_name: "InfiniDysk"}
                if previous_service_name and previous_service_name != "InfiniDysk"
                else {}
            )

            if rename_attached_services:
                for item in self._attached_services(config):
                    instances = config[item["service_key"]]["instances"]
                    old_instance_name = item["instance_name"]
                    new_instance_name = item["suggested_instance_name"]
                    instance = instances.get(old_instance_name)
                    if not isinstance(instance, dict):
                        continue
                    if (
                        new_instance_name != old_instance_name
                        and new_instance_name in instances
                    ):
                        CONFIG_MANAGER.config = backup
                        raise InfiniDyskMigrationError(
                            f"Cannot rename {old_instance_name} because {new_instance_name} already exists."
                        )
                    old_name = str(instance.get("process_name") or "")
                    new_name = _replace_display_name(old_name)
                    if new_name != old_name:
                        instance["process_name"] = new_name
                        renamed_processes[old_name] = new_name
                    if new_instance_name != old_instance_name:
                        instances[new_instance_name] = instances.pop(old_instance_name)
                    if new_name != old_name or new_instance_name != old_instance_name:
                        changed_processes.append(
                            {
                                "previous": old_name,
                                "current": new_name,
                                "previous_instance": old_instance_name,
                                "current_instance": new_instance_name,
                            }
                        )

            changed_references = self._update_process_references(
                config, renamed_processes
            )

            try:
                CONFIG_MANAGER.adopt_infinidysk_identity()
                CONFIG_MANAGER.save_config()
            except Exception as exc:
                CONFIG_MANAGER.config = backup
                if legacy_identity:
                    CONFIG_MANAGER.restore_legacy_infinidysk_identity()
                raise InfiniDyskMigrationError(
                    "The InfiniDysk configuration cutover could not be saved."
                ) from exc

            state = {
                "state_version": STATE_VERSION,
                "status": "completed",
                "selected_mode": "retain_legacy_namespace",
                "rename_attached_services": bool(rename_attached_services),
                "completed_at": now,
                "snoozed_until": None,
                "config_backup_path": str(backup_path),
            }
            try:
                self._save_state(state)
            except OSError as exc:
                CONFIG_MANAGER.config = backup
                if legacy_identity:
                    CONFIG_MANAGER.restore_legacy_infinidysk_identity()
                try:
                    CONFIG_MANAGER.save_config()
                except Exception as rollback_exc:
                    raise InfiniDyskMigrationError(
                        "The migration state could not be saved and the configuration rollback also failed. Restore dumb_config.json from its backup before restarting DUMB."
                    ) from rollback_exc
                raise InfiniDyskMigrationError(
                    "The migration state could not be saved, so the configuration changes were rolled back."
                ) from exc
            return {
                "status": "completed",
                "selected_mode": "retain_legacy_namespace",
                "restart_required": bool(service.get("enabled")),
                "process_name": "InfiniDysk",
                "changed_processes": changed_processes,
                "changed_references": changed_references,
                "retained_namespace": True,
                "config_backup_path": str(backup_path),
                "message": "InfiniDysk branding and repository settings were saved. Existing paths, categories, and media-server libraries were retained.",
            }

    @staticmethod
    def _write_private_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _create_namespace_backup(
        self, preflight: dict, now: int, progress_callback=None
    ) -> tuple[Path, Path]:
        config_backup = self._backup_config(now)
        bundle = config_backup.parent / f"namespace-{now}-{time.time_ns()}"
        bundle.mkdir(mode=0o700)
        self._write_private_json(bundle / "preflight.json", preflight)

        paths = set()
        config = CONFIG_MANAGER.config
        for service_key in (*ARR_SERVICE_API, "prowlarr", *MEDIA_SERVICE_KEYS):
            service = config.get(service_key)
            candidates = []
            if isinstance(service, dict) and isinstance(service.get("instances"), dict):
                candidates.extend(service["instances"].values())
            elif isinstance(service, dict):
                candidates.append(service)
            for candidate in candidates:
                if isinstance(candidate, dict) and candidate.get("config_file"):
                    paths.add(str(candidate["config_file"]))
        service = self._service_config(config)
        config_dir = str(service.get("config_dir") or "/nzbdav")
        for suffix in ("db.sqlite", "db.sqlite-wal", "db.sqlite-shm"):
            paths.add(str(Path(config_dir) / suffix))

        files_dir = bundle / "files"
        files_dir.mkdir(mode=0o700)
        manifest = []
        for source_value in sorted(paths):
            source = Path(source_value)
            if not source.is_file() or source.is_symlink():
                continue
            source_stat = source.stat()
            size = source_stat.st_size
            if size > 512 * 1024 * 1024:
                raise InfiniDyskMigrationError(
                    f"Required configuration backup {source} is larger than 512 MiB. Back it up manually before retrying."
                )
            destination = files_dir / (
                hashlib.sha256(source_value.encode("utf-8")).hexdigest()[:16]
                + "-"
                + source.name
            )
            shutil.copy2(source, destination)
            os.chmod(destination, 0o600)
            manifest.append(
                {
                    "source": source_value,
                    "backup": str(destination),
                    "size": size,
                    "mode": stat.S_IMODE(source_stat.st_mode),
                    "uid": source_stat.st_uid,
                    "gid": source_stat.st_gid,
                }
            )
        self._write_private_json(bundle / "files.json", {"files": manifest})

        if callable(progress_callback):
            progress_callback(
                "backup",
                "Configuration snapshots complete; cataloging symlinks without reading media targets.",
                16,
            )

        symlink_roots = [
            str(Path(str(action["source"])))
            for action in preflight.get("filesystem") or []
            if "symlink" in Path(str(action.get("source") or "")).name.lower()
            and "nzbdav" in Path(str(action.get("source") or "")).name.lower()
            and Path(str(action["source"])).is_dir()
        ]
        if symlink_roots:

            def symlink_progress(payload: dict) -> None:
                if not callable(progress_callback):
                    return
                processed = int(payload.get("processed_symlinks") or 0)
                total = int(payload.get("total_symlinks") or 0)
                percent = 19 if payload.get("stage") == "completed" else 16
                if total > 0:
                    percent = min(19, 16 + int((processed / total) * 3))
                progress_callback(
                    "backup",
                    f"Cataloging symlinks for rollback ({processed}/{total}).",
                    percent,
                )

            report = backup_symlink_manifest(
                symlink_roots,
                str(bundle / "symlinks-before.json"),
                include_broken=True,
                progress_callback=symlink_progress,
                check_targets=False,
            )
            if report.get("errors"):
                raise InfiniDyskMigrationError(
                    "The symlink backup reported errors; no namespace paths were changed."
                )
            os.chmod(bundle / "symlinks-before.json", 0o600)
        return config_backup, bundle

    @staticmethod
    def _restore_backup_files(bundle: Path) -> list[str]:
        errors = []
        try:
            payload = json.loads((bundle / "files.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return [f"backup manifest: {error}"]
        for item in payload.get("files") or []:
            try:
                source = Path(str(item["backup"]))
                destination = Path(str(item["source"]))
                existing_stat = None
                try:
                    if destination.exists() and not destination.is_symlink():
                        existing_stat = destination.stat()
                except OSError:
                    existing_stat = None
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                mode = item.get("mode")
                uid = item.get("uid")
                gid = item.get("gid")
                if mode is None and existing_stat is not None:
                    mode = stat.S_IMODE(existing_stat.st_mode)
                if uid is None and existing_stat is not None:
                    uid = existing_stat.st_uid
                if gid is None and existing_stat is not None:
                    gid = existing_stat.st_gid
                if uid is None:
                    uid = int(CONFIG_MANAGER.get("puid") or os.getuid())
                if gid is None:
                    gid = int(CONFIG_MANAGER.get("pgid") or os.getgid())
                if mode is not None:
                    os.chmod(destination, int(mode))
                os.chown(destination, int(uid), int(gid))
            except Exception as error:
                errors.append(f"{item.get('source')}: {error}")
        return errors

    @staticmethod
    def _restore_and_validate_symlink_manifest(bundle: Path) -> list[str]:
        """Restore the exact captured link targets after namespace paths return."""

        manifest_path = bundle / "symlinks-before.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            expected = False
            try:
                preflight = json.loads(
                    (bundle / "preflight.json").read_text(encoding="utf-8")
                )
                expected = any(
                    "symlink" in Path(str(action.get("source") or "")).name.lower()
                    for action in preflight.get("filesystem") or []
                )
            except (OSError, json.JSONDecodeError):
                pass
            if expected:
                return ["captured symlink manifest is missing or unsafe"]
            return []
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return [f"symlink manifest: {error}"]

        roots = [
            str(root).strip()
            for root in payload.get("roots") or []
            if str(root).strip()
        ]
        missing_roots = [root for root in roots if not Path(root).is_dir()]
        if missing_roots:
            preview = ", ".join(missing_roots[:3])
            suffix = "" if len(missing_roots) <= 3 else ", ..."
            return [
                "symlink manifest roots were not restored; refusing to create a "
                f"split symlink tree: {preview}{suffix}"
            ]

        try:
            report = restore_symlink_manifest(
                str(manifest_path),
                dry_run=False,
                overwrite_existing=True,
                restore_broken=True,
            )
        except Exception as error:
            return [f"symlink manifest restore: {_safe_error_detail(error)}"]

        errors = [
            f"symlink restore {item.get('link_path')}: {item.get('error')}"
            for item in report.get("errors") or []
        ]
        invalid_entries = int(report.get("skipped_invalid_entries") or 0)
        if invalid_entries:
            errors.append(
                f"symlink manifest contained {invalid_entries} invalid entries"
            )

        mismatches = []
        for entry in payload.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            link_path = str(entry.get("link_path") or "").strip()
            expected_target = str(entry.get("target") or "").strip()
            if not link_path or not expected_target:
                continue
            try:
                if not os.path.islink(link_path):
                    mismatches.append(f"{link_path} is not a symlink")
                elif os.readlink(link_path) != expected_target:
                    mismatches.append(f"{link_path} target did not restore")
            except OSError as error:
                mismatches.append(f"{link_path}: {error}")
            if len(mismatches) >= 20:
                break
        errors.extend(f"symlink validation {item}" for item in mismatches)
        return errors

    @staticmethod
    def _rewrite_external_value(value):
        if isinstance(value, dict):
            return {
                key: InfiniDyskMigrationManager._rewrite_external_value(child)
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [
                InfiniDyskMigrationManager._rewrite_external_value(child)
                for child in value
            ]
        if not isinstance(value, str):
            return value
        rewritten = _replace_namespace_text(value)
        if rewritten == value and _contains_legacy_name(value):
            rewritten = _replace_legacy_token(value, display=not value.islower())
        return rewritten

    @staticmethod
    def _desired_arr_snapshot(snapshot: dict) -> dict:
        desired = copy.deepcopy(snapshot)
        for root in desired.get("roots") or []:
            path = str(root.get("path") or "")
            root["path"] = _replace_namespace_text(path)
            if root.get("name") and root["path"] != path:
                root["name"] = Path(root["path"]).name
        for item in desired.get("items") or []:
            if isinstance(item.get("path"), str):
                item["path"] = _replace_namespace_text(item["path"])
        desired["clients"] = InfiniDyskMigrationManager._rewrite_external_value(
            desired.get("clients") or []
        )
        desired["tags"] = InfiniDyskMigrationManager._rewrite_external_value(
            desired.get("tags") or []
        )
        return desired

    @staticmethod
    def _desired_media_snapshot(snapshot: dict) -> dict:
        desired = copy.deepcopy(snapshot)
        for library in desired.get("libraries") or []:
            library["paths"] = [
                _replace_namespace_text(str(path))
                for path in library.get("paths") or []
            ]
        return desired

    @staticmethod
    def _desired_prowlarr_snapshot(snapshot: dict) -> dict:
        desired = copy.deepcopy(snapshot)
        desired["applications"] = InfiniDyskMigrationManager._rewrite_external_value(
            desired.get("applications") or []
        )
        desired["tags"] = InfiniDyskMigrationManager._rewrite_external_value(
            desired.get("tags") or []
        )
        return desired

    @staticmethod
    def _arr_change_counts(snapshot: dict, desired: dict) -> dict[str, int]:
        counts = {}
        for key in ("roots", "items", "clients", "tags"):
            original = {
                str(item.get("id")): item
                for item in snapshot.get(key) or []
                if item.get("id") is not None
            }
            counts[key] = sum(
                1
                for item in desired.get(key) or []
                if item.get("id") is not None
                and original.get(str(item.get("id"))) != item
            )
        return counts

    @staticmethod
    def _arr_item_root_destination(
        original: dict,
        target: dict,
        root_mappings: list[tuple[str, str]],
    ) -> str | None:
        old_path = str(original.get("path") or "").rstrip("/")
        new_path = str(target.get("path") or "").rstrip("/")
        for old_root, new_root in root_mappings:
            old_root = old_root.rstrip("/")
            new_root = new_root.rstrip("/")
            if old_path == old_root:
                suffix = ""
            elif old_path.startswith(f"{old_root}/"):
                suffix = old_path[len(old_root) :]
            else:
                continue
            if new_path == f"{new_root}{suffix}":
                return new_root
        return None

    @staticmethod
    def _update_arr_items(
        snapshot: dict,
        desired: dict,
        progress_callback=None,
    ) -> int:
        host = snapshot["host"]
        token = snapshot["api_key"]
        api_version = snapshot["api_version"]
        item_endpoint = snapshot["item_endpoint"]
        service_key = snapshot["service_key"]
        original_items = {
            str(item.get("id")): item for item in snapshot.get("items") or []
        }
        changed = [
            item
            for item in desired.get("items") or []
            if item.get("id") is not None
            and original_items.get(str(item.get("id")), {}).get("path")
            != item.get("path")
        ]
        if not changed:
            return 0

        original_roots = {
            str(root.get("id")): root for root in snapshot.get("roots") or []
        }
        desired_roots = {
            str(root.get("id")): root for root in desired.get("roots") or []
        }
        root_mappings = [
            (
                str(original_roots[root_id].get("path") or ""),
                str(target.get("path") or ""),
            )
            for root_id, target in desired_roots.items()
            if root_id in original_roots
            and original_roots[root_id].get("path") != target.get("path")
        ]
        grouped: dict[str, list[dict]] = {}
        exact_updates = []
        for target in changed:
            destination = InfiniDyskMigrationManager._arr_item_root_destination(
                original_items.get(str(target.get("id"))) or {},
                target,
                root_mappings,
            )
            if destination:
                grouped.setdefault(destination, []).append(target)
            else:
                exact_updates.append(target)

        editor_endpoint, id_field = ARR_EDITOR_API[service_key]
        completed = 0

        def report(mode: str) -> None:
            if callable(progress_callback):
                progress_callback(completed, len(changed), mode)

        def apply_bulk(root_path: str, batch: list[dict]) -> None:
            nonlocal completed
            payload = {
                id_field: [int(item["id"]) for item in batch],
                "rootFolderPath": root_path,
                "moveFiles": False,
            }
            try:
                _migration_arr_req(
                    _arr_url(host, api_version, editor_endpoint),
                    token,
                    "PUT",
                    payload,
                )
                completed += len(batch)
                report("bulk")
            except urllib.error.HTTPError as error:
                if error.code not in {400, 404, 405, 409, 422}:
                    raise
                if (
                    error.code in {409, 422}
                    and len(batch) > ARR_EDITOR_SPLIT_MIN_BATCH_SIZE
                ):
                    # Validation/conflict failures can be caused by one record in
                    # an otherwise valid catalog-sized batch. Bisect only those
                    # failures so one bad item does not degrade all 500 records to
                    # slow individual writes. A small failing leaf still uses the
                    # exact item endpoint and final validation remains authoritative.
                    midpoint = len(batch) // 2
                    apply_bulk(root_path, batch[:midpoint])
                    apply_bulk(root_path, batch[midpoint:])
                    return
                # HTTP 400/404/405 normally means this Arr build has no compatible
                # bulk editor. Avoid recursively probing every item in that case.
                exact_updates.extend(batch)

        for root_path, targets in grouped.items():
            for offset in range(0, len(targets), ARR_EDITOR_BATCH_SIZE):
                batch = targets[offset : offset + ARR_EDITOR_BATCH_SIZE]
                apply_bulk(root_path, batch)

        for target in exact_updates:
            item_id = str(target.get("id") or "")
            _migration_arr_req(
                _arr_url(
                    host,
                    api_version,
                    f"{item_endpoint}/{item_id}?moveFiles=false",
                ),
                token,
                "PUT",
                target,
            )
            completed += 1
            if (
                completed == len(changed)
                or completed % ARR_FALLBACK_PROGRESS_INTERVAL == 0
            ):
                report("individual")
        return completed

    @staticmethod
    def _apply_prowlarr_snapshot(snapshot: dict, desired: dict) -> dict:
        host = snapshot["host"]
        token = snapshot["api_key"]
        original_applications = {
            str(item.get("id")): item
            for item in snapshot.get("applications") or []
            if item.get("id") is not None
        }
        changed_applications = 0
        for target in desired.get("applications") or []:
            application_id = str(target.get("id") or "")
            if (
                not application_id
                or original_applications.get(application_id) == target
            ):
                continue
            _migration_prowlarr_req(
                f"{host}/api/v1/applications/{application_id}",
                token,
                "PUT",
                target,
            )
            changed_applications += 1

        original_tags = {
            str(item.get("id")): item
            for item in snapshot.get("tags") or []
            if item.get("id") is not None
        }
        changed_tags = 0
        for target in desired.get("tags") or []:
            tag_id = str(target.get("id") or "")
            if not tag_id or original_tags.get(tag_id) == target:
                continue
            _migration_prowlarr_req(f"{host}/api/v1/tag/{tag_id}", token, "PUT", target)
            changed_tags += 1
        return {"applications": changed_applications, "tags": changed_tags}

    @staticmethod
    def _validate_prowlarr_snapshot(snapshot: dict, desired: dict) -> None:
        host = snapshot["host"]
        token = snapshot["api_key"]
        current_applications = {
            str(item.get("id")): item
            for item in (
                _migration_prowlarr_req(f"{host}/api/v1/applications", token, "GET")
                or []
            )
        }
        for target in desired.get("applications") or []:
            application_id = str(target.get("id") or "")
            current = current_applications.get(application_id) or {}
            if application_id and current.get("name") != target.get("name"):
                raise RuntimeError(
                    f"Prowlarr application name did not persist for application {application_id}"
                )
            desired_fields = {
                str(field.get("name")): field.get("value")
                for field in target.get("fields") or []
            }
            current_fields = {
                str(field.get("name")): field.get("value")
                for field in current.get("fields") or []
            }
            for field_name, value in desired_fields.items():
                if current_fields.get(field_name) != value:
                    raise RuntimeError(
                        f"Prowlarr application field {field_name} did not persist for application {application_id}"
                    )

        current_tags = {
            str(item.get("id")): item
            for item in (
                _migration_prowlarr_req(f"{host}/api/v1/tag", token, "GET") or []
            )
        }
        for target in desired.get("tags") or []:
            tag_id = str(target.get("id") or "")
            if tag_id and (current_tags.get(tag_id) or {}).get("label") != target.get(
                "label"
            ):
                raise RuntimeError(f"Prowlarr tag did not persist for tag {tag_id}")

    @staticmethod
    def _apply_arr_snapshot(
        snapshot: dict,
        desired: dict,
        progress_callback=None,
    ) -> dict:
        host = snapshot["host"]
        token = snapshot["api_key"]
        api_version = snapshot["api_version"]
        changed_roots = 0
        changed_items = 0
        changed_clients = 0
        changed_tags = 0

        original_roots = {
            str(root.get("id")): root for root in snapshot.get("roots") or []
        }
        desired_roots = {
            str(root.get("id")): root for root in desired.get("roots") or []
        }
        current_root_paths = {
            str(root.get("path") or "")
            for root in (
                _migration_arr_req(
                    _arr_url(host, api_version, "rootfolder"), token, "GET"
                )
                or []
            )
        }
        for root_id, target in desired_roots.items():
            original = original_roots.get(root_id) or {}
            old_path = str(original.get("path") or "")
            new_path = str(target.get("path") or "")
            if not old_path or old_path == new_path:
                continue
            if new_path in current_root_paths:
                continue
            payload = {"path": new_path}
            if api_version == "v1":
                payload = (
                    _get_lidarr_rootfolder_payload(
                        host,
                        token,
                        new_path,
                        timeout=ARR_INVENTORY_TIMEOUT_SECONDS,
                    )
                    or {}
                )
                if not payload:
                    raise RuntimeError(
                        f"Could not construct the Lidarr root-folder payload for {new_path}"
                    )
            _migration_arr_req(
                _arr_url(host, api_version, "rootfolder"),
                token,
                "POST",
                payload,
            )
            changed_roots += 1
            current_root_paths.add(new_path)

        changed_items = InfiniDyskMigrationManager._update_arr_items(
            snapshot,
            desired,
            progress_callback=progress_callback,
        )

        original_clients = {
            str(item.get("id")): item for item in snapshot.get("clients") or []
        }
        for target in desired.get("clients") or []:
            client_id = str(target.get("id") or "")
            original = original_clients.get(client_id) or {}
            if not client_id or original == target:
                continue
            _migration_arr_req(
                _arr_url(host, api_version, f"downloadclient/{client_id}"),
                token,
                "PUT",
                target,
            )
            changed_clients += 1

        original_tags = {
            str(item.get("id")): item for item in snapshot.get("tags") or []
        }
        for target in desired.get("tags") or []:
            tag_id = str(target.get("id") or "")
            original = original_tags.get(tag_id) or {}
            if not tag_id or original == target:
                continue
            _migration_arr_req(
                _arr_url(host, api_version, f"tag/{tag_id}"),
                token,
                "PUT",
                target,
            )
            changed_tags += 1

        target_root_paths = {
            str(root.get("path") or "")
            for root in desired_roots.values()
            if str(root.get("path") or "")
        }
        obsolete_root_paths = {
            str(original.get("path") or "")
            for root_id, original in original_roots.items()
            if str(original.get("path") or "")
            and str(original.get("path") or "")
            != str((desired_roots.get(root_id) or {}).get("path") or "")
            and str(original.get("path") or "") not in target_root_paths
        }
        current_roots = (
            _migration_arr_req(_arr_url(host, api_version, "rootfolder"), token, "GET")
            or []
        )
        for current in current_roots:
            current_path = str(current.get("path") or "")
            if current_path not in obsolete_root_paths or not current.get("id"):
                continue
            try:
                _migration_arr_req(
                    _arr_url(host, api_version, f"rootfolder/{current.get('id')}"),
                    token,
                    "DELETE",
                )
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
        return {
            "roots": changed_roots,
            "items": changed_items,
            "clients": changed_clients,
            "tags": changed_tags,
        }

    @staticmethod
    def _apply_media_snapshot(snapshot: dict, desired: dict, logger) -> list[dict]:
        adapter = build_adapter(
            snapshot["service_key"], snapshot["process_name"], logger
        )
        InfiniDyskMigrationManager._verify_media_identity(
            adapter, snapshot.get("identity") or {}
        )
        return adapter.replace_library_paths(desired.get("libraries") or [])

    @staticmethod
    def _verify_media_identity(adapter, expected: dict) -> None:
        expected_identifier = str(expected.get("machine_identifier") or "")
        if not expected_identifier:
            return
        current = adapter.server_identity()
        current_identifier = str(current.get("machine_identifier") or "")
        if not current_identifier or not secrets.compare_digest(
            expected_identifier, current_identifier
        ):
            raise RuntimeError(
                "The connected Plex server identity changed after preflight; refusing to update library paths"
            )

    @staticmethod
    def _retry(callback, attempts: int = 30, delay: float = 2.0):
        last_error = None
        for attempt in range(attempts):
            try:
                return callback()
            except Exception as error:
                last_error = error
                if attempt + 1 < attempts:
                    time.sleep(delay)
        raise last_error or RuntimeError("Operation did not become ready")

    @staticmethod
    def _validate_arr_snapshot(snapshot: dict, desired: dict) -> None:
        host = snapshot["host"]
        token = snapshot["api_key"]
        api_version = snapshot["api_version"]
        item_endpoint = snapshot["item_endpoint"]
        current_roots = (
            _migration_arr_req(_arr_url(host, api_version, "rootfolder"), token, "GET")
            or []
        )
        root_paths = {str(root.get("path") or "") for root in current_roots}
        for root in desired.get("roots") or []:
            desired_path = str(root.get("path") or "")
            if desired_path and desired_path not in root_paths:
                raise RuntimeError(f"Arr root path did not persist: {desired_path}")
        desired_root_paths = {
            str(root.get("path") or "")
            for root in desired.get("roots") or []
            if str(root.get("path") or "")
        }
        original_roots = {
            str(root.get("id")): root for root in snapshot.get("roots") or []
        }
        desired_roots = {
            str(root.get("id")): root for root in desired.get("roots") or []
        }
        obsolete_root_paths = {
            str(original.get("path") or "")
            for root_id, original in original_roots.items()
            if str(original.get("path") or "")
            and str(original.get("path") or "")
            != str((desired_roots.get(root_id) or {}).get("path") or "")
            and str(original.get("path") or "") not in desired_root_paths
        }
        stale_roots = sorted(obsolete_root_paths & root_paths)
        if stale_roots:
            raise RuntimeError(
                "Arr obsolete root paths remain after migration: "
                + ", ".join(stale_roots[:3])
            )
        current_items = {
            str(item.get("id")): item
            for item in (
                _migration_arr_req(
                    _arr_url(host, api_version, item_endpoint), token, "GET"
                )
                or []
            )
        }
        for item in desired.get("items") or []:
            item_id = str(item.get("id") or "")
            if item_id and (current_items.get(item_id) or {}).get("path") != item.get(
                "path"
            ):
                raise RuntimeError(f"Arr item path did not persist for item {item_id}")
        current_clients = {
            str(item.get("id")): item
            for item in (
                _migration_arr_req(
                    _arr_url(host, api_version, "downloadclient"), token, "GET"
                )
                or []
            )
        }
        for client in desired.get("clients") or []:
            client_id = str(client.get("id") or "")
            current = current_clients.get(client_id) or {}
            if client_id and client.get("name") != current.get("name"):
                raise RuntimeError(
                    f"Arr download-client name did not persist for client {client_id}"
                )
            desired_fields = {
                str(field.get("name")): field.get("value")
                for field in client.get("fields") or []
            }
            current_fields = {
                str(field.get("name")): field.get("value")
                for field in current.get("fields") or []
            }
            for field_name, value in desired_fields.items():
                if current_fields.get(field_name) != value:
                    raise RuntimeError(
                        f"Arr download-client field {field_name} did not persist for client {client_id}"
                    )
        current_tags = {
            str(item.get("id")): item
            for item in (
                _migration_arr_req(_arr_url(host, api_version, "tag"), token, "GET")
                or []
            )
        }
        for tag in desired.get("tags") or []:
            tag_id = str(tag.get("id") or "")
            if tag_id and current_tags.get(tag_id) != tag:
                raise RuntimeError(f"Arr tag did not persist for tag {tag_id}")

    @staticmethod
    def _validate_media_snapshot(snapshot: dict, desired: dict, logger) -> None:
        adapter = build_adapter(
            snapshot["service_key"], desired["process_name"], logger
        )
        InfiniDyskMigrationManager._verify_media_identity(
            adapter, snapshot.get("identity") or desired.get("identity") or {}
        )
        current = {
            str(item.get("id") or item.get("name")): item
            for item in adapter.library_paths()
        }
        for library in desired.get("libraries") or []:
            key = str(library.get("id") or library.get("name"))
            if (current.get(key) or {}).get("paths") != library.get("paths"):
                raise RuntimeError(
                    f"{desired['process_name']} library paths did not persist for {library.get('name') or key}"
                )

    @staticmethod
    def _migrate_infinidysk_database(config_dir: str) -> int:
        database = Path(config_dir) / "db.sqlite"
        if not database.is_file():
            return 0
        changed = 0
        with sqlite3.connect(database) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ConfigItems'"
            ).fetchone()
            if not table:
                return 0
            rows = connection.execute(
                "SELECT ConfigName, ConfigValue FROM ConfigItems"
            ).fetchall()
            for name, value in rows:
                if not isinstance(value, str):
                    continue
                rewritten = InfiniDyskMigrationManager._rewrite_external_value(value)
                if rewritten == value:
                    continue
                connection.execute(
                    "UPDATE ConfigItems SET ConfigValue=? WHERE ConfigName=?",
                    (rewritten, name),
                )
                changed += 1
            connection.commit()
        return changed

    @staticmethod
    def _remove_compatible_destination(action: dict) -> None:
        destination = Path(action["destination"])
        if not os.path.lexists(destination):
            return
        if destination.is_symlink():
            if (
                action.get("source") == "/nzbdav"
                and os.path.realpath(destination) == "/data/infinidysk"
            ):
                destination.unlink()
                return
            raise RuntimeError(
                f"Migration destination {destination} changed after preflight"
            )
        if destination.is_dir() and not any(destination.iterdir()):
            destination.rmdir()
            return
        raise RuntimeError(f"Migration destination {destination} is no longer empty")

    @staticmethod
    def _move_namespace_paths(actions: list[dict]) -> list[dict]:
        moved = []
        try:
            for original in actions:
                action = copy.deepcopy(original)
                source = Path(action["source"])
                destination = Path(action["destination"])
                if not os.path.lexists(source):
                    continue
                if os.path.ismount(source):
                    result = subprocess.run(
                        ["umount", str(source)], capture_output=True, text=True
                    )
                    if result.returncode != 0 or os.path.ismount(source):
                        raise RuntimeError(
                            f"Could not unmount {source}: {result.stderr.strip() or 'mount remains active'}"
                        )
                action["link_target"] = (
                    os.readlink(source) if source.is_symlink() else None
                )
                InfiniDyskMigrationManager._remove_compatible_destination(action)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
                if destination.is_symlink() and action.get("link_target"):
                    rewritten_target = _replace_namespace_text(action["link_target"])
                    if rewritten_target != action["link_target"]:
                        destination.unlink()
                        os.symlink(rewritten_target, destination)
                moved.append(action)
        except Exception as error:
            rollback_errors = InfiniDyskMigrationManager._rollback_namespace_paths(
                moved
            )
            raise NamespaceMoveError(str(error), rollback_errors) from error
        return moved

    @staticmethod
    def _rollback_namespace_paths(actions: list[dict]) -> list[str]:
        errors = []
        for action in reversed(actions):
            source = Path(action["source"])
            destination = Path(action["destination"])
            try:
                if not os.path.lexists(destination):
                    continue
                if os.path.lexists(source):
                    if (
                        source.is_dir()
                        and not source.is_symlink()
                        and not any(source.iterdir())
                    ):
                        source.rmdir()
                    else:
                        raise RuntimeError(f"rollback source {source} already exists")
                os.replace(destination, source)
                if source.is_symlink() and action.get("link_target") is not None:
                    source.unlink()
                    os.symlink(action["link_target"], source)
                if action.get("destination_state") == "directory":
                    destination.mkdir(parents=True, exist_ok=True)
            except Exception as error:
                errors.append(f"{destination} -> {source}: {error}")
        return errors

    @staticmethod
    def _affected_processes(
        config: dict, preflight: dict, rename_attached_services: bool = True
    ) -> list[str]:
        names = []
        service = InfiniDyskMigrationManager._service_config(config)
        if service.get("process_name"):
            names.append(str(service["process_name"]))
        names.extend(
            item["process_name"]
            for item in InfiniDyskMigrationManager._linked_instances(
                config, INFINIDYSK_CORE_CONSUMER_KEYS
            )
        )
        names.extend(item["process_name"] for item in preflight.get("arr") or [])
        names.extend(item["process_name"] for item in preflight.get("prowlarr") or [])
        names.extend(item["process_name"] for item in preflight.get("media") or [])
        # Renaming a process or instance changes the process-manager lookup key even
        # when that service is not linked to InfiniDysk. Include every rename target
        # so a running process cannot be orphaned under its legacy name.
        if rename_attached_services:
            names.extend(
                item["process_name"]
                for item in InfiniDyskMigrationManager._attached_services(config)
                if item.get("process_name")
            )
        return list(dict.fromkeys(names))

    @staticmethod
    def _stop_processes(process_handler, process_names: list[str]) -> list[str]:
        running = [
            name
            for name in process_names
            if InfiniDyskMigrationManager._process_running(process_handler, name)
        ]
        stopped = []
        for process_name in reversed(running):
            process_handler.stop_process(process_name)
            if InfiniDyskMigrationManager._process_running(
                process_handler, process_name
            ):
                restart_errors = []
                for stopped_name in reversed(stopped):
                    try:
                        result, error = process_handler.start_process(stopped_name)
                        if not result:
                            restart_errors.append(
                                error or f"{stopped_name} failed to restart"
                            )
                    except Exception as error:
                        restart_errors.append(f"{stopped_name}: {error}")
                suffix = (
                    f"; already-stopped services also failed to restart: {', '.join(restart_errors)}"
                    if restart_errors
                    else ""
                )
                raise RuntimeError(f"{process_name} did not stop cleanly{suffix}")
            stopped.append(process_name)
        return running

    @staticmethod
    def _quiesce_producer_processes(config: dict, preflight: dict) -> list[str]:
        names = [
            item["process_name"]
            for item in InfiniDyskMigrationManager._linked_instances(
                config, {"neutarr", "profilarr", "seerr"}
            )
        ]
        names.extend(
            str(item.get("process_name") or "")
            for item in preflight.get("prowlarr") or []
        )
        return list(dict.fromkeys(name for name in names if name))

    def _quiesce_for_cutover(
        self,
        config: dict,
        preflight: dict,
        old_process_names: list[str],
        process_handler,
        logger,
        progress,
        running: list[str],
        scan_guards: list[dict],
        job_id: str | None = None,
    ) -> dict:
        """Drain transient activity and latch each service stopped when safe."""
        producers = self._quiesce_producer_processes(config, preflight)
        if producers:
            progress(
                "quiescing",
                "Stopping linked request, search, and indexer producers so Arr queues cannot refill.",
                20,
            )
            running.extend(self._stop_processes(process_handler, producers))

        arr_preflight = {
            str(item.get("process_name") or ""): item
            for item in preflight.get("arr") or []
        }
        arr_targets = [
            target
            for target in self._enabled_instances(config, set(ARR_SERVICE_API))
            if target["process_name"] in arr_preflight
        ]
        media_targets = [
            {
                "service_key": item["service_key"],
                "process_name": item["process_name"],
                "external_api_only": bool(item.get("external_api_only")),
            }
            for item in preflight.get("media") or []
        ]
        captured_arr: dict[str, dict] = {}
        captured_media: dict[str, dict] = {}
        deadline = time.monotonic() + QUIESCE_TIMEOUT_SECONDS
        last_message = None
        last_reported_at = 0.0

        while True:
            pending = []
            arr_queue_pending = False
            managed_media_pending = False
            external_media_pending = False
            active_reads_pending = False
            for target in arr_targets:
                process_name = target["process_name"]
                if process_name in captured_arr:
                    continue
                original = arr_preflight.get(process_name)
                if not original:
                    raise InfiniDyskMigrationError(
                        f"{process_name} has no saved Arr inventory for automatic quiescence."
                    )
                try:
                    queue = (
                        _migration_arr_req(
                            _arr_url(
                                original["host"],
                                original["api_version"],
                                "queue?page=1&pageSize=1000&includeUnknownMovieItems=true&includeUnknownSeriesItems=true",
                            ),
                            original["api_key"],
                            "GET",
                        )
                        or {}
                    )
                    records = (
                        queue.get("records") if isinstance(queue, dict) else queue
                    ) or []
                    queue_count = (
                        int(queue.get("totalRecords") or len(records))
                        if isinstance(queue, dict)
                        else len(records)
                    )
                except Exception as error:
                    raise InfiniDyskMigrationError(
                        f"{process_name} queue could not be checked while quiescing: "
                        f"{_safe_error_detail(error)}."
                    ) from error
                if queue_count:
                    arr_queue_pending = True
                    pending.append(f"{process_name}: {queue_count} queued")
                    continue
                snapshot, blockers = self._arr_target_snapshot(target, process_handler)
                if blockers or snapshot is None:
                    raise InfiniDyskMigrationError(
                        blockers[0]
                        if blockers
                        else f"{process_name} could not be inventoried while quiescing."
                    )
                if int(snapshot.get("queue_count") or 0):
                    arr_queue_pending = True
                    pending.append(
                        f"{process_name}: queue changed while capturing its final inventory"
                    )
                    continue
                final_queue = (
                    _migration_arr_req(
                        _arr_url(
                            snapshot["host"],
                            snapshot["api_version"],
                            "queue?page=1&pageSize=1000&includeUnknownMovieItems=true&includeUnknownSeriesItems=true",
                        ),
                        snapshot["api_key"],
                        "GET",
                    )
                    or {}
                )
                final_records = (
                    final_queue.get("records")
                    if isinstance(final_queue, dict)
                    else final_queue
                ) or []
                final_count = (
                    int(final_queue.get("totalRecords") or len(final_records))
                    if isinstance(final_queue, dict)
                    else len(final_records)
                )
                if final_count:
                    arr_queue_pending = True
                    pending.append(
                        f"{process_name}: {final_count} queued after final inventory"
                    )
                    continue
                running.extend(self._stop_processes(process_handler, [process_name]))
                captured_arr[process_name] = snapshot

            stop_active_playback = self._playback_stop_requested(job_id)
            if stop_active_playback:
                requested_servers = self._playback_override_media_servers(job_id)
                progress(
                    "quiescing",
                    "Operator approved playback interruption; stopping active media server"
                    f"{'s' if len(requested_servers) != 1 else ''}: "
                    f"{', '.join(requested_servers) or 'affected media servers'}.",
                    25,
                )
            busy_media_servers = []
            for target in media_targets:
                process_name = target["process_name"]
                adapter = build_adapter(target["service_key"], process_name, logger)
                external_api_only = bool(target.get("external_api_only"))
                if process_name in captured_media:
                    if not external_api_only:
                        continue
                    activity = adapter.activity()
                    state = str(activity.get("state") or "unknown")
                    if state == "unknown":
                        raise InfiniDyskMigrationError(
                            f"{process_name} activity could not be verified while quiescing: "
                            f"{activity.get('reason') or 'unknown error'}."
                        )
                    if state != "idle":
                        external_media_pending = True
                        sessions = activity.get("active_sessions")
                        suffix = (
                            f" ({sessions} active session(s))"
                            if sessions is not None
                            else ""
                        )
                        pending.append(f"{process_name}: {state}{suffix}")
                    continue
                activity = adapter.activity()
                state = str(activity.get("state") or "unknown")
                if state == "unknown":
                    raise InfiniDyskMigrationError(
                        f"{process_name} activity could not be verified while quiescing: "
                        f"{activity.get('reason') or 'unknown error'}."
                    )
                if state != "idle":
                    if stop_active_playback and not external_api_only:
                        libraries = adapter.library_paths()
                        guard = {
                            "service_key": target["service_key"],
                            "process_name": process_name,
                            "snapshot": adapter.enter_scan_guard(),
                        }
                        scan_guards.append(guard)
                        running.extend(
                            self._stop_processes(process_handler, [process_name])
                        )
                        captured_media[process_name] = {
                            "service_key": target["service_key"],
                            "process_name": process_name,
                            "libraries": libraries,
                            "activity": activity,
                            "playback_interrupted": True,
                            "external_api_only": False,
                        }
                        continue
                    sessions = activity.get("active_sessions")
                    suffix = (
                        f" ({sessions} active session(s))"
                        if sessions is not None
                        else ""
                    )
                    pending.append(f"{process_name}: {state}{suffix}")
                    if external_api_only:
                        external_media_pending = True
                    else:
                        managed_media_pending = True
                        busy_media_servers.append(process_name)
                    continue
                libraries = adapter.library_paths()
                guard = {
                    "service_key": target["service_key"],
                    "process_name": process_name,
                    "snapshot": adapter.enter_scan_guard(),
                }
                scan_guards.append(guard)
                if not external_api_only:
                    running.extend(
                        self._stop_processes(process_handler, [process_name])
                    )
                captured_media[process_name] = {
                    "service_key": target["service_key"],
                    "process_name": process_name,
                    "libraries": libraries,
                    "activity": activity,
                    "identity": next(
                        (
                            item.get("identity") or {}
                            for item in preflight.get("media") or []
                            if item.get("process_name") == process_name
                        ),
                        {},
                    ),
                    "external_api_only": external_api_only,
                }

            if stop_active_playback:
                self._finish_playback_stop_request(job_id)
            else:
                self._set_playback_override_availability(job_id, busy_media_servers)

            active_reads, active_error = self._infinidysk_active_reads(
                config, process_handler
            )
            if active_error:
                raise InfiniDyskMigrationError(active_error)
            if active_reads:
                active_reads_pending = True
                pending.append(f"InfiniDysk: {active_reads} active read(s)")

            if (
                len(captured_arr) == len(arr_targets)
                and len(captured_media) == len(media_targets)
                and not pending
            ):
                break

            now = time.monotonic()
            summary = "; ".join(pending) or "waiting for services to settle"
            if summary != last_message or now - last_reported_at >= 30:
                instructions = []
                if arr_queue_pending:
                    instructions.append(
                        "Resolve failed or held Arr items while producers remain stopped."
                    )
                if managed_media_pending:
                    instructions.append("Wait for managed media playback to end.")
                if external_media_pending:
                    instructions.append(
                        "Stop external Plex playback and pause Autoscan or other external scan producers; DUMB cannot stop that process."
                    )
                if active_reads_pending:
                    instructions.append("Waiting for InfiniDysk active reads to close.")
                progress(
                    "quiescing",
                    "Waiting for safe cutover conditions. "
                    f"{' '.join(instructions)} Remaining activity: {summary}.",
                    24,
                )
                last_message = summary
                last_reported_at = now
            if now >= deadline:
                raise InfiniDyskMigrationError(
                    "Automatic quiescence timed out after one hour before any namespace paths were moved. "
                    f"Remaining activity: {summary}."
                )
            time.sleep(min(QUIESCE_POLL_SECONDS, max(0.0, deadline - now)))

        progress(
            "quiescing",
            "Queues and playback are drained; holding managed services stopped and external media scan guards active through the cutover.",
            27,
        )
        running.extend(self._stop_processes(process_handler, old_process_names))
        self._finish_playback_stop_request(job_id)
        running_set = set(running)
        running[:] = [name for name in old_process_names if name in running_set]

        refreshed = copy.deepcopy(preflight)
        refreshed["arr"] = [
            captured_arr[target["process_name"]] for target in arr_targets
        ]
        refreshed["media"] = [
            captured_media[target["process_name"]] for target in media_targets
        ]
        refreshed["active_reads"] = 0
        refreshed["pending_conditions"] = []
        return refreshed

    @staticmethod
    def _start_processes(
        process_handler,
        process_names: list[str],
        *,
        force_setup: bool = True,
        defer_provider_integrations: bool = False,
        continue_on_error: bool = False,
    ) -> list[str]:
        errors = []
        for process_name in process_names:
            tracker_lock = getattr(process_handler, "setup_tracker_lock", None)
            tracker = getattr(process_handler, "setup_tracker", None)
            if tracker_lock is not None and tracker is not None:
                with tracker_lock:
                    if force_setup:
                        tracker.discard(process_name)
                    else:
                        # Rollback restores the captured runtime/configuration. Avoid
                        # rerunning integration setup against dependencies that are
                        # intentionally still stopped during recovery.
                        tracker.add(process_name)
            if defer_provider_integrations and process_name == "InfiniDysk":
                with defer_nzbdav_runtime_integrations():
                    result, error = process_handler.start_process(process_name)
            else:
                result, error = process_handler.start_process(process_name)
            if not result:
                detail = error or f"{process_name} failed to start"
                if not continue_on_error:
                    raise RuntimeError(detail)
                errors.append(f"{process_name}: {detail}")
        return errors

    @staticmethod
    def _start_prowlarr_for_migration(
        process_handler, process_names: list[str]
    ) -> None:
        """Start captured Prowlarr runtimes without preemptive integration setup."""

        InfiniDyskMigrationManager._start_processes(
            process_handler,
            process_names,
            force_setup=False,
        )

    @staticmethod
    def _renamed_processes(
        old_process_names: list[str],
        original_service_name: str,
        rename_attached_services: bool,
    ) -> dict[str, str]:
        renamed = {original_service_name: "InfiniDysk"}
        if rename_attached_services:
            renamed.update(
                {
                    name: _replace_display_name(name)
                    for name in old_process_names
                    if name != original_service_name and _contains_legacy_name(name)
                }
            )
        return renamed

    @staticmethod
    def _rename_attached_instance_keys(config: dict) -> None:
        for service_key in ATTACHED_SERVICE_KEYS:
            instances = (config.get(service_key) or {}).get("instances") or {}
            for old_name in list(instances):
                instance = instances[old_name]
                if isinstance(instance, dict) and _contains_legacy_name(
                    instance.get("process_name")
                ):
                    instance["process_name"] = _replace_display_name(
                        str(instance["process_name"])
                    )
                if not _contains_legacy_name(old_name):
                    continue
                new_name = _replace_display_name(old_name)
                if new_name != old_name and new_name in instances:
                    raise InfiniDyskMigrationError(
                        f"Cannot rename {old_name} because {new_name} already exists."
                    )
                instances[new_name] = instances.pop(old_name)

    def apply_full_namespace(
        self,
        preflight_token: str,
        rename_attached_services: bool,
        process_handler,
        logger,
        progress_callback=None,
        job_id: str | None = None,
    ) -> dict:
        with self._lock:

            def progress(
                stage: str,
                message: str,
                percent: int,
                status="running",
                detail: dict | None = None,
            ):
                if callable(progress_callback):
                    progress_callback(stage, message, percent, status, detail)

            progress("validating", "Validating the saved preflight.", 3)
            state = self._load_state()
            preflight = self._load_preflight()
            now = int(time.time())
            if not preflight_token or not secrets.compare_digest(
                str(preflight.get("token") or ""), str(preflight_token)
            ):
                raise InfiniDyskMigrationError(
                    "Run the namespace preflight again before applying the migration."
                )
            if int(preflight.get("expires_at") or 0) < now:
                raise InfiniDyskMigrationError(
                    "The namespace preflight expired. Run it again before applying."
                )
            if preflight.get("blockers"):
                raise InfiniDyskMigrationError(
                    "The namespace preflight still has blockers. Resolve them and run it again."
                )
            config = CONFIG_MANAGER.config
            if preflight.get("config_fingerprint") != _config_fingerprint(config):
                raise InfiniDyskMigrationError(
                    "DUMB configuration changed after preflight. Run the preflight again."
                )
            if not self._legacy_paths(config):
                if state.get("selected_mode") == "full_namespace":
                    return {
                        "status": "completed",
                        "selected_mode": "full_namespace",
                        "restart_required": False,
                        "retained_namespace": False,
                        "message": "The complete InfiniDysk namespace migration is already complete.",
                    }
                raise InfiniDyskMigrationError(
                    "No legacy NzbDAV namespace paths were found to migrate."
                )

            progress("preflight", "Repeating live safety checks.", 8)
            refreshed = self.preflight(process_handler, logger, now=now)
            if not refreshed.get("ready"):
                raise InfiniDyskMigrationError(
                    "Runtime conditions changed after preflight. Resolve the reported blockers and run it again."
                )
            preflight = self._load_preflight() or preflight

            progress("backup", "Creating private rollback snapshots.", 14)
            backup_config = copy.deepcopy(config)
            legacy_identity = CONFIG_MANAGER.uses_legacy_infinidysk_identity()
            config_backup_path, backup_bundle = self._create_namespace_backup(
                preflight, now, progress_callback=progress
            )
            old_process_names = self._affected_processes(
                config, preflight, rename_attached_services
            )
            producer_process_names = set(
                self._quiesce_producer_processes(config, preflight)
            )
            original_service_name = str(
                self._service_config(config).get("process_name") or "InfiniDysk"
            )
            renamed_processes = self._renamed_processes(
                old_process_names,
                original_service_name,
                rename_attached_services,
            )
            running = []
            moved = []
            scan_guards = []
            arr_changes = []
            prowlarr_changes = []
            media_changes = []
            try:
                preflight = self._quiesce_for_cutover(
                    config,
                    preflight,
                    old_process_names,
                    process_handler,
                    logger,
                    progress,
                    running,
                    scan_guards,
                    job_id=job_id,
                )
                self._write_private_json(
                    backup_bundle / "scan-guards.json", {"guards": scan_guards}
                )
                self._write_private_json(backup_bundle / "preflight.json", preflight)
                progress("filesystem", "Moving managed namespace paths atomically.", 38)
                moved = self._move_namespace_paths(preflight.get("filesystem") or [])

                progress(
                    "configuration", "Saving canonical InfiniDysk paths and names.", 48
                )
                preserved_zurg = copy.deepcopy(config.get("zurg"))
                rewritten = _rewrite_config_namespace(copy.deepcopy(config))
                if preserved_zurg is not None:
                    rewritten["zurg"] = preserved_zurg
                service = self._service_config(rewritten)
                service["repo_owner"] = "infinidysk"
                service["repo_name"] = "infinidysk"
                service["process_name"] = "InfiniDysk"
                if rename_attached_services:
                    self._rename_attached_instance_keys(rewritten)
                renamed_process_map = {
                    old: new for old, new in renamed_processes.items() if old != new
                }
                changed_references = self._update_process_references(
                    rewritten, renamed_process_map
                )
                CONFIG_MANAGER.config = rewritten
                CONFIG_MANAGER.adopt_infinidysk_identity()
                CONFIG_MANAGER.save_config()

                progress("symlinks", "Rewriting symlink targets.", 56)
                symlink_changes = 0
                symlink_roots = [
                    str(Path(str(action["destination"])))
                    for action in moved
                    if "symlink"
                    in Path(str(action.get("destination") or "")).name.lower()
                    and "infinidysk"
                    in Path(str(action.get("destination") or "")).name.lower()
                    and Path(str(action["destination"])).is_dir()
                ]
                if symlink_roots:
                    report = repair_symlinks(
                        symlink_roots,
                        [
                            {
                                "from_prefix": "/mnt/debrid/nzbdav",
                                "to_prefix": "/mnt/debrid/infinidysk",
                            }
                        ],
                        dry_run=False,
                        include_broken=True,
                        backup_path=str(backup_bundle / "symlink-rewrites.json"),
                    )
                    if report.get("errors"):
                        raise RuntimeError("Symlink target rewriting reported errors")
                    symlink_changes = int(report.get("changed") or 0)
                    manifest_path = backup_bundle / "symlink-rewrites.json"
                    if manifest_path.exists():
                        os.chmod(manifest_path, 0o600)

                progress("database", "Updating InfiniDysk configuration records.", 62)
                database_changes = self._migrate_infinidysk_database(
                    str(service.get("config_dir") or "/infinidysk")
                )
                new_running = [
                    renamed_processes.get(name, name)
                    for name in running
                    if name not in producer_process_names
                ]
                held_producers = [
                    renamed_processes.get(name, name)
                    for name in running
                    if name in producer_process_names
                ]
                prowlarr_names = {
                    renamed_processes.get(
                        str(item.get("process_name") or ""),
                        str(item.get("process_name") or ""),
                    )
                    for item in preflight.get("prowlarr") or []
                }
                held_prowlarr = [
                    name for name in held_producers if name in prowlarr_names
                ]
                held_producers = [
                    name for name in held_producers if name not in prowlarr_names
                ]
                progress(
                    "starting",
                    "Restarting the provider, Arrs, and media servers while request/search producers remain stopped.",
                    68,
                )
                self._start_processes(
                    process_handler,
                    new_running,
                    defer_provider_integrations=True,
                )

                arr_snapshots = preflight.get("arr") or []
                arr_work = []
                for original in arr_snapshots:
                    desired = self._desired_arr_snapshot(original)
                    desired["process_name"] = _replace_display_name(
                        str(desired.get("process_name") or "")
                    )
                    counts = self._arr_change_counts(original, desired)
                    arr_work.append((original, desired, counts))
                arr_total = sum(sum(counts.values()) for _, _, counts in arr_work)
                arr_completed = 0
                for index, (original, desired, counts) in enumerate(arr_work):
                    service_total = sum(counts.values())
                    service_name = str(original.get("process_name") or "Arr")
                    initial_percent = (
                        82
                        if not arr_total
                        else 72 + int((arr_completed / arr_total) * 10)
                    )
                    progress(
                        "arr",
                        f"Preparing {service_name} reference updates (0/{service_total}).",
                        initial_percent,
                        detail={
                            "kind": "arr_references",
                            "process_name": service_name,
                            "phase": "applying",
                            "completed": 0,
                            "total": service_total,
                            "overall_completed": arr_completed,
                            "overall_total": arr_total,
                            "service_index": index + 1,
                            "service_total": len(arr_work),
                        },
                    )
                    self._retry(
                        lambda original=original: _migration_arr_req(
                            _arr_url(
                                original["host"],
                                original["api_version"],
                                "system/status",
                            ),
                            original["api_key"],
                            "GET",
                        )
                    )

                    fixed_updates = counts["roots"]

                    def arr_item_progress(
                        completed_items: int,
                        _total_items: int,
                        mode: str,
                        *,
                        service_completed_base: int = fixed_updates,
                        completed_before_service: int = arr_completed,
                    ) -> None:
                        service_completed = min(
                            service_total,
                            service_completed_base + completed_items,
                        )
                        overall_completed = min(
                            arr_total,
                            completed_before_service + service_completed,
                        )
                        percent = (
                            82
                            if not arr_total
                            else 72 + int((overall_completed / arr_total) * 10)
                        )
                        progress(
                            "arr",
                            f"Updating {service_name} references ({service_completed}/{service_total}).",
                            percent,
                            detail={
                                "kind": "arr_references",
                                "process_name": service_name,
                                "phase": "applying",
                                "mode": mode,
                                "completed": service_completed,
                                "total": service_total,
                                "overall_completed": overall_completed,
                                "overall_total": arr_total,
                                "service_index": index + 1,
                                "service_total": len(arr_work),
                            },
                        )

                    result = self._apply_arr_snapshot(
                        original,
                        desired,
                        progress_callback=arr_item_progress,
                    )
                    progress(
                        "arr",
                        f"Validating {service_name} references ({service_total}/{service_total}).",
                        (
                            82
                            if not arr_total
                            else 72
                            + int(((arr_completed + service_total) / arr_total) * 10)
                        ),
                        detail={
                            "kind": "arr_references",
                            "process_name": service_name,
                            "phase": "validating",
                            "completed": service_total,
                            "total": service_total,
                            "overall_completed": min(
                                arr_total, arr_completed + service_total
                            ),
                            "overall_total": arr_total,
                            "service_index": index + 1,
                            "service_total": len(arr_work),
                        },
                    )
                    self._validate_arr_snapshot(original, desired)
                    arr_completed += service_total
                    arr_changes.append(
                        {"process_name": desired["process_name"], **result}
                    )
                prowlarr_snapshots = preflight.get("prowlarr") or []
                if held_prowlarr:
                    progress(
                        "starting_prowlarr",
                        "Restarting Prowlarr for guarded connection and tag updates.",
                        83,
                    )
                    # The captured Prowlarr records are the migration authority.
                    # Running normal setup here can create canonical applications
                    # or tags before the guarded ID-preserving rename, producing a
                    # duplicate-name HTTP 409. Start the existing runtime without
                    # integration setup, then apply and validate the snapshot below.
                    self._start_prowlarr_for_migration(process_handler, held_prowlarr)
                for index, original in enumerate(prowlarr_snapshots):
                    progress(
                        "prowlarr",
                        f"Updating and verifying {original.get('process_name')} application connections.",
                        83 + int(((index + 1) / max(1, len(prowlarr_snapshots))) * 4),
                    )
                    desired = self._desired_prowlarr_snapshot(original)
                    desired["process_name"] = _replace_display_name(
                        str(desired.get("process_name") or "")
                    )
                    self._retry(
                        lambda original=original: _migration_prowlarr_req(
                            f"{original['host']}/api/v1/system/status",
                            original["api_key"],
                            "GET",
                        )
                    )
                    result = self._apply_prowlarr_snapshot(original, desired)
                    self._validate_prowlarr_snapshot(original, desired)
                    prowlarr_changes.append(
                        {"process_name": desired["process_name"], **result}
                    )
                media_snapshots = preflight.get("media") or []
                for index, original in enumerate(media_snapshots):
                    progress(
                        "media",
                        f"Updating and verifying {original.get('process_name')} libraries.",
                        87 + int(((index + 1) / max(1, len(media_snapshots))) * 6),
                    )
                    desired = self._desired_media_snapshot(original)
                    desired["process_name"] = _replace_display_name(
                        str(desired.get("process_name") or "")
                    )
                    self._retry(
                        lambda original=original, desired=desired: build_adapter(
                            original["service_key"],
                            desired["process_name"],
                            logger,
                        ).library_paths()
                    )
                    changes = self._apply_media_snapshot(original, desired, logger)
                    self._validate_media_snapshot(original, desired, logger)
                    media_changes.append(
                        {
                            "process_name": desired["process_name"],
                            "libraries": len(changes),
                        }
                    )
                progress("scan_guards", "Restoring media-server scan settings.", 95)
                for guard in scan_guards:
                    process_name = _replace_display_name(guard["process_name"])
                    adapter = build_adapter(guard["service_key"], process_name, logger)
                    adapter.restore_scan_guard(guard["snapshot"])

                if held_producers:
                    progress(
                        "starting_producers",
                        "Restarting linked request, search, and indexer producers after reference validation.",
                        97,
                    )
                    self._start_processes(process_handler, held_producers)

                progress("validation", "Running final namespace validation.", 98)
                remaining = self._legacy_paths(CONFIG_MANAGER.config)
                if remaining:
                    raise RuntimeError(
                        "Configuration validation still found legacy namespace paths"
                    )
                legacy_core_references = self._legacy_core_service_references(
                    CONFIG_MANAGER.config
                )
                if legacy_core_references:
                    raise RuntimeError(
                        "Configuration validation still found legacy core-service references"
                    )
                restarted_linked_services = [
                    renamed_processes.get(name, name)
                    for name in running
                    if name
                    in {
                        item.get("process_name")
                        for item in preflight.get("linked_services") or []
                    }
                ]
                state = self._load_state()
                state.update(
                    {
                        "state_version": STATE_VERSION,
                        "status": "completed",
                        "selected_mode": "full_namespace",
                        "rename_attached_services": bool(rename_attached_services),
                        "completed_at": now,
                        "snoozed_until": None,
                        "config_backup_path": str(config_backup_path),
                        "backup_bundle_path": str(backup_bundle),
                    }
                )
                self._save_state(state)
                return {
                    "status": "completed",
                    "selected_mode": "full_namespace",
                    "restart_required": False,
                    "process_name": "InfiniDysk",
                    "retained_namespace": False,
                    "config_backup_path": str(config_backup_path),
                    "backup_bundle_path": str(backup_bundle),
                    "changed_references": changed_references,
                    "symlink_changes": symlink_changes,
                    "database_changes": database_changes,
                    "arr_changes": arr_changes,
                    "prowlarr_changes": prowlarr_changes,
                    "media_changes": media_changes,
                    "restarted_linked_services": restarted_linked_services,
                    "message": "The complete InfiniDysk namespace migration finished successfully. Run normal Arr and media-server library scans to refresh availability.",
                }
            except Exception as error:
                running_set = set(running)
                running = [name for name in old_process_names if name in running_set]
                failure_detail = _safe_error_detail(error)
                logger.error(
                    "InfiniDysk namespace cutover failed before rollback: %s",
                    failure_detail,
                )
                progress(
                    "rollback",
                    f"The cutover failed; restoring captured paths and configuration. Cause: {failure_detail}",
                    50,
                    "rolling_back",
                )
                rollback_errors = list(getattr(error, "rollback_errors", []) or [])
                progress(
                    "rollback_stop",
                    "Stopping partially migrated services.",
                    52,
                    "rolling_back",
                )
                try:
                    new_names = [renamed_processes.get(name, name) for name in running]
                    self._stop_processes(process_handler, new_names)
                except Exception as rollback_error:
                    rollback_errors.append(f"stop: {rollback_error}")
                progress(
                    "rollback_symlinks",
                    "Restoring original symlink targets.",
                    58,
                    "rolling_back",
                )
                try:
                    new_symlink_roots = [
                        str(Path(str(action["destination"])))
                        for action in moved
                        if "symlink"
                        in Path(str(action.get("destination") or "")).name.lower()
                        and "infinidysk"
                        in Path(str(action.get("destination") or "")).name.lower()
                        and Path(str(action["destination"])).is_dir()
                    ]
                    if new_symlink_roots:
                        reverse_report = repair_symlinks(
                            new_symlink_roots,
                            [
                                {
                                    "from_prefix": "/mnt/debrid/infinidysk",
                                    "to_prefix": "/mnt/debrid/nzbdav",
                                }
                            ],
                            dry_run=False,
                            include_broken=True,
                        )
                        if reverse_report.get("errors"):
                            rollback_errors.append(
                                "symlink target rollback reported errors"
                            )
                except Exception as rollback_error:
                    rollback_errors.append(f"symlink targets: {rollback_error}")
                progress(
                    "rollback_paths",
                    "Restoring original namespace paths.",
                    65,
                    "rolling_back",
                )
                rollback_errors.extend(self._rollback_namespace_paths(moved))
                progress(
                    "rollback_symlink_manifest",
                    "Restoring and verifying the captured symlink catalog.",
                    69,
                    "rolling_back",
                )
                rollback_errors.extend(
                    self._restore_and_validate_symlink_manifest(backup_bundle)
                )
                progress(
                    "rollback_files",
                    "Restoring captured application files.",
                    72,
                    "rolling_back",
                )
                rollback_errors.extend(self._restore_backup_files(backup_bundle))
                progress(
                    "rollback_config",
                    "Restoring the captured DUMB configuration.",
                    78,
                    "rolling_back",
                )
                try:
                    CONFIG_MANAGER.config = backup_config
                    if legacy_identity:
                        CONFIG_MANAGER.restore_legacy_infinidysk_identity()
                    CONFIG_MANAGER.save_config()
                except Exception as rollback_error:
                    rollback_errors.append(f"config: {rollback_error}")
                progress(
                    "rollback_services",
                    "Restarting the original services.",
                    84,
                    "rolling_back",
                )
                try:
                    restart_errors = self._start_processes(
                        process_handler,
                        running,
                        force_setup=False,
                        continue_on_error=True,
                    )
                    rollback_errors.extend(
                        f"start: {restart_error}" for restart_error in restart_errors
                    )
                except Exception as rollback_error:
                    rollback_errors.append(
                        f"start recovery: {_safe_error_detail(rollback_error)}"
                    )
                progress(
                    "rollback_arr", "Restoring Arr references.", 90, "rolling_back"
                )
                for original in preflight.get("arr") or []:
                    try:
                        desired = self._desired_arr_snapshot(original)
                        self._retry(
                            lambda original=original: _migration_arr_req(
                                _arr_url(
                                    original["host"],
                                    original["api_version"],
                                    "system/status",
                                ),
                                original["api_key"],
                                "GET",
                            )
                        )
                        self._apply_arr_snapshot(desired, original)
                        self._validate_arr_snapshot(desired, original)
                    except Exception as rollback_error:
                        rollback_errors.append(
                            f"{original.get('process_name')} Arr paths: "
                            f"{_safe_error_detail(rollback_error)}"
                        )
                progress(
                    "rollback_prowlarr",
                    "Restoring Prowlarr connections and tags.",
                    94,
                    "rolling_back",
                )
                for original in preflight.get("prowlarr") or []:
                    try:
                        desired = self._desired_prowlarr_snapshot(original)
                        self._retry(
                            lambda original=original: _migration_prowlarr_req(
                                f"{original['host']}/api/v1/system/status",
                                original["api_key"],
                                "GET",
                            )
                        )
                        self._apply_prowlarr_snapshot(desired, original)
                        self._validate_prowlarr_snapshot(desired, original)
                    except Exception as rollback_error:
                        rollback_errors.append(
                            f"{original.get('process_name')} Prowlarr applications: "
                            f"{_safe_error_detail(rollback_error)}"
                        )
                progress(
                    "rollback_media",
                    "Restoring media-server library paths.",
                    97,
                    "rolling_back",
                )
                for original in preflight.get("media") or []:
                    try:
                        desired = self._desired_media_snapshot(original)
                        self._retry(
                            lambda original=original: build_adapter(
                                original["service_key"],
                                original["process_name"],
                                logger,
                            ).library_paths()
                        )
                        self._apply_media_snapshot(desired, original, logger)
                        self._validate_media_snapshot(desired, original, logger)
                    except Exception as rollback_error:
                        rollback_errors.append(
                            f"{original.get('process_name')} libraries: "
                            f"{_safe_error_detail(rollback_error)}"
                        )
                progress(
                    "rollback_scan_guards",
                    "Restoring media-server scan settings.",
                    99,
                    "rolling_back",
                )
                for guard in scan_guards:
                    try:
                        adapter = build_adapter(
                            guard["service_key"], guard["process_name"], logger
                        )
                        adapter.restore_scan_guard(guard["snapshot"])
                    except Exception as rollback_error:
                        rollback_errors.append(
                            f"{guard.get('process_name')} scan guard: {rollback_error}"
                        )
                failure_state = self._load_state()
                failure_state.update(
                    {
                        "state_version": STATE_VERSION,
                        "status": (
                            "failed_rolled_back"
                            if not rollback_errors
                            else "rollback_attention_required"
                        ),
                        "failed_at": now,
                        "last_error": failure_detail,
                        "rollback_errors": rollback_errors,
                        "config_backup_path": str(config_backup_path),
                        "backup_bundle_path": str(backup_bundle),
                    }
                )
                try:
                    self._save_state(failure_state)
                except OSError:
                    pass
                detail = (
                    "The full namespace migration failed and was rolled back. "
                    f"Cause: {failure_detail}"
                )
                if rollback_errors:
                    detail = (
                        "The full namespace migration failed. Rollback completed with "
                        f"{len(rollback_errors)} issue(s); review the recovery details before restoring anything manually."
                    )
                raise InfiniDyskMigrationError(detail) from error


INFINIDYSK_MIGRATION_MANAGER = InfiniDyskMigrationManager()
