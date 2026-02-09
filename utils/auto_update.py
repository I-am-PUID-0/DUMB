from utils.global_logger import logger
from utils.logger import format_time
from utils.versions import Versions
from utils.setup import setup_project, setup_release_version, configure_project
from utils.plex import PlexInstaller
from utils.arr import ArrInstaller
from utils.jellyfin import JellyfinInstaller
from utils.config_loader import CONFIG_MANAGER
from datetime import datetime
from glob import glob
import threading, time, os, schedule, requests, subprocess


class Update:
    _scheduler_initialized = False
    _jobs = {}
    _next_check_at = {}
    _symlink_backup_jobs = {}
    _symlink_backup_next_at = {}
    _schedule_thread_started = False
    _schedule_thread_count = 0
    _schedule_thread_lock = threading.Lock()

    def __init__(self, process_handler):
        self.process_handler = process_handler
        self.logger = process_handler.logger
        self.updating = threading.Lock()

        if not Update._scheduler_initialized:
            self.scheduler = schedule.Scheduler()
            Update._scheduler_initialized = True
        else:
            self.scheduler = schedule.default_scheduler

    def supports_manual_update(self, key, config):
        if key in {"bazarr"}:
            return False
        if key in {
            "plex",
            "jellyfin",
            "emby",
            "sonarr",
            "radarr",
            "lidarr",
            "prowlarr",
            "readarr",
            "whisparr",
            "whisparr-v3",
        }:
            return True
        if config and config.get("repo_owner") and config.get("repo_name"):
            return True
        return False

    def _get_update_block_reason(self, config):
        if config.get("pinned_version"):
            return "pinned_version"
        if config.get("branch_enabled"):
            return "branch"
        if config.get("release_version_enabled"):
            if not self._release_is_nightly_or_prerelease(config):
                return "release"
        return None

    def _safe_record_update_status(self, process_name, payload):
        try:
            from utils.dependencies import get_api_state

            api_state = get_api_state()
            if api_state:
                api_state.set_update_status(process_name, payload)
        except Exception:
            return

    def _safe_record_symlink_backup_status(self, process_name, payload):
        try:
            from utils.dependencies import get_api_state

            api_state = get_api_state()
            if api_state:
                api_state.set_symlink_backup_status(process_name, payload)
        except Exception:
            return

    def supports_symlink_backup(self, key):
        return key in {"decypharr", "nzbdav", "cli_debrid", "riven_backend"}

    def symlink_backup_enabled(self, process_name, config, key):
        if not self.supports_symlink_backup(key):
            return False
        return bool(config.get("symlink_backup_enabled", False))

    def symlink_backup_interval(self, process_name, config):
        default_interval = 24
        try:
            interval = int(config.get("symlink_backup_interval", default_interval))
        except Exception as e:
            self.logger.error(
                f"Failed to retrieve symlink_backup_interval for {process_name}: {e}"
            )
            interval = default_interval
        return max(1, interval)

    def symlink_backup_start_time(self, process_name, config):
        default_start_time = "04:00"
        try:
            raw_value = str(config.get("symlink_backup_start_time", default_start_time))
            normalized = raw_value.strip()
            datetime.strptime(normalized, "%H:%M")
            return normalized
        except Exception:
            self.logger.warning(
                "Invalid symlink_backup_start_time for %s. Falling back to %s",
                process_name,
                default_start_time,
            )
            return default_start_time

    def symlink_backup_path(self, process_name, config):
        process_slug = self._normalize_process_slug(process_name)
        default_path = (
            f"/config/symlink-repair/snapshots/{process_slug}-{{timestamp}}.json"
        )
        value = str(config.get("symlink_backup_path", default_path) or "").strip()
        return value or default_path

    def symlink_backup_include_broken(self, config):
        return bool(config.get("symlink_backup_include_broken", True))

    def symlink_backup_roots(self, config):
        raw = config.get("symlink_backup_roots")
        if isinstance(raw, list):
            roots = [str(v).strip() for v in raw if str(v).strip()]
            return roots or None
        if isinstance(raw, str):
            roots = [
                entry.strip()
                for entry in raw.replace(",", "\n").split("\n")
                if entry.strip()
            ]
            return roots or None
        return None

    def symlink_backup_retention_count(self, process_name, config):
        default_count = 1
        try:
            count = int(config.get("symlink_backup_retention_count", default_count))
        except Exception as e:
            self.logger.error(
                f"Failed to retrieve symlink_backup_retention_count for {process_name}: {e}"
            )
            count = default_count
        return max(0, count)

    def _normalize_process_slug(self, process_name):
        return (
            "".join(
                ch.lower() if ch.isalnum() else "-" for ch in str(process_name or "")
            ).strip("-")
            or "service"
        )

    def _symlink_manifest_glob_pattern(self, process_name, template):
        raw_template = str(template or "").strip()
        if not raw_template:
            raw_template = f"/config/symlink-repair/snapshots/{self._normalize_process_slug(process_name)}-{{timestamp}}.json"
        replacements = {
            "{timestamp}": "*",
            "{date}": "*",
            "{time}": "*",
            "{process_name}": str(process_name or ""),
            "{process_slug}": self._normalize_process_slug(process_name),
        }
        pattern = raw_template
        for token, value in replacements.items():
            pattern = pattern.replace(token, value)
        return pattern

    def _prune_symlink_backup_manifests(
        self, process_name, path_template, retention_count
    ):
        keep_count = max(0, int(retention_count))
        if keep_count <= 0:
            return {"pruned": 0, "kept": 0, "errors": []}

        pattern = self._symlink_manifest_glob_pattern(process_name, path_template)
        manifest_candidates = []
        errors = []
        for path in glob(pattern):
            if not os.path.isfile(path):
                continue
            try:
                mtime = int(os.path.getmtime(path))
            except Exception:
                mtime = 0
            manifest_candidates.append((path, mtime))

        manifest_candidates.sort(key=lambda item: item[1], reverse=True)
        stale = manifest_candidates[keep_count:]
        pruned = 0
        for stale_path, _ in stale:
            try:
                os.remove(stale_path)
                pruned += 1
            except Exception as e:
                errors.append({"path": stale_path, "error": str(e)})

        return {"pruned": pruned, "kept": keep_count, "errors": errors}

    def _resolve_symlink_backup_path(self, process_name, path_template, run_ts):
        dt = datetime.utcfromtimestamp(run_ts)
        replacements = {
            "{timestamp}": dt.strftime("%Y%m%dT%H%M%SZ"),
            "{date}": dt.strftime("%Y%m%d"),
            "{time}": dt.strftime("%H%M%S"),
            "{process_name}": str(process_name or ""),
            "{process_slug}": self._normalize_process_slug(process_name),
        }
        resolved = str(path_template or "").strip()
        for token, value in replacements.items():
            resolved = resolved.replace(token, value)
        return resolved

    def manual_update_check(self, process_name, force: bool = False):
        key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
        config = CONFIG_MANAGER.get_instance(instance_name, key)
        if not config:
            return {
                "status": "error",
                "reason": "config_not_found",
                "message": f"Configuration for {process_name} not found.",
            }
        if not self.supports_manual_update(key, config):
            return {
                "status": "unsupported",
                "reason": "unsupported",
                "message": f"Manual updates are not supported for {process_name}.",
            }

        with self.updating:
            payload = self._manual_update_check_internal(
                process_name, config, key, instance_name
            )
            self._safe_record_update_status(process_name, payload)
            return payload

    def _manual_update_check_internal(self, process_name, config, key, instance_name):
        block_reason = self._get_update_block_reason(config)
        checked_at = int(time.time())
        auto_update_enabled = bool(config.get("auto_update"))
        interval_hours = self.auto_update_interval(process_name, config)
        start_time = self.auto_update_start_time(process_name, config)
        next_check_at = (
            self._calculate_next_check_at(process_name, config, checked_at)
            if auto_update_enabled
            else None
        )

        if key == "plex":
            return self._manual_check_plex(
                process_name,
                config,
                instance_name,
                block_reason,
                checked_at,
                auto_update_enabled,
                interval_hours,
                start_time,
                next_check_at,
            )
        if key == "jellyfin":
            return self._manual_check_jellyfin(
                process_name,
                config,
                instance_name,
                block_reason,
                checked_at,
                auto_update_enabled,
                interval_hours,
                start_time,
                next_check_at,
            )
        if key == "emby":
            return self._manual_check_emby(
                process_name,
                config,
                instance_name,
                block_reason,
                checked_at,
                auto_update_enabled,
                interval_hours,
                start_time,
                next_check_at,
            )
        if key in [
            "sonarr",
            "radarr",
            "lidarr",
            "prowlarr",
            "readarr",
            "whisparr",
            "whisparr-v3",
        ]:
            release_enabled = config.get("release_version_enabled")
            branch_enabled = config.get("branch_enabled")
            repo_owner = config.get("repo_owner")
            repo_name = config.get("repo_name")
            has_repo = repo_owner and repo_name

            if branch_enabled:
                self.logger.warning(
                    "%s has 'branch_enabled' set, but branch builds are disabled for arr services. "
                    "Set 'release_version_enabled' instead.",
                    process_name,
                )
                branch_enabled = False

            # Check for conflicting flags - release_version_enabled takes priority
            if release_enabled and branch_enabled:
                self.logger.warning(
                    "%s has both 'release_version_enabled' and 'branch_enabled' set. "
                    "Using 'release_version_enabled'.",
                    process_name,
                )
                branch_enabled = False

            # Determine if using a custom fork
            official_repos = {
                "sonarr": ("Sonarr", "Sonarr"),
                "radarr": ("Radarr", "Radarr"),
                "lidarr": ("Lidarr", "Lidarr"),
                "prowlarr": ("Prowlarr", "Prowlarr"),
                "readarr": ("Readarr", "Readarr"),
                "whisparr": ("Whisparr", "Whisparr"),
                "whisparr-v3": ("Whisparr", "Whisparr"),
            }
            # Use GitHub for release_version_enabled OR branch_enabled (both need GitHub checks)
            use_github = has_repo and (release_enabled or branch_enabled)
            if use_github:
                return self._manual_check_generic_repo(
                    process_name,
                    config,
                    key,
                    instance_name,
                    block_reason,
                    checked_at,
                    auto_update_enabled,
                    interval_hours,
                    start_time,
                    next_check_at,
                )
            return self._manual_check_arr(
                process_name,
                config,
                key,
                instance_name,
                block_reason,
                checked_at,
                auto_update_enabled,
                interval_hours,
                start_time,
                next_check_at,
            )

        return self._manual_check_generic_repo(
            process_name,
            config,
            key,
            instance_name,
            block_reason,
            checked_at,
            auto_update_enabled,
            interval_hours,
            start_time,
            next_check_at,
        )

    def _manual_check_generic_repo(
        self,
        process_name,
        config,
        key,
        instance_name,
        block_reason,
        checked_at,
        auto_update_enabled,
        interval_hours,
        start_time,
        next_check_at,
    ):
        versions = Versions()
        repo_owner = config.get("repo_owner")
        repo_name = config.get("repo_name")
        if not repo_owner or not repo_name:
            return {
                "status": "unsupported",
                "reason": "repo_missing",
                "message": f"{process_name} missing repo configuration.",
                "checked_at": checked_at,
                "auto_update_enabled": auto_update_enabled,
                "auto_update_interval": interval_hours,
                "auto_update_start_time": start_time,
                "next_check_at": next_check_at,
            }

        nightly = False
        prerelease = False
        if config.get("release_version_enabled"):
            release_value = (config.get("release_version") or "").lower()
            if "nightly" in release_value:
                nightly = True
            elif "prerelease" in release_value:
                prerelease = True

        update_needed, update_info = versions.compare_versions(
            process_name,
            repo_owner,
            repo_name,
            instance_name,
            key,
            nightly=nightly,
            prerelease=prerelease,
        )
        if isinstance(update_info, str):
            return {
                "status": "error",
                "reason": "version_check_failed",
                "message": update_info,
                "checked_at": checked_at,
                "auto_update_enabled": auto_update_enabled,
                "auto_update_interval": interval_hours,
                "auto_update_start_time": start_time,
                "next_check_at": next_check_at,
            }

        current_version = update_info.get("current_version")
        latest_version = update_info.get("latest_version")
        if not update_needed:
            return {
                "status": "no_update",
                "current_version": current_version,
                "available_version": latest_version,
                "message": update_info.get("message"),
                "checked_at": checked_at,
                "auto_update_enabled": auto_update_enabled,
                "auto_update_interval": interval_hours,
                "auto_update_start_time": start_time,
                "next_check_at": next_check_at,
            }

        status = "update_available"
        if block_reason:
            status = "blocked"

        return {
            "status": status,
            "current_version": current_version,
            "available_version": latest_version,
            "reason": block_reason,
            "message": update_info.get("message"),
            "checked_at": checked_at,
            "auto_update_enabled": auto_update_enabled,
            "auto_update_interval": interval_hours,
            "auto_update_start_time": start_time,
            "next_check_at": next_check_at,
        }

    def _manual_check_arr(
        self,
        process_name,
        config,
        key,
        instance_name,
        block_reason,
        checked_at,
        auto_update_enabled,
        interval_hours,
        start_time,
        next_check_at,
    ):
        versions = Versions()
        install_dir = config.get("install_dir")
        if install_dir:
            current_version, error = versions.read_arr_version_from_dir(
                key, install_dir
            )
        else:
            current_version, error = versions.version_check(
                process_name, instance_name, key
            )
        installer = ArrInstaller(
            key,
            install_dir=install_dir,
            branch=config.get("branch"),
            repo_owner=config.get("repo_owner"),
            repo_name=config.get("repo_name"),
        )
        latest_version, latest_error = installer.get_latest_version()
        if not latest_version:
            return {
                "status": "error",
                "reason": "version_check_failed",
                "message": latest_error or error,
                "checked_at": checked_at,
                "auto_update_enabled": auto_update_enabled,
                "auto_update_interval": interval_hours,
                "auto_update_start_time": start_time,
                "next_check_at": next_check_at,
            }
        if current_version == latest_version:
            return {
                "status": "no_update",
                "current_version": current_version,
                "available_version": latest_version,
                "checked_at": checked_at,
                "auto_update_enabled": auto_update_enabled,
                "auto_update_interval": interval_hours,
                "auto_update_start_time": start_time,
                "next_check_at": next_check_at,
            }

        status = "update_available"
        if block_reason:
            status = "blocked"

        return {
            "status": status,
            "current_version": current_version,
            "available_version": latest_version,
            "reason": block_reason,
            "checked_at": checked_at,
            "auto_update_enabled": auto_update_enabled,
            "auto_update_interval": interval_hours,
            "auto_update_start_time": start_time,
            "next_check_at": next_check_at,
        }

    def _manual_check_jellyfin(
        self,
        process_name,
        config,
        instance_name,
        block_reason,
        checked_at,
        auto_update_enabled,
        interval_hours,
        start_time,
        next_check_at,
    ):
        versions = Versions()
        current_version, error = versions.version_check(
            process_name, instance_name, "jellyfin"
        )
        latest_version, latest_error = self.get_jellyfin_latest_version()
        if not latest_version:
            return {
                "status": "error",
                "reason": "version_check_failed",
                "message": latest_error or error,
                "checked_at": checked_at,
                "auto_update_enabled": auto_update_enabled,
                "auto_update_interval": interval_hours,
                "auto_update_start_time": start_time,
                "next_check_at": next_check_at,
            }
        if current_version == latest_version:
            return {
                "status": "no_update",
                "current_version": current_version,
                "available_version": latest_version,
                "checked_at": checked_at,
                "auto_update_enabled": auto_update_enabled,
                "auto_update_interval": interval_hours,
                "auto_update_start_time": start_time,
                "next_check_at": next_check_at,
            }

        status = "update_available"
        if block_reason:
            status = "blocked"

        return {
            "status": status,
            "current_version": current_version,
            "available_version": latest_version,
            "reason": block_reason,
            "checked_at": checked_at,
            "auto_update_enabled": auto_update_enabled,
            "auto_update_interval": interval_hours,
            "auto_update_start_time": start_time,
            "next_check_at": next_check_at,
        }

    def _manual_check_emby(
        self,
        process_name,
        config,
        instance_name,
        block_reason,
        checked_at,
        auto_update_enabled,
        interval_hours,
        start_time,
        next_check_at,
    ):
        versions = Versions()
        current_version, error = versions.version_check(
            process_name, instance_name, "emby"
        )
        latest_version, latest_error = self.get_emby_latest_version(config)
        if not latest_version:
            return {
                "status": "error",
                "reason": "version_check_failed",
                "message": latest_error or error,
                "checked_at": checked_at,
                "auto_update_enabled": auto_update_enabled,
                "auto_update_interval": interval_hours,
                "auto_update_start_time": start_time,
                "next_check_at": next_check_at,
            }
        if current_version == latest_version:
            return {
                "status": "no_update",
                "current_version": current_version,
                "available_version": latest_version,
                "checked_at": checked_at,
                "auto_update_enabled": auto_update_enabled,
                "auto_update_interval": interval_hours,
                "auto_update_start_time": start_time,
                "next_check_at": next_check_at,
            }

        status = "update_available"
        if block_reason:
            status = "blocked"

        return {
            "status": status,
            "current_version": current_version,
            "available_version": latest_version,
            "reason": block_reason,
            "checked_at": checked_at,
            "auto_update_enabled": auto_update_enabled,
            "auto_update_interval": interval_hours,
            "auto_update_start_time": start_time,
            "next_check_at": next_check_at,
        }

    def _manual_check_plex(
        self,
        process_name,
        config,
        instance_name,
        block_reason,
        checked_at,
        auto_update_enabled,
        interval_hours,
        start_time,
        next_check_at,
    ):
        plex_media_server_dir = config.get(
            "plex_media_server_dir", "/usr/lib/plexmediaserver"
        )
        if not os.path.exists(plex_media_server_dir):
            return {
                "status": "not_installed",
                "message": "Plex Media Server not installed.",
                "checked_at": checked_at,
                "auto_update_enabled": auto_update_enabled,
                "auto_update_interval": interval_hours,
                "auto_update_start_time": start_time,
                "next_check_at": next_check_at,
            }

        installer = PlexInstaller()
        versions = Versions()
        current_version, error = versions.version_check(
            process_name, instance_name, "plex"
        )
        current_version = installer.normalize_version(current_version or "")
        build = installer.get_architecture()
        if not build:
            return {
                "status": "error",
                "reason": "unsupported_arch",
                "message": "Unsupported architecture.",
                "checked_at": checked_at,
                "auto_update_enabled": auto_update_enabled,
                "auto_update_interval": interval_hours,
                "auto_update_start_time": start_time,
                "next_check_at": next_check_at,
            }
        try:
            latest_version, _ = installer.get_download_info(build)
        except Exception as e:
            return {
                "status": "error",
                "reason": "version_check_failed",
                "message": str(e),
                "checked_at": checked_at,
                "auto_update_enabled": auto_update_enabled,
                "auto_update_interval": interval_hours,
                "auto_update_start_time": start_time,
                "next_check_at": next_check_at,
            }
        latest_version = installer.normalize_version(latest_version or "")

        if current_version == latest_version:
            return {
                "status": "no_update",
                "current_version": current_version,
                "available_version": latest_version,
                "checked_at": checked_at,
                "auto_update_enabled": auto_update_enabled,
                "auto_update_interval": interval_hours,
                "auto_update_start_time": start_time,
                "next_check_at": next_check_at,
            }

        status = "update_available"
        if block_reason:
            status = "blocked"

        return {
            "status": status,
            "current_version": current_version,
            "available_version": latest_version,
            "reason": block_reason,
            "checked_at": checked_at,
            "auto_update_enabled": auto_update_enabled,
            "auto_update_interval": interval_hours,
            "auto_update_start_time": start_time,
            "next_check_at": next_check_at,
        }

    def manual_update_install(self, process_name, allow_override=False, target=None):
        key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
        config = CONFIG_MANAGER.get_instance(instance_name, key)
        if not config:
            return {
                "status": "error",
                "reason": "config_not_found",
                "message": f"Configuration for {process_name} not found.",
            }
        if not self.supports_manual_update(key, config):
            return {
                "status": "unsupported",
                "reason": "unsupported",
                "message": f"Manual updates are not supported for {process_name}.",
            }

        block_reason = self._get_update_block_reason(config)
        if block_reason and not allow_override:
            payload = {
                "status": "blocked",
                "reason": block_reason,
                "message": f"Updates blocked for {process_name}.",
            }
            self._safe_record_update_status(process_name, payload)
            return payload

        original = {
            "pinned_version": config.get("pinned_version"),
            "release_version_enabled": config.get("release_version_enabled"),
            "release_version": config.get("release_version"),
            "branch_enabled": config.get("branch_enabled"),
            "branch": config.get("branch"),
        }

        with self.updating:
            try:
                if allow_override:
                    config["pinned_version"] = ""
                    config["branch_enabled"] = False
                    if config.get(
                        "release_version_enabled"
                    ) and not self._release_is_nightly_or_prerelease(config):
                        config["release_version_enabled"] = False
                        config["release_version"] = ""
                if target:
                    target_value = str(target).lower()
                    if target_value in {"prerelease", "nightly"}:
                        config["release_version_enabled"] = True
                        config["release_version"] = target_value

                success, message = self.update_check(
                    process_name, config, key, instance_name
                )
                if not success:
                    status = "no_update"
                    if isinstance(message, str) and "Failed" in message:
                        status = "error"
                    payload = {
                        "status": status,
                        "message": message,
                    }
                    self._safe_record_update_status(process_name, payload)
                    return payload

                payload = {
                    "status": "updated",
                    "message": message,
                }
                self._safe_record_update_status(process_name, payload)
                return payload
            finally:
                config["pinned_version"] = original.get("pinned_version")
                config["release_version_enabled"] = original.get(
                    "release_version_enabled"
                )
                config["release_version"] = original.get("release_version")
                config["branch_enabled"] = original.get("branch_enabled")
                config["branch"] = original.get("branch")

    def update_schedule(self, process_name, config, key, instance_name):
        interval_minutes = int(self.auto_update_interval(process_name, config) * 60)
        start_time = self.auto_update_start_time(process_name, config)
        self.logger.debug(
            f"Scheduling automatic update check every {interval_minutes} minutes for {process_name} (start time: {start_time})"
        )

        existing_job = Update._jobs.get(process_name)
        if existing_job:
            try:
                self.scheduler.cancel_job(existing_job)
            except Exception:
                pass
        next_check_at = self._calculate_next_check_at(process_name, config)
        Update._next_check_at[process_name] = next_check_at
        job = self.scheduler.every(1).minutes.do(
            self._run_scheduled_update_if_due, process_name, config, key, instance_name
        )
        Update._jobs[process_name] = job
        self.logger.debug(
            f"Scheduled automatic update check for {process_name}, w/ key: {key}, and job ID: {id(job)}"
        )
        self._safe_record_update_status(
            process_name,
            {
                "status": "scheduled",
                "auto_update_enabled": True,
                "auto_update_interval": self.auto_update_interval(process_name, config),
                "auto_update_start_time": start_time,
                "next_check_at": next_check_at,
            },
        )

        self._ensure_scheduler_running(process_name)

    def _ensure_scheduler_running(self, process_name):
        with Update._schedule_thread_lock:
            if Update._schedule_thread_started:
                self.logger.debug(
                    "Scheduler loop already active; skipping duplicate for %s. Active loops: %d, jobs: %d, thread: %s",
                    process_name,
                    Update._schedule_thread_count,
                    len(self.scheduler.jobs),
                    threading.current_thread().name,
                )
                return
            Update._schedule_thread_started = True
            Update._schedule_thread_count += 1
            thread = threading.Thread(target=self._run_scheduler_loop, daemon=True)
            thread.start()
            self.logger.debug(
                "Scheduler loop started for %s. Active loops: %d, jobs: %d, thread: %s",
                process_name,
                Update._schedule_thread_count,
                len(self.scheduler.jobs),
                thread.name,
            )

    def _run_scheduler_loop(self):
        try:
            while not self.process_handler.shutting_down:
                self.scheduler.run_pending()
                time.sleep(1)
        finally:
            with Update._schedule_thread_lock:
                Update._schedule_thread_started = False
                if Update._schedule_thread_count > 0:
                    Update._schedule_thread_count -= 1
                self.logger.debug(
                    "Scheduler loop stopped. Active loops: %d, jobs: %d, thread: %s",
                    Update._schedule_thread_count,
                    len(self.scheduler.jobs),
                    threading.current_thread().name,
                )

    def reschedule_auto_update(self, process_name):
        key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
        config = CONFIG_MANAGER.get_instance(instance_name, key)
        if not config:
            return False, "Configuration not found"
        if not config.get("auto_update"):
            existing_job = Update._jobs.get(process_name)
            if existing_job:
                try:
                    self.scheduler.cancel_job(existing_job)
                except Exception:
                    pass
                Update._jobs.pop(process_name, None)
            Update._next_check_at.pop(process_name, None)
            self._safe_record_update_status(
                process_name,
                {
                    "status": "disabled",
                    "auto_update_enabled": False,
                    "auto_update_interval": self.auto_update_interval(
                        process_name, config
                    ),
                    "auto_update_start_time": self.auto_update_start_time(
                        process_name, config
                    ),
                    "next_check_at": None,
                },
            )
            return True, "Auto-update disabled"

        self.update_schedule(process_name, config, key, instance_name)
        return True, "Auto-update rescheduled"

    def _cancel_symlink_backup_job(self, process_name):
        existing_job = Update._symlink_backup_jobs.get(process_name)
        if existing_job:
            try:
                self.scheduler.cancel_job(existing_job)
            except Exception:
                pass
            Update._symlink_backup_jobs.pop(process_name, None)
        Update._symlink_backup_next_at.pop(process_name, None)

    def schedule_symlink_backup(self, process_name, config, key, instance_name):
        if not self.supports_symlink_backup(key):
            return
        self._cancel_symlink_backup_job(process_name)
        interval_hours = self.symlink_backup_interval(process_name, config)
        start_time = self.symlink_backup_start_time(process_name, config)
        retention_count = self.symlink_backup_retention_count(process_name, config)
        next_backup_at = self._calculate_next_run_at(interval_hours, start_time)
        Update._symlink_backup_next_at[process_name] = next_backup_at
        job = self.scheduler.every(1).minutes.do(
            self._run_scheduled_symlink_backup_if_due, process_name, key, instance_name
        )
        Update._symlink_backup_jobs[process_name] = job
        self._safe_record_symlink_backup_status(
            process_name,
            {
                "status": "scheduled",
                "symlink_backup_enabled": True,
                "symlink_backup_interval": interval_hours,
                "symlink_backup_start_time": start_time,
                "symlink_backup_path": self.symlink_backup_path(process_name, config),
                "symlink_backup_include_broken": self.symlink_backup_include_broken(
                    config
                ),
                "symlink_backup_roots": self.symlink_backup_roots(config),
                "symlink_backup_retention_count": retention_count,
                "next_backup_at": next_backup_at,
            },
        )
        self.logger.debug(
            "Scheduled symlink backup for %s every %s hours (start time: %s, next: %s).",
            process_name,
            interval_hours,
            start_time,
            next_backup_at,
        )
        self._ensure_scheduler_running(process_name)

    def _run_scheduled_symlink_backup_if_due(self, process_name, key, instance_name):
        latest_config = CONFIG_MANAGER.get_instance(instance_name, key)
        if not latest_config:
            return
        if not self.symlink_backup_enabled(process_name, latest_config, key):
            return

        now_ts = int(time.time())
        due_at = Update._symlink_backup_next_at.get(process_name)
        if due_at is None:
            due_at = self._calculate_next_symlink_backup_at(
                process_name, latest_config, now_ts
            )
            Update._symlink_backup_next_at[process_name] = due_at
        if now_ts < due_at:
            return

        next_due_at = self._calculate_next_symlink_backup_at(
            process_name, latest_config, now_ts + 1
        )
        Update._symlink_backup_next_at[process_name] = next_due_at
        self._run_symlink_backup(
            process_name, latest_config, key, instance_name, now_ts, next_due_at
        )

    def _run_symlink_backup(
        self, process_name, config, key, instance_name, run_ts=None, next_backup_at=None
    ):
        if run_ts is None:
            run_ts = int(time.time())
        from utils.symlink_repair import backup_symlink_manifest

        path_template = self.symlink_backup_path(process_name, config)
        backup_path = self._resolve_symlink_backup_path(
            process_name, path_template, run_ts
        )
        include_broken = self.symlink_backup_include_broken(config)
        roots = self.symlink_backup_roots(config)
        interval_hours = self.symlink_backup_interval(process_name, config)
        start_time = self.symlink_backup_start_time(process_name, config)
        retention_count = self.symlink_backup_retention_count(process_name, config)
        if next_backup_at is None:
            next_backup_at = self._calculate_next_symlink_backup_at(
                process_name, config, run_ts + 1
            )
            Update._symlink_backup_next_at[process_name] = next_backup_at

        try:
            report = backup_symlink_manifest(
                roots=roots,
                backup_path=backup_path,
                include_broken=include_broken,
            )
            prune_report = self._prune_symlink_backup_manifests(
                process_name=process_name,
                path_template=path_template,
                retention_count=retention_count,
            )
            payload = {
                "status": "completed",
                "message": "Symlink backup completed.",
                "symlink_backup_enabled": True,
                "symlink_backup_interval": interval_hours,
                "symlink_backup_start_time": start_time,
                "symlink_backup_path": path_template,
                "symlink_backup_include_broken": include_broken,
                "symlink_backup_roots": roots,
                "symlink_backup_retention_count": retention_count,
                "next_backup_at": next_backup_at,
                "last_backup_at": run_ts,
                "last_backup_manifest": report.get("backup_manifest"),
                "scanned_symlinks": report.get("scanned_symlinks"),
                "recorded_entries": report.get("recorded_entries"),
                "pruned_backups": prune_report.get("pruned"),
                "retention_errors": prune_report.get("errors"),
                "errors": report.get("errors"),
            }
            self._safe_record_symlink_backup_status(process_name, payload)
        except Exception as e:
            self.logger.error(
                "Scheduled symlink backup failed for %s: %s", process_name, e
            )
            self._safe_record_symlink_backup_status(
                process_name,
                {
                    "status": "error",
                    "message": str(e),
                    "symlink_backup_enabled": True,
                    "symlink_backup_interval": interval_hours,
                    "symlink_backup_start_time": start_time,
                    "symlink_backup_path": path_template,
                    "symlink_backup_include_broken": include_broken,
                    "symlink_backup_roots": roots,
                    "symlink_backup_retention_count": retention_count,
                    "next_backup_at": next_backup_at,
                    "last_backup_at": run_ts,
                },
            )

    def reschedule_symlink_backup(self, process_name):
        key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
        config = CONFIG_MANAGER.get_instance(instance_name, key)
        if not config:
            return False, "Configuration not found"

        if not self.supports_symlink_backup(key):
            self._cancel_symlink_backup_job(process_name)
            self._safe_record_symlink_backup_status(
                process_name,
                {
                    "status": "unsupported",
                    "message": "Symlink backup scheduling is not supported for this service.",
                    "symlink_backup_enabled": False,
                    "next_backup_at": None,
                },
            )
            return False, "Symlink backup scheduling not supported for this service"

        if not self.symlink_backup_enabled(process_name, config, key):
            self._cancel_symlink_backup_job(process_name)
            self._safe_record_symlink_backup_status(
                process_name,
                {
                    "status": "disabled",
                    "symlink_backup_enabled": False,
                    "symlink_backup_interval": self.symlink_backup_interval(
                        process_name, config
                    ),
                    "symlink_backup_start_time": self.symlink_backup_start_time(
                        process_name, config
                    ),
                    "symlink_backup_path": self.symlink_backup_path(
                        process_name, config
                    ),
                    "symlink_backup_include_broken": self.symlink_backup_include_broken(
                        config
                    ),
                    "symlink_backup_roots": self.symlink_backup_roots(config),
                    "symlink_backup_retention_count": self.symlink_backup_retention_count(
                        process_name, config
                    ),
                    "next_backup_at": None,
                },
            )
            return True, "Symlink backup schedule disabled"

        self.schedule_symlink_backup(process_name, config, key, instance_name)
        return True, "Symlink backup schedule rescheduled"

    def auto_update_interval(self, process_name, config):
        default_interval = 24
        try:
            interval = config.get("auto_update_interval", default_interval)
        except Exception as e:
            self.logger.error(
                f"Failed to retrieve auto_update_interval for {process_name}: {e}"
            )
            interval = default_interval

        return interval

    def auto_update_start_time(self, process_name, config):
        default_start_time = "04:00"
        try:
            raw_value = str(config.get("auto_update_start_time", default_start_time))
            normalized = raw_value.strip()
            datetime.strptime(normalized, "%H:%M")
            return normalized
        except Exception:
            self.logger.warning(
                "Invalid auto_update_start_time for %s. Falling back to %s",
                process_name,
                default_start_time,
            )
            return default_start_time

    def _calculate_next_run_at(self, interval_hours, start_time, now_ts=None):
        if now_ts is None:
            now_ts = int(time.time())
        interval_seconds = max(60, int(interval_hours * 3600))
        hour, minute = [int(part) for part in start_time.split(":", 1)]
        now_dt = datetime.fromtimestamp(now_ts)
        anchor_dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        anchor_ts = int(anchor_dt.timestamp())

        if now_ts <= anchor_ts:
            return anchor_ts

        elapsed = now_ts - anchor_ts
        intervals_elapsed = (elapsed + interval_seconds - 1) // interval_seconds
        return anchor_ts + intervals_elapsed * interval_seconds

    def _calculate_next_check_at(self, process_name, config, now_ts=None):
        interval_hours = self.auto_update_interval(process_name, config)
        start_time = self.auto_update_start_time(process_name, config)
        return self._calculate_next_run_at(interval_hours, start_time, now_ts)

    def _calculate_next_symlink_backup_at(self, process_name, config, now_ts=None):
        interval_hours = self.symlink_backup_interval(process_name, config)
        start_time = self.symlink_backup_start_time(process_name, config)
        return self._calculate_next_run_at(interval_hours, start_time, now_ts)

    def _run_scheduled_update_if_due(self, process_name, config, key, instance_name):
        latest_config = CONFIG_MANAGER.get_instance(instance_name, key)
        if not latest_config:
            return
        if not latest_config.get("auto_update"):
            return

        now_ts = int(time.time())
        due_at = Update._next_check_at.get(process_name)
        if due_at is None:
            due_at = self._calculate_next_check_at(process_name, latest_config, now_ts)
            Update._next_check_at[process_name] = due_at
        if now_ts < due_at:
            return

        next_due_at = self._calculate_next_check_at(
            process_name, latest_config, now_ts + 1
        )
        Update._next_check_at[process_name] = next_due_at
        self.scheduled_update_check(process_name, latest_config, key, instance_name)
        self._safe_record_update_status(
            process_name,
            {
                "status": "scheduled",
                "auto_update_enabled": True,
                "auto_update_interval": self.auto_update_interval(
                    process_name, latest_config
                ),
                "auto_update_start_time": self.auto_update_start_time(
                    process_name, latest_config
                ),
                "next_check_at": next_due_at,
            },
        )

    def auto_update(
        self, process_name, enable_update, force_update_check: bool = False
    ):
        key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
        config = CONFIG_MANAGER.get_instance(instance_name, key)
        if not config:
            return None, f"Configuration for {process_name} not found."
        try:
            self.reschedule_symlink_backup(process_name)
        except Exception as e:
            self.logger.warning(
                "Failed to reschedule symlink backup for %s: %s", process_name, e
            )

        if (
            config.get("pinned_version")
            or config.get("release_version_enabled")
            or config.get("branch_enabled")
        ):
            if not self._release_is_nightly_or_prerelease(config):
                enable_update = False
                self.logger.info(
                    "Automatic updates disabled for %s due to pinned, release, or branch configuration.",
                    process_name,
                )

        if key in ["bazarr"]:
            enable_update = False
            self.logger.info(
                f"Automatic updates are not yet supported for {process_name} ({key})."
            )
        if enable_update:
            self.logger.info(
                f"Automatic updates set to {format_time(self.auto_update_interval(process_name, config))} for {process_name}"
            )
            self.schedule_thread = threading.Thread(
                target=self.update_schedule,
                args=(process_name, config, key, instance_name),
            )
            self.schedule_thread.start()

            if (
                not force_update_check
                and self.process_handler.preinstall_complete
                and process_name in self.process_handler.preinstalled_processes
            ):
                self.logger.info(
                    "Skipping initial update check for preinstalled %s.",
                    process_name,
                )
                if self.process_handler.preinstall_complete:
                    success, setup_error = configure_project(
                        self.process_handler, process_name
                    )
                    if not success:
                        self.logger.warning(
                            "Configure-only setup failed for %s (%s). Falling back to full setup.",
                            process_name,
                            setup_error,
                        )
                        success, setup_error = setup_project(
                            self.process_handler, process_name
                        )
                else:
                    success, setup_error = setup_project(
                        self.process_handler, process_name
                    )
                if not success:
                    return None, setup_error

                return self.start_process(process_name, config, key, instance_name)

            success, error = self.initial_update_check(
                process_name, config, key, instance_name
            )
            if success:
                return success, error
            self.logger.warning(
                "Initial update check failed for %s: %s. Continuing startup without update.",
                process_name,
                error,
            )
            if self.process_handler.preinstall_complete:
                success, setup_error = configure_project(
                    self.process_handler, process_name
                )
                if not success:
                    self.logger.warning(
                        "Configure-only setup failed for %s (%s). Falling back to full setup.",
                        process_name,
                        setup_error,
                    )
                    success, setup_error = setup_project(
                        self.process_handler, process_name
                    )
            else:
                success, setup_error = setup_project(self.process_handler, process_name)
            if not success:
                return None, setup_error

            return self.start_process(process_name, config, key, instance_name)
        else:
            self.logger.info(f"Automatic update disabled for {process_name}")
            if (
                self.process_handler.preinstall_complete
                and process_name in self.process_handler.preinstalled_processes
            ):
                success, setup_error = configure_project(
                    self.process_handler, process_name
                )
                if not success:
                    self.logger.warning(
                        "Configure-only setup failed for %s (%s). Falling back to full setup.",
                        process_name,
                        setup_error,
                    )
                    success, setup_error = setup_project(
                        self.process_handler, process_name
                    )
            else:
                success, setup_error = setup_project(self.process_handler, process_name)
            if not success:
                return None, setup_error

            return self.start_process(process_name, config, key, instance_name)

    def _release_is_nightly_or_prerelease(self, config):
        if not config.get("release_version_enabled"):
            return False
        release_value = (config.get("release_version") or "").lower()
        return "nightly" in release_value or "prerelease" in release_value

    def initial_update_check(self, process_name, config, key, instance_name):
        with self.updating:
            self.logger.info(f"Performing initial update check for {process_name}")
            success, error = self.update_check(process_name, config, key, instance_name)
            if not success:
                if "No updates available" in error:
                    self.logger.info(error)
                    success, error = setup_project(self.process_handler, process_name)
                    if not success:
                        return None, f"Failed to set up {process_name}: {error}"

                    return self.start_process(process_name, config, key, instance_name)
                else:
                    return None, error

            return True, error

    def scheduled_update_check(self, process_name, config, key, instance_name):
        with self.updating:
            self.logger.info(f"Performing scheduled update check for {process_name}")
            try:
                payload = self._manual_update_check_internal(
                    process_name, config, key, instance_name
                )
                self._safe_record_update_status(process_name, payload)
            except Exception:
                pass
            success, error = self.update_check(process_name, config, key, instance_name)
            if not success:
                if "No updates available" in error:
                    self.logger.info(error)
                    # self.start_process(process_name, config, key, instance_name)
                else:
                    raise RuntimeError(error)

    def update_check(self, process_name, config, key, instance_name):
        if key == "plex":
            return self.update_check_plex(process_name, config, key, instance_name)
        if key == "jellyfin":
            pinned_version = config.get("pinned_version")
            if pinned_version:
                return self.update_check_pinned_version(
                    process_name,
                    config,
                    key,
                    instance_name,
                    pinned_version,
                )
            return self.update_check_jellyfin_latest(
                process_name, config, key, instance_name
            )
        if key == "emby":
            target_release = None
            if config.get("release_version_enabled"):
                target_release = config.get("release_version")
            if target_release:
                return self.update_check_pinned_version(
                    process_name, config, key, instance_name, target_release
                )
            return self.update_check_emby_latest(
                process_name, config, key, instance_name
            )
        if key in [
            "sonarr",
            "radarr",
            "lidarr",
            "prowlarr",
            "readarr",
            "whisparr",
            "whisparr-v3",
        ]:
            release_enabled = config.get("release_version_enabled")
            branch_enabled = config.get("branch_enabled")
            repo_owner = config.get("repo_owner")
            repo_name = config.get("repo_name")
            has_repo = repo_owner and repo_name

            if branch_enabled:
                self.logger.warning(
                    "%s has 'branch_enabled' set, but branch builds are disabled for arr services. "
                    "Set 'release_version_enabled' instead.",
                    process_name,
                )
                branch_enabled = False

            # Check for conflicting flags - release_version_enabled takes priority
            if release_enabled and branch_enabled:
                self.logger.warning(
                    "%s has both 'release_version_enabled' and 'branch_enabled' set. "
                    "Using 'release_version_enabled'.",
                    process_name,
                )
                branch_enabled = False

            # Use GitHub for release_version_enabled OR branch_enabled (both need GitHub)
            use_github = has_repo and (release_enabled or branch_enabled)
            if use_github:
                # Fall through to the generic repo-based update flow below
                pass
            else:
                pinned_version = config.get("pinned_version")
                if pinned_version:
                    return self.update_check_pinned_version(
                        process_name,
                        config,
                        key,
                        instance_name,
                        pinned_version,
                    )
                return self.update_check_arr_latest(
                    process_name, config, key, instance_name
                )

        if config.get("release_version_enabled"):
            release_value = (config.get("release_version") or "").lower()
            if "nightly" in release_value:
                nightly = True
                prerelease = False
                self.logger.info(f"Checking for nightly updates for {process_name}.")
            elif "prerelease" in release_value:
                nightly = False
                prerelease = True
                self.logger.info(f"Checking for prerelease updates for {process_name}.")
            else:
                nightly = False
                prerelease = False
                self.logger.info(f"Checking for stable updates for {process_name}.")
        else:
            nightly = False
            prerelease = False
            self.logger.info(f"Checking for stable updates for {process_name}.")

        versions = Versions()
        try:
            repo_owner = config["repo_owner"]
            repo_name = config["repo_name"]
            update_needed, update_info = versions.compare_versions(
                process_name,
                repo_owner,
                repo_name,
                instance_name,
                key,
                nightly=nightly,
                prerelease=prerelease,
            )

            if not update_needed:
                return False, f"{update_info.get('message')} for {process_name}."

            self.logger.info(
                f"Updating {process_name} from {update_info.get('current_version')} to {update_info.get('latest_version')}."
            )
            if process_name in self.process_handler.process_names:
                self.stop_process(process_name)
            with self.process_handler.setup_tracker_lock:
                if process_name in self.process_handler.setup_tracker:
                    self.process_handler.setup_tracker.remove(process_name)
            release_version = f"{update_info.get('latest_version')}"
            if not prerelease and not nightly:
                config["release_version"] = release_version
                self.logger.info(
                    f"Updating {process_name} config to {release_version}."
                )
            success, error = setup_release_version(
                self.process_handler, config, process_name, key
            )
            if not success:
                return (
                    False,
                    f"Failed to update {process_name} to {release_version}: {error}",
                )
            success, error = setup_project(self.process_handler, process_name)
            if not success:
                return (
                    False,
                    f"Failed to update {process_name} to {release_version}: {error}",
                )
            self.start_process(process_name, config, key, instance_name)
            return True, f"Updated {process_name} to {release_version}."

        except Exception as e:
            return False, f"Update check failed for {process_name}: {e}"

    def update_check_pinned_version(
        self, process_name, config, key, instance_name, target_version
    ):
        if not target_version:
            return False, f"No updates available for {process_name}."

        versions = Versions()
        install_dir = config.get("install_dir")
        if install_dir and key in (
            "sonarr",
            "radarr",
            "lidarr",
            "prowlarr",
            "readarr",
            "whisparr",
            "whisparr-v3",
        ):
            current_version, error = versions.read_arr_version_from_dir(
                key, install_dir
            )
        else:
            current_version, error = versions.version_check(
                process_name, instance_name, key
            )
        self.logger.info(
            f"{process_name} pinned version: {target_version} (current: {current_version or 'unknown'})."
        )
        if current_version == target_version:
            return False, f"No updates available for {process_name}."
        if not current_version:
            self.logger.warning(
                f"Failed to read current version for {process_name}: {error}"
            )

        self.logger.info(
            f"Updating {process_name} from {current_version or 'unknown'} to {target_version}."
        )
        if process_name in self.process_handler.process_names:
            self.stop_process(process_name)
        with self.process_handler.setup_tracker_lock:
            if process_name in self.process_handler.setup_tracker:
                self.process_handler.setup_tracker.remove(process_name)

        success, error = setup_project(self.process_handler, process_name)
        if not success:
            return (
                False,
                f"Failed to update {process_name} to {target_version}: {error}",
            )

        self.start_process(process_name, config, key, instance_name)
        return True, f"Updated {process_name} to {target_version}."

    def update_check_jellyfin_latest(self, process_name, config, key, instance_name):
        jellyfin_service_path = "/usr/lib/jellyfin/bin/jellyfin"
        if not os.path.exists(jellyfin_service_path):
            self.logger.info(
                f"{process_name} not installed yet; deferring install to setup."
            )
            return False, f"No updates available for {process_name}."

        versions = Versions()
        current_version, error = versions.version_check(
            process_name, instance_name, key
        )
        latest_version, latest_error = self.get_jellyfin_latest_version()
        if not latest_version:
            return False, f"Failed to get latest Jellyfin version: {latest_error}"
        self.logger.info(
            f"Jellyfin latest version: {latest_version} (current: {current_version or 'unknown'})."
        )
        if current_version == latest_version:
            return False, f"No updates available for {process_name}."

        self.logger.info(
            f"Updating {process_name} from {current_version or 'unknown'} to {latest_version}."
        )
        if process_name in self.process_handler.process_names:
            self.stop_process(process_name)
        with self.process_handler.setup_tracker_lock:
            if process_name in self.process_handler.setup_tracker:
                self.process_handler.setup_tracker.remove(process_name)

        installer = JellyfinInstaller()
        success, error = installer.install_jellyfin_server()
        if not success:
            return (
                False,
                f"Failed to update {process_name} to {latest_version}: {error}",
            )

        success, error = setup_project(self.process_handler, process_name)
        if not success:
            return (
                False,
                f"Failed to update {process_name} to {latest_version}: {error}",
            )

        self.start_process(process_name, config, key, instance_name)
        return True, f"Updated {process_name} to {latest_version}."

    def update_check_emby_latest(self, process_name, config, key, instance_name):
        versions = Versions()
        current_version, error = versions.version_check(
            process_name, instance_name, key
        )
        latest_version, latest_error = self.get_emby_latest_version(config)
        if not latest_version:
            return False, f"Failed to get latest Emby version: {latest_error}"
        self.logger.info(
            f"Emby latest version: {latest_version} (current: {current_version or 'unknown'})."
        )
        if current_version == latest_version:
            return False, f"No updates available for {process_name}."

        self.logger.info(
            f"Updating {process_name} from {current_version or 'unknown'} to {latest_version}."
        )
        if process_name in self.process_handler.process_names:
            self.stop_process(process_name)
        with self.process_handler.setup_tracker_lock:
            if process_name in self.process_handler.setup_tracker:
                self.process_handler.setup_tracker.remove(process_name)

        original_release_enabled = config.get("release_version_enabled")
        original_release_version = config.get("release_version")
        config["release_version_enabled"] = True
        config["release_version"] = latest_version
        try:
            success, error = setup_project(self.process_handler, process_name)
            if not success:
                return (
                    False,
                    f"Failed to update {process_name} to {latest_version}: {error}",
                )
        finally:
            config["release_version_enabled"] = original_release_enabled
            config["release_version"] = original_release_version

        self.start_process(process_name, config, key, instance_name)
        return True, f"Updated {process_name} to {latest_version}."

    def update_check_arr_latest(self, process_name, config, key, instance_name):
        versions = Versions()
        install_dir = config.get("install_dir")
        if install_dir:
            current_version, error = versions.read_arr_version_from_dir(
                key, install_dir
            )
        else:
            current_version, error = versions.version_check(
                process_name, instance_name, key
            )
        installer = ArrInstaller(
            key,
            install_dir=install_dir,
            branch=config.get("branch"),
            repo_owner=config.get("repo_owner"),
            repo_name=config.get("repo_name"),
        )
        latest_version, latest_error = installer.get_latest_version()
        if not latest_version:
            return False, f"Failed to get latest {key} version: {latest_error}"
        self.logger.info(
            f"{key.capitalize()} latest version: {latest_version} (current: {current_version or 'unknown'})."
        )
        if current_version == latest_version:
            return False, f"No updates available for {process_name}."

        self.logger.info(
            f"Updating {process_name} from {current_version or 'unknown'} to {latest_version}."
        )
        if process_name in self.process_handler.process_names:
            self.stop_process(process_name)
        with self.process_handler.setup_tracker_lock:
            if process_name in self.process_handler.setup_tracker:
                self.process_handler.setup_tracker.remove(process_name)

        success, error = installer.install(force=True)
        if not success:
            return (
                False,
                f"Failed to update {process_name} to {latest_version}: {error}",
            )

        success, error = setup_project(self.process_handler, process_name)
        if not success:
            return (
                False,
                f"Failed to update {process_name} to {latest_version}: {error}",
            )

        self.start_process(process_name, config, key, instance_name)
        return True, f"Updated {process_name} to {latest_version}."

    def get_jellyfin_latest_version(self):
        try:
            result = subprocess.run(
                ["apt-cache", "policy", "jellyfin"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("Candidate:"):
                        candidate = line.split(":", 1)[1].strip()
                        if candidate and candidate != "(none)":
                            return candidate, None
        except Exception as e:
            return None, str(e)
        return None, "Candidate version not found"

    def get_emby_latest_version(self, config):
        try:
            repo_owner = config.get("repo_owner")
            repo_name = config.get("repo_name")
            if not repo_owner or not repo_name:
                return None, "Emby repo owner/name not configured"
            downloader = Versions().downloader
            latest_release_version, error = downloader.get_latest_release(
                repo_owner, repo_name, nightly=False
            )
            if not latest_release_version:
                return None, error
            return latest_release_version, None
        except Exception as e:
            return None, str(e)

    def update_check_plex(self, process_name, config, key, instance_name):
        installer = PlexInstaller()
        plex_media_server_dir = config.get(
            "plex_media_server_dir", "/usr/lib/plexmediaserver"
        )
        if not os.path.exists(plex_media_server_dir):
            pinned_version = installer.normalize_version(config.get("pinned_version"))
            install_label = pinned_version or "latest"
            self.logger.info(
                f"Plex Media Server not found; installing {install_label} for {process_name}."
            )
            success, error = installer.install_plex_media_server(version=pinned_version)
            if not success:
                return (
                    False,
                    f"Failed to install {process_name} ({install_label}): {error}",
                )

            success, error = setup_project(self.process_handler, process_name)
            if not success:
                return (
                    False,
                    f"Failed to install {process_name} ({install_label}): {error}",
                )

            self.start_process(process_name, config, key, instance_name)
            return True, f"Installed {process_name} ({install_label})."

        update_needed, update_info = installer.check_for_update(
            process_name, instance_name
        )
        if not update_needed:
            return False, f"{update_info} for {process_name}."

        pinned_version = config.get("pinned_version")
        if pinned_version:
            if update_info.get("current_version") == pinned_version:
                return False, f"Plex pinned to {pinned_version} for {process_name}."
            if update_info.get("latest_version") != pinned_version:
                return (
                    False,
                    f"Plex pinned to {pinned_version}; latest is {update_info.get('latest_version')} for {process_name}.",
                )

        self.logger.info(
            f"Updating {process_name} from {update_info.get('current_version')} to {update_info.get('latest_version')}."
        )
        if process_name in self.process_handler.process_names:
            self.stop_process(process_name)
        with self.process_handler.setup_tracker_lock:
            if process_name in self.process_handler.setup_tracker:
                self.process_handler.setup_tracker.remove(process_name)

        success, error = installer.install_plex_media_server()
        if not success:
            return (
                False,
                f"Failed to update {process_name} to {update_info.get('latest_version')}: {error}",
            )

        success, error = setup_project(self.process_handler, process_name)
        if not success:
            return (
                False,
                f"Failed to update {process_name} to {update_info.get('latest_version')}: {error}",
            )

        self.start_process(process_name, config, key, instance_name)
        return True, f"Updated {process_name} to {update_info.get('latest_version')}."

    def stop_process(self, process_name):
        self.process_handler.stop_process(process_name)

    def start_process(self, process_name, config, key, instance_name):
        refreshed_key, refreshed_instance = CONFIG_MANAGER.find_key_for_process(
            process_name
        )
        if refreshed_key:
            config = (
                CONFIG_MANAGER.get_instance(refreshed_instance, refreshed_key) or config
            )
            key = refreshed_key
            instance_name = refreshed_instance

        if config.get("wait_for_dir", False):
            sleep_s = 10
            while not os.path.exists(wait_dir := config["wait_for_dir"]):
                if self.process_handler.shutting_down:
                    self.logger.info(
                        "Shutdown requested; skipping wait for directory %s.",
                        wait_dir,
                    )
                    return False, "Shutdown requested"
                self.logger.info(
                    f"Waiting for directory {wait_dir} to become available before starting {process_name}"
                )
                time.sleep(sleep_s)
                sleep_s = min(60, int(sleep_s * 1.5))

        wait_mounts = config.get("wait_for_mounts") or []
        if wait_mounts:
            sleep_s = 10
            while True:
                if self.process_handler.shutting_down:
                    self.logger.info(
                        "Shutdown requested; skipping wait for mounts before %s.",
                        process_name,
                    )
                    return False, "Shutdown requested"
                missing = [
                    mount_path
                    for mount_path in wait_mounts
                    if not os.path.ismount(mount_path)
                ]
                if not missing:
                    break
                self.logger.info(
                    "Waiting for mounts to become available before starting %s: %s",
                    process_name,
                    ", ".join(missing),
                )
                time.sleep(sleep_s)
                sleep_s = min(60, int(sleep_s * 1.5))

        if config.get("wait_for_url", False):
            wait_for_urls = config["wait_for_url"]
            time.sleep(5)
            start_time = time.time()

            for wait_entry in wait_for_urls:
                wait_url = wait_entry["url"]
                auth = wait_entry.get("auth", None)

                logger.info(
                    f"Waiting to start {process_name} until {wait_url} is accessible."
                )

                sleep_s = 5
                while time.time() - start_time < 600:
                    if self.process_handler.shutting_down:
                        self.logger.info(
                            "Shutdown requested; skipping wait for %s.",
                            wait_url,
                        )
                        return False, "Shutdown requested"
                    try:
                        if auth:
                            response = requests.get(
                                wait_url, auth=(auth["user"], auth["password"])
                            )
                            # logger.debug(
                            #    f"Authenticating to {wait_url} with {auth['user']}:{auth['password']}"
                            # )
                        else:
                            response = requests.get(wait_url)

                        if 200 <= response.status_code < 300:
                            logger.info(
                                f"{wait_url} is accessible with {response.status_code}."
                            )
                            break
                        else:
                            logger.debug(
                                f"Received status code {response.status_code} while waiting for {wait_url} to be accessible."
                            )
                    except requests.RequestException as e:
                        logger.debug(f"Waiting for {wait_url}: {e}")
                    time.sleep(sleep_s)
                    sleep_s = min(60, int(sleep_s * 1.5))
                else:
                    raise RuntimeError(
                        f"Timeout: {wait_url} is not accessible after 600 seconds."
                    )

        command = config["command"]
        config_dir = config["config_dir"]

        if config.get("suppress_logging", False):
            self.logger.info(f"Suppressing {process_name} logging")
            suppress_logging = True
        else:
            suppress_logging = False

        if key == "riven_backend":
            if not os.path.exists(os.path.join(config_dir, "data", "settings.json")):
                from utils.riven_settings import set_env_variables

                logger.info("Riven initial setup for first run")
                threading.Thread(target=set_env_variables).start()

        env = os.environ.copy()
        env.update(config.get("env", {}))

        process, error = self.process_handler.start_process(
            process_name,
            config_dir,
            command,
            instance_name,
            suppress_logging=suppress_logging,
            env=env,
        )
        if self.process_handler.shutting_down:
            return process, "Shutdown requested"
        if key == "riven_backend":
            from utils.riven_settings import load_settings

            time.sleep(10)
            load_settings()

        if key == "decypharr":
            if self.process_handler.shutting_down:
                return process, "Shutdown requested"
            from utils.decypharr_settings import patch_decypharr_config

            time.sleep(10)
            patched, error = patch_decypharr_config()
            if patched:
                self.logger.info("Restarting Decypharr to apply new config")
                self.process_handler.stop_process(process_name)
                self.process_handler.start_process(
                    process_name,
                    config_dir,
                    command,
                    instance_name,
                    suppress_logging=suppress_logging,
                    env=env,
                )
            elif error:
                self.logger.warning("Decypharr config patch failed: %s", error)

        if key == "nzbdav":
            if self.process_handler.shutting_down:
                return process, "Shutdown requested"
            from utils.nzbdav_settings import patch_nzbdav_config

            time.sleep(10)
            patched, error = patch_nzbdav_config()
            if patched:
                self.logger.info("Restarting NzbDAV to apply new config")
                self.process_handler.stop_process(process_name)
                self.process_handler.start_process(
                    process_name,
                    config_dir,
                    command,
                    instance_name,
                    suppress_logging=suppress_logging,
                    env=env,
                )
            elif error:
                self.logger.warning("NzbDAV config patch failed: %s", error)

        if key in [
            "prowlarr",
            "sonarr",
            "radarr",
            "lidarr",
            "readarr",
            "whisparr",
            "whisparr-v3",
        ]:
            if self.process_handler.shutting_down:
                return process, "Shutdown requested"
            prowlarr_cfg = CONFIG_MANAGER.get("prowlarr") or {}
            if isinstance(prowlarr_cfg.get("instances"), dict):
                prowlarr_enabled = any(
                    isinstance(inst, dict) and inst.get("enabled")
                    for inst in prowlarr_cfg["instances"].values()
                )
            else:
                prowlarr_enabled = bool(prowlarr_cfg.get("enabled"))
            if not prowlarr_enabled:
                return process, None

            from utils.prowlarr_settings import patch_prowlarr_apps

            time.sleep(10)
            ok, err = patch_prowlarr_apps()
            if not ok and err:
                self.logger.warning("Prowlarr app sync failed: %s", err)

        if key == "plex":
            if self.process_handler.shutting_down:
                return process, "Shutdown requested"
            from utils.plex_settings import patch_plex_config

            time.sleep(10)
            patched, error = patch_plex_config()
            if patched:
                self.logger.info("Restarting Plex to apply new config")
                self.process_handler.stop_process(process_name)
                self.process_handler.start_process(
                    process_name,
                    config_dir,
                    command,
                    instance_name,
                    suppress_logging=suppress_logging,
                    env=env,
                )

        return process, error
