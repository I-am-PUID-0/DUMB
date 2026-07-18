import os, socket, psutil, threading, time, uuid, json, re
from json import load
from utils.config_loader import CONFIG_MANAGER
from utils.project_metadata import get_project_version
from utils.notifications import notify_event


class APIState:
    def __init__(self, process_handler, logger):
        self.logger = logger
        self.process_handler = process_handler
        self.status_file_path = "/healthcheck/running_processes.json"
        os.makedirs(os.path.dirname(self.status_file_path), exist_ok=True)
        self._status_cache = {}
        self._status_mtime = None
        self.service_status = self._load_status_from_file()
        self._status_cache = self.service_status
        self._status_mtime = self._get_status_mtime()
        self.shutdown_in_progress = set()
        self._update_cache = {}
        self._update_cache_lock = threading.Lock()
        self.update_notices_file_path = "/config/update_notices.json"
        self._update_notices_file_existed = os.path.exists(
            self.update_notices_file_path
        )
        self._update_notices_lock = threading.Lock()
        self._update_notices = self._load_update_notices()
        self._ensure_first_run_update_notice()
        self._symlink_backup_cache = {}
        self._symlink_backup_cache_lock = threading.Lock()
        self._symlink_job_cache = {}
        self._symlink_job_cache_lock = threading.Lock()

    def _normalize_process_name(self, value):
        return str(value or "").replace(" ", "").replace("/ ", "/").strip().lower()

    def _load_update_notices(self):
        try:
            with open(self.update_notices_file_path, "r") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                applied = payload.get("applied")
                info = payload.get("info")
                return {
                    "applied": (
                        [item for item in applied if isinstance(item, dict)]
                        if isinstance(applied, list)
                        else []
                    ),
                    "info": (
                        [item for item in info if isinstance(item, dict)]
                        if isinstance(info, list)
                        else []
                    ),
                }
        except FileNotFoundError:
            pass
        except Exception as exc:
            self.logger.debug("Failed to load update notices: %s", exc)
        return {"applied": [], "info": []}

    def _current_dumb_version(self):
        env_version = (os.environ.get("DUMB_VERSION") or "").strip()
        if env_version:
            return env_version

        candidates = (
            "/pyproject.toml",
            os.path.join(os.getcwd(), "pyproject.toml"),
            "/workspace/pyproject.toml",
        )
        for candidate in candidates:
            version = get_project_version(candidate, default="")
            if version:
                return version
        return None

    def _is_dev_version(self, version):
        value = str(version or "").strip()
        return bool(re.match(r"^v?\d+(?:\.\d+){1,3}-dev\.\d+$", value))

    def _is_release_version(self, version):
        value = str(version or "").strip()
        return bool(
            value
            and not self._is_dev_version(value)
            and re.match(r"^v?\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9._-]+)?$", value)
        )

    def _branch_commit_marker(self, version):
        value = str(version or "").strip()
        match = re.match(r"^(.+)-([0-9a-fA-F]{7,40})$", value)
        if not match:
            return None
        return match.group(2).strip().lower() or None

    def _ensure_first_run_update_notice(self):
        if self._update_notices_file_existed:
            return

        version = self._current_dumb_version()
        release_url = "https://github.com/I-am-PUID-0/DUMB/releases"
        notes_label = "Release notes"
        branch_commit = self._branch_commit_marker(version)
        if branch_commit:
            release_url = f"https://github.com/I-am-PUID-0/DUMB/commit/{branch_commit}"
            notes_label = "View commit"
        elif self._is_dev_version(version):
            release_url = "https://github.com/I-am-PUID-0/DUMB/releases/tag/dev-build"
            notes_label = "View dev build"
        elif self._is_release_version(version):
            release_url = f"{release_url}/tag/{version}"

        now_ts = int(time.time())
        notice = {
            "id": f"update-notices-intro:{version or 'unknown'}",
            "type": "info",
            "process_name": "DUMB API",
            "display_name": "DUMB",
            "status": "info",
            "message": "Update notices are now available in this DUMB release. Future backend/frontend updates will show available and applied change notices here.",
            "current_version": version,
            "checked_at": now_ts,
            "applied_at": now_ts,
            "release_url": release_url,
            "notes_label": notes_label,
            "repo_url": "https://github.com/I-am-PUID-0/DUMB",
        }
        notice = {
            key: value for key, value in notice.items() if value not in (None, "")
        }
        with self._update_notices_lock:
            info = self._update_notices.setdefault("info", [])
            if any(
                item.get("id") == notice["id"]
                for item in info
                if isinstance(item, dict)
            ):
                return
            info.insert(0, notice)
            self._update_notices["info"] = info[:10]
            self._save_update_notices()

    def _save_update_notices(self):
        try:
            os.makedirs(os.path.dirname(self.update_notices_file_path), exist_ok=True)
            tmp_path = f"{self.update_notices_file_path}.tmp"
            with open(tmp_path, "w") as handle:
                json.dump(self._update_notices, handle, indent=2, sort_keys=True)
            os.replace(tmp_path, self.update_notices_file_path)
        except Exception as exc:
            self.logger.debug("Failed to save update notices: %s", exc)

    def _record_applied_update_notice(self, process_name, payload, previous):
        now_ts = int(time.time())
        previous = previous if isinstance(previous, dict) else {}
        notice = {
            "id": uuid.uuid4().hex,
            "type": "applied",
            "process_name": process_name,
            "display_name": process_name,
            "status": "updated",
            "message": payload.get("message") or f"Updated {process_name}.",
            "previous_version": payload.get("previous_version")
            or previous.get("current_version"),
            "current_version": payload.get("current_version")
            or previous.get("available_version"),
            "available_version": payload.get("available_version")
            or previous.get("available_version"),
            "applied_at": payload.get("applied_at")
            or payload.get("checked_at")
            or now_ts,
            "checked_at": payload.get("checked_at") or now_ts,
        }
        notice = {
            key: value for key, value in notice.items() if value not in (None, "")
        }
        with self._update_notices_lock:
            applied = self._update_notices.setdefault("applied", [])
            applied.insert(0, notice)
            self._update_notices["applied"] = applied[:30]
            self._save_update_notices()

    def set_update_status(self, process_name, payload):
        if not process_name or not isinstance(payload, dict):
            return
        normalized = self._normalize_process_name(process_name)
        now_ts = int(time.time())
        with self._update_cache_lock:
            previous = self._update_cache.get(normalized)
            update_payload = {
                "process_name": process_name,
                "checked_at": payload.get("checked_at") or now_ts,
                **payload,
            }
            if update_payload.get("status") == "updated":
                update_payload.setdefault("applied_at", now_ts)
                if isinstance(previous, dict):
                    update_payload.setdefault(
                        "previous_version", previous.get("current_version")
                    )
                    update_payload.setdefault(
                        "available_version", previous.get("available_version")
                    )
                    update_payload.setdefault(
                        "current_version", previous.get("available_version")
                    )
            self._update_cache[normalized] = update_payload

        if update_payload.get("status") == "updated":
            self._record_applied_update_notice(process_name, update_payload, previous)
            notify_event(
                "update.succeeded",
                "success",
                f"Update completed for {process_name}",
                update_payload.get("message")
                or f"{process_name} was updated successfully.",
                service_name=process_name,
            )
        elif update_payload.get("status") == "update_available" and (
            not isinstance(previous, dict)
            or previous.get("status") != "update_available"
            or previous.get("available_version")
            != update_payload.get("available_version")
        ):
            notify_event(
                "update.available",
                "info",
                f"Update available for {process_name}",
                update_payload.get("message")
                or f"Version {update_payload.get('available_version') or 'unknown'} is available.",
                service_name=process_name,
            )
        elif update_payload.get("status") in {"error", "failed"}:
            notify_event(
                "update.failed",
                "critical",
                f"Update operation failed for {process_name}",
                update_payload.get("message") or "Review DUMB logs for details.",
                service_name=process_name,
            )

    def get_update_status(self, process_name):
        normalized = self._normalize_process_name(process_name)
        with self._update_cache_lock:
            payload = self._update_cache.get(normalized)
            if not payload:
                return None
            return dict(payload)

    def get_update_statuses(self):
        with self._update_cache_lock:
            return [dict(payload) for payload in self._update_cache.values()]

    def get_update_notices(self):
        with self._update_notices_lock:
            return {
                "applied": [
                    dict(item)
                    for item in self._update_notices.get("applied", [])
                    if isinstance(item, dict)
                ],
                "info": [
                    dict(item)
                    for item in self._update_notices.get("info", [])
                    if isinstance(item, dict)
                ],
            }

    def set_symlink_backup_status(self, process_name, payload):
        if not process_name or not isinstance(payload, dict):
            return
        normalized = self._normalize_process_name(process_name)
        status_payload = {
            "process_name": process_name,
            "checked_at": payload.get("checked_at") or int(time.time()),
            **payload,
        }
        with self._symlink_backup_cache_lock:
            self._symlink_backup_cache[normalized] = status_payload

    def get_symlink_backup_status(self, process_name):
        normalized = self._normalize_process_name(process_name)
        with self._symlink_backup_cache_lock:
            payload = self._symlink_backup_cache.get(normalized)
            if not payload:
                return None
            return dict(payload)

    def _cleanup_symlink_jobs(self):
        max_jobs = 500
        now_ts = int(time.time())
        cutoff_ts = now_ts - 86400
        keys_to_remove = []
        for job_id, payload in self._symlink_job_cache.items():
            if not isinstance(payload, dict):
                keys_to_remove.append(job_id)
                continue
            status = str(payload.get("status") or "").strip().lower()
            updated_at = int(payload.get("updated_at") or 0)
            if (
                status in {"completed", "error"}
                and updated_at
                and updated_at < cutoff_ts
            ):
                keys_to_remove.append(job_id)
        for job_id in keys_to_remove:
            self._symlink_job_cache.pop(job_id, None)

        if len(self._symlink_job_cache) <= max_jobs:
            return
        sorted_jobs = sorted(
            self._symlink_job_cache.items(),
            key=lambda item: int((item[1] or {}).get("updated_at") or 0),
            reverse=True,
        )
        retained = dict(sorted_jobs[:max_jobs])
        self._symlink_job_cache = retained

    def create_symlink_job(self, process_name, operation, metadata=None):
        now_ts = int(time.time())
        job_id = uuid.uuid4().hex
        payload = {
            "job_id": job_id,
            "process_name": process_name,
            "operation": str(operation or "").strip() or "unknown",
            "status": "queued",
            "created_at": now_ts,
            "updated_at": now_ts,
            "metadata": metadata if isinstance(metadata, dict) else {},
            "result": None,
            "error": None,
        }
        with self._symlink_job_cache_lock:
            self._cleanup_symlink_jobs()
            self._symlink_job_cache[job_id] = payload
        return dict(payload)

    def update_symlink_job(self, job_id, updates):
        if not job_id or not isinstance(updates, dict):
            return None
        terminal_payload = None
        with self._symlink_job_cache_lock:
            payload = self._symlink_job_cache.get(job_id)
            if not payload:
                return None
            previous_status = str(payload.get("status") or "").strip().lower()
            payload.update(updates)
            payload["updated_at"] = int(time.time())
            self._symlink_job_cache[job_id] = payload
            next_status = str(payload.get("status") or "").strip().lower()
            if next_status in {"completed", "error"} and next_status != previous_status:
                terminal_payload = dict(payload)
            result = dict(payload)
        if terminal_payload:
            process_name = terminal_payload.get("process_name") or "DUMB"
            operation = terminal_payload.get("operation") or "symlink job"
            succeeded = terminal_payload.get("status") == "completed"
            notify_event(
                "symlink.job.succeeded" if succeeded else "symlink.job.failed",
                "success" if succeeded else "critical",
                f"Symlink {operation} {'completed' if succeeded else 'failed'} for {process_name}",
                (
                    "The background symlink operation completed successfully."
                    if succeeded
                    else terminal_payload.get("error")
                    or "Review DUMB logs for details."
                ),
                service_name=process_name,
                metadata={"job_id": job_id, "operation": operation},
            )
        return result

    def get_symlink_job(self, job_id):
        if not job_id:
            return None
        with self._symlink_job_cache_lock:
            payload = self._symlink_job_cache.get(job_id)
            if not payload:
                return None
            return dict(payload)

    def get_latest_symlink_job(
        self,
        process_name: str,
        operation: str | None = None,
        active_only: bool = True,
    ):
        normalized_process = self._normalize_process_name(process_name)
        operation_filter = str(operation or "").strip().lower()
        active_statuses = {"queued", "running"}
        with self._symlink_job_cache_lock:
            candidates = []
            for payload in self._symlink_job_cache.values():
                if not isinstance(payload, dict):
                    continue
                if (
                    self._normalize_process_name(payload.get("process_name"))
                    != normalized_process
                ):
                    continue
                op = str(payload.get("operation") or "").strip().lower()
                if operation_filter and op != operation_filter:
                    continue
                status = str(payload.get("status") or "").strip().lower()
                if active_only and status not in active_statuses:
                    continue
                candidates.append(payload)
            if not candidates:
                return None
            candidates.sort(
                key=lambda item: int(item.get("updated_at") or 0), reverse=True
            )
            return dict(candidates[0])

    def _get_status_mtime(self):
        try:
            return os.path.getmtime(self.status_file_path)
        except FileNotFoundError:
            return None

    def _load_status_from_file(self):
        try:
            with open(self.status_file_path, "r") as f:
                data = load(f)
                return data
        except FileNotFoundError:
            self.logger.debug(
                f"Status file {self.status_file_path} not found. Initializing empty status."
            )
            with open(self.status_file_path, "w") as f:
                f.write("{}")
            return {}
        except Exception as e:
            self.logger.error(f"Error loading status file: {e}")
            return {}

    def _refresh_status_cache(self):
        mtime = self._get_status_mtime()
        if mtime is not None and mtime == self._status_mtime:
            return self._status_cache
        data = self._load_status_from_file()
        self._status_cache = data
        self._status_mtime = self._get_status_mtime()
        return data

    def get_status(self, process_name):
        running_processes = self._refresh_status_cache()

        def normalize(name):
            return name.replace(" ", "").replace("/ ", "/").strip().lower()

        normalized_input = normalize(process_name)
        if normalized_input == "dumbapi" or normalized_input == "dmbapi":
            return "running"
        for stored_name in running_processes:
            if normalized_input == normalize(stored_name):
                return "running"
        if normalized_input in {"plexdbrepair", "dbrepair"}:
            plex_cfg = CONFIG_MANAGER.get("plex", {}) or {}
            if plex_cfg.get("dbrepair", {}).get("enabled"):
                return "idle"
        return "stopped"

    def get_running_processes(self):
        running_processes = self._refresh_status_cache()
        if isinstance(running_processes, dict):
            return list(running_processes.keys())
        return []

    def get_status_details(self, process_name, include_health=False):
        running_processes = self._refresh_status_cache()

        def normalize(name):
            return name.replace(" ", "").replace("/ ", "/").strip().lower()

        normalized_input = normalize(process_name)
        status = "stopped"
        matched_name = None
        pid = None

        if normalized_input in ("dumbapi", "dmbapi"):
            status = "running"
        elif normalized_input in {"plexdbrepair", "dbrepair"}:
            plex_cfg = CONFIG_MANAGER.get("plex", {}) or {}
            if plex_cfg.get("dbrepair", {}).get("enabled"):
                status = "idle"
        else:
            for stored_name, stored_pid in running_processes.items():
                if normalized_input == normalize(stored_name):
                    status = "running"
                    matched_name = stored_name
                    pid = stored_pid
                    break

        if not include_health:
            return {"status": status}

        healthy, reason = self._check_health(matched_name, pid, status)
        restart_stats = self.process_handler.get_restart_stats(
            matched_name or process_name
        )
        return {
            "status": status,
            "healthy": healthy,
            "health_reason": reason,
            "restart": restart_stats,
        }

    def get_running_status_snapshot(self, include_health=False):
        running_processes = self._refresh_status_cache()
        if not isinstance(running_processes, dict):
            return []
        if not include_health:
            return list(running_processes.keys())
        snapshot = []
        for name, pid in running_processes.items():
            healthy, reason = self._check_health(name, pid, "running")
            snapshot.append(
                {
                    "process_name": name,
                    "status": "running",
                    "healthy": healthy,
                    "health_reason": reason,
                    "restart": self.process_handler.get_restart_stats(name),
                }
            )
        return snapshot

    def _collect_config_ports(self, config):
        ports = set()
        for key in ("port", "frontend_port", "backend_port", "webdav_port"):
            value = config.get(key)
            if isinstance(value, int):
                ports.add(value)
        env = config.get("env", {})
        for key in ("PORT", "FRONTEND_PORT", "BACKEND_PORT", "WEBDAV_PORT"):
            value = env.get(key)
            if isinstance(value, str) and value.isdigit():
                ports.add(int(value))
        return sorted(ports)

    def _normalize_host(self, host):
        if not host or host in {"0.0.0.0", "::"}:
            return "127.0.0.1"
        return host

    def _is_port_open(self, host, port, timeout=1.5):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _get_process_config(self, process_name):
        if not CONFIG_MANAGER:
            return None
        key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
        if not key and not instance_name:
            return None
        return CONFIG_MANAGER.get_instance(instance_name, key)

    def _check_health(self, process_name, pid, status):
        if status == "idle":
            return True, "Process idle"
        if status != "running" or not process_name:
            return False, "Process not running"

        if not pid or not psutil.pid_exists(pid):
            return False, "Process PID not running"

        try:
            proc = psutil.Process(pid)
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return False, "Process not healthy"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False, "Process could not be inspected"

        config = self._get_process_config(process_name)
        if not config:
            return True, None

        host = self._normalize_host(config.get("host"))
        ports = self._collect_config_ports(config)
        for port in ports:
            if not self._is_port_open(host, port):
                return False, f"Port {host}:{port} not responding"

        return True, None

    def debug_state(self):
        self.logger.info(f"Current APIState: {self.service_status}")
