from utils.global_logger import logger
from utils.logger import format_time
from utils.versions import Versions, display_version
from utils.download import Downloader
from utils.setup import (
    setup_project,
    setup_release_version,
    setup_branch_version,
    configure_project,
)
from utils.plex import PlexInstaller
from utils.arr import ArrInstaller
from utils.jellyfin import JellyfinInstaller
from utils.config_loader import CONFIG_MANAGER
from utils.mediastorm_installer import (
    MediaStormInstallError,
    mediastorm_install_selector,
    mediastorm_runtime_matches_selection,
)
from utils.wait_for_url import wait_for_urls
from utils.install_cache import INSTALL_CACHE
from utils.transactional_install import RuntimeRollbackSnapshot
from utils.transactional_install import DirectoryReleaseTransaction
from datetime import datetime
from glob import glob
import threading, time, os, schedule, subprocess, shutil


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
        self.downloader = Downloader()
        self._rollback_snapshots = {}
        self._active_install_operation = None
        self._update_timing_lock = threading.Lock()
        self._update_timings = {}

        if not Update._scheduler_initialized:
            self.scheduler = schedule.Scheduler()
            Update._scheduler_initialized = True
        else:
            self.scheduler = schedule.default_scheduler

    def _fetch_branch_head_sha(self, repo_owner: str, repo_name: str, branch: str):
        return self.downloader.get_ref_commit_sha(repo_owner, repo_name, branch)

    def _resolve_nzbdav_release_marker(self, config):
        release = str(config.get("release_version") or "").strip()
        if not release:
            return None, "NzbDAV release tag is required."
        sha, error = self.downloader.get_ref_commit_sha(
            config.get("repo_owner"), config.get("repo_name"), release
        )
        if not sha:
            return None, error or "Failed to resolve NzbDAV release tag commit SHA."
        return f"{release}-{sha[:8]}", None

    def supports_manual_update(self, key, config):
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

    @staticmethod
    def _is_nzbdav_named_release_channel(key, config):
        if key != "nzbdav" or not config.get("release_version_enabled"):
            return False
        release = str(config.get("release_version") or "").strip().lower()
        if not release or release in {"latest", "nightly", "prerelease"}:
            return False
        return not any(character.isdigit() for character in release)

    def _get_update_block_reason(self, config, key=None):
        if config.get("pinned_version"):
            return "pinned_version"
        if str(config.get("commit_sha") or "").strip():
            return "commit"
        if config.get("branch_enabled"):
            return "branch"
        if config.get("release_version_enabled"):
            if self._is_nzbdav_named_release_channel(key, config):
                return None
            if str(config.get("release_version") or "").strip().lower() == "latest":
                return None
            if not self._release_is_nightly_or_prerelease(config):
                return "release"
        return None

    @staticmethod
    def _configured_versions_match(current_version, configured_version):
        current = str(current_version or "").strip().lower()
        configured = str(configured_version or "").strip().lower()
        if not current or not configured:
            return False
        if current == configured:
            return True
        return current.removeprefix("v") == configured.removeprefix("v")

    @staticmethod
    def _configured_target_label(config, block_reason):
        if block_reason == "commit":
            commit_sha = str(config.get("commit_sha") or "").strip().lower()
            return f"commit {commit_sha[:12]}"
        if block_reason == "branch":
            branch = str(config.get("branch") or "main").strip() or "main"
            return f"branch {branch}"
        if block_reason == "release":
            release = str(config.get("release_version") or "latest").strip() or "latest"
            return f"release {release}"
        if block_reason == "pinned_version":
            version = str(config.get("pinned_version") or "").strip()
            return f"version {version}"
        return str(block_reason or "target").replace("_", " ")

    def _immutable_artifact_identity(self, config, hint):
        """Resolve cache identity to a commit when the source can move."""
        commit_sha = str(config.get("commit_sha") or "").strip().lower()
        if len(commit_sha) == 40:
            return f"commit:{commit_sha}"
        if config.get("branch_enabled"):
            reference = str(config.get("branch") or "main").strip() or "main"
        elif config.get("release_version_enabled"):
            reference = (
                str(config.get("release_version") or "latest").strip() or "latest"
            )
        else:
            reference = str(hint or "latest").strip() or "latest"
        owner = str(config.get("repo_owner") or "").strip()
        repository = str(config.get("repo_name") or "").strip()
        if owner and repository:
            sha, _ = self.downloader.get_ref_commit_sha(owner, repository, reference)
            if sha:
                return f"git:{owner}/{repository}:{reference}:{sha.lower()}"
        # A moving ref must never share a build artifact when its immutable
        # revision could not be established.
        return f"unresolved:{reference}:{time.time_ns()}"

    def _ensure_update_timing_state(self):
        # A few focused unit tests construct Update without calling __init__.
        # Keep the timing helpers safe for those instances as well as normal
        # runtime construction.
        if not hasattr(self, "_update_timing_lock"):
            self._update_timing_lock = threading.Lock()
        if not hasattr(self, "_update_timings"):
            self._update_timings = {}

    def _begin_update_timing(self, process_name):
        self._ensure_update_timing_state()
        with self._update_timing_lock:
            if process_name in self._update_timings:
                return False
            self._update_timings[process_name] = {
                "started_at": time.monotonic(),
                "downtime_started_at": None,
                "downtime_seconds": 0.0,
                "downtime_observed": False,
                "pending_payload": None,
            }
        return True

    def _mark_update_downtime_started(self, process_name):
        self._ensure_update_timing_state()
        with self._update_timing_lock:
            timing = self._update_timings.get(process_name)
            if timing is None or timing["downtime_started_at"] is not None:
                return
            timing["downtime_started_at"] = time.monotonic()
            timing["downtime_observed"] = True

    def _mark_update_service_ready(self, process_name):
        self._ensure_update_timing_state()
        with self._update_timing_lock:
            timing = self._update_timings.get(process_name)
            if timing is None or timing["downtime_started_at"] is None:
                return
            timing["downtime_seconds"] += max(
                0.0, time.monotonic() - timing["downtime_started_at"]
            )
            timing["downtime_started_at"] = None

    @staticmethod
    def _update_timing_metrics(timing, now):
        downtime_seconds = float(timing.get("downtime_seconds") or 0.0)
        downtime_started_at = timing.get("downtime_started_at")
        if downtime_started_at is not None:
            downtime_seconds += max(0.0, now - downtime_started_at)
        if not timing.get("downtime_observed"):
            downtime_status = "not_observed"
        elif downtime_started_at is not None:
            downtime_status = "ongoing"
        else:
            downtime_status = "completed"
        return {
            "install_duration_seconds": round(max(0.0, now - timing["started_at"]), 3),
            "downtime_seconds": round(downtime_seconds, 3),
            "downtime_status": downtime_status,
            "timing_completed_at": datetime.now().astimezone().isoformat(),
        }

    def _finish_update_timing(self, process_name, payload=None):
        self._ensure_update_timing_state()
        with self._update_timing_lock:
            timing = self._update_timings.pop(process_name, None)
        if timing is None:
            return {}
        metrics = self._update_timing_metrics(timing, time.monotonic())
        final_payload = payload or timing.get("pending_payload")
        if isinstance(final_payload, dict):
            final_payload.update(metrics)
            self._write_update_status(process_name, final_payload)
        return metrics

    def _write_update_status(self, process_name, payload):
        try:
            from utils.dependencies import get_api_state

            api_state = get_api_state()
            if api_state:
                api_state.set_update_status(process_name, payload)
        except Exception:
            return

    def _safe_record_update_status(self, process_name, payload):
        self._ensure_update_timing_state()
        with self._update_timing_lock:
            timing = self._update_timings.get(process_name)
            if timing is not None:
                timing["pending_payload"] = payload
                return
        self._write_update_status(process_name, payload)

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
        default_interval = 168
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
            payload = {
                "status": "error",
                "reason": "config_not_found",
                "message": f"Configuration for {process_name} not found.",
            }
            self._safe_record_update_status(process_name, payload)
            return payload
        if not self.supports_manual_update(key, config):
            payload = {
                "status": "unsupported",
                "reason": "unsupported",
                "message": f"Manual updates are not supported for {process_name}.",
            }
            self._safe_record_update_status(process_name, payload)
            return payload

        with self.updating:
            payload = self._manual_update_check_internal(
                process_name, config, key, instance_name
            )
            self._safe_record_update_status(process_name, payload)
            return payload

    def _manual_update_check_internal(self, process_name, config, key, instance_name):
        block_reason = self._get_update_block_reason(config, key)
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

        commit_sha = str(config.get("commit_sha") or "").strip().lower()
        if commit_sha:
            current_version, _ = versions.version_check(
                process_name, instance_name, key
            )
            configured_version = f"commit-{commit_sha[:12]}"
            return {
                "status": "blocked",
                "reason": "commit",
                "message": (
                    f"{process_name} is pinned to commit {commit_sha[:12]}. "
                    "Change or clear commit_sha to select another source revision."
                ),
                "current_version": current_version or "unknown",
                "available_version": configured_version,
                "configured_target_kind": "commit",
                "configured_target_installed": current_version == configured_version,
                "checked_at": checked_at,
                "auto_update_enabled": False,
                "auto_update_interval": interval_hours,
                "auto_update_start_time": start_time,
                "next_check_at": None,
            }

        release_value = str(config.get("release_version") or "").strip()
        named_release_channel = self._is_nzbdav_named_release_channel(key, config)
        if (
            (block_reason == "release" or named_release_channel)
            and release_value
            and release_value.lower() != "latest"
        ):
            release_is_blocked = block_reason == "release"
            current_version, current_error = versions.version_check(
                process_name, instance_name, key
            )
            if not current_version:
                return {
                    "status": "error",
                    "reason": "version_check_failed",
                    "message": current_error,
                    "checked_at": checked_at,
                    "auto_update_enabled": (
                        False if release_is_blocked else auto_update_enabled
                    ),
                    "auto_update_interval": interval_hours,
                    "auto_update_start_time": start_time,
                    "next_check_at": None if release_is_blocked else next_check_at,
                }
            available_version = release_value
            if key == "nzbdav":
                available_version, ref_error = self._resolve_nzbdav_release_marker(
                    config
                )
                if not available_version:
                    return {
                        "status": "error",
                        "reason": "version_check_failed",
                        "message": ref_error,
                        "current_version": current_version,
                        "checked_at": checked_at,
                        "auto_update_enabled": auto_update_enabled,
                        "auto_update_interval": interval_hours,
                        "auto_update_start_time": start_time,
                        "next_check_at": next_check_at,
                    }
                configured_target_installed = current_version == available_version
                display_current_version = display_version(key, current_version)
                display_available_version = display_version(key, available_version)
            else:
                configured_target_installed = self._configured_versions_match(
                    current_version, release_value
                )
                display_current_version = current_version
                display_available_version = available_version
            return {
                "status": (
                    "no_update"
                    if configured_target_installed
                    else "blocked" if release_is_blocked else "update_available"
                ),
                "reason": "release" if release_is_blocked else None,
                "message": (
                    "Configured release is installed"
                    if configured_target_installed
                    else (
                        "Configured release is ready to install"
                        if release_is_blocked
                        else "Release channel update is ready to install"
                    )
                ),
                "current_version": display_current_version,
                "available_version": display_available_version,
                "configured_target_kind": "release",
                "configured_target_installed": configured_target_installed,
                "checked_at": checked_at,
                "auto_update_enabled": (
                    False if release_is_blocked else auto_update_enabled
                ),
                "auto_update_interval": interval_hours,
                "auto_update_start_time": start_time,
                "next_check_at": None if release_is_blocked else next_check_at,
            }

        branch_enabled = bool(config.get("branch_enabled")) and key in {
            "decypharr",
            "nzbdav",
            "neutarr",
        }
        if branch_enabled:
            current_version, current_error = versions.version_check(
                process_name, instance_name, key
            )
            if not current_version:
                return {
                    "status": "error",
                    "reason": "version_check_failed",
                    "message": current_error,
                    "checked_at": checked_at,
                    "auto_update_enabled": auto_update_enabled,
                    "auto_update_interval": interval_hours,
                    "auto_update_start_time": start_time,
                    "next_check_at": next_check_at,
                }
            branch_name = (config.get("branch") or "main").strip() or "main"
            head_sha, head_error = self._fetch_branch_head_sha(
                repo_owner, repo_name, branch_name
            )
            if not head_sha:
                return {
                    "status": "error",
                    "reason": "version_check_failed",
                    "message": head_error or "Failed to resolve branch head SHA.",
                    "checked_at": checked_at,
                    "auto_update_enabled": auto_update_enabled,
                    "auto_update_interval": interval_hours,
                    "auto_update_start_time": start_time,
                    "next_check_at": next_check_at,
                }

            available_version = f"{branch_name}-{head_sha[:8]}"
            if current_version == available_version:
                return {
                    "status": "no_update",
                    "current_version": current_version,
                    "available_version": available_version,
                    "message": "No updates available",
                    "configured_target_kind": "branch",
                    "configured_target_installed": True,
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
                "available_version": available_version,
                "reason": block_reason,
                "message": "Branch update available",
                "configured_target_kind": "branch",
                "configured_target_installed": False,
                "checked_at": checked_at,
                "auto_update_enabled": auto_update_enabled,
                "auto_update_interval": interval_hours,
                "auto_update_start_time": start_time,
                "next_check_at": next_check_at,
            }

        nightly = False
        prerelease = False
        release_value = (config.get("release_version") or "").lower()
        if config.get("release_version_enabled"):
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
            payload = {
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
            if block_reason == "release":
                payload.update(
                    {
                        "reason": "release",
                        "configured_target_kind": "release",
                        "configured_target_installed": True,
                    }
                )
            return payload

        status = "update_available"
        if block_reason:
            status = "blocked"

        payload = {
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
        if block_reason == "release":
            payload.update(
                {
                    "configured_target_kind": "release",
                    "configured_target_installed": False,
                }
            )
        return payload

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

    def manual_update_install(
        self,
        process_name,
        allow_override=False,
        target=None,
        protection_override=None,
    ):
        key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
        config = CONFIG_MANAGER.get_instance(instance_name, key) if key else None
        requested_target = str(target or "").strip().lower()
        block_reason = self._get_update_block_reason(config, key) if config else None
        request_is_actionable = bool(
            config
            and self.supports_manual_update(key, config)
            and not (
                block_reason and not allow_override and requested_target != "configured"
            )
            and not (requested_target == "configured" and not block_reason)
        )
        if not request_is_actionable:
            return self._manual_update_install_unprotected(
                process_name, allow_override, target
            )
        protection = None
        manager = getattr(self, "media_protection_manager", None)
        if manager is not None:
            protection = manager.begin_planned(
                process_name, "update", protection_override
            )
            if protection["status"] == "deferred":
                payload = {
                    "status": "protection_required",
                    "message": "Update deferred by media library protection.",
                    "media_protection": protection["preflight"],
                }
                self._safe_record_update_status(process_name, payload)
                return payload
        success = False
        operation_id = INSTALL_CACHE.begin_operation(process_name)
        self._active_install_operation = operation_id
        timing_started = self._begin_update_timing(process_name)
        payload = None
        try:
            INSTALL_CACHE.update_operation(operation_id, stage="preflight")
            payload = self._manual_update_install_unprotected(
                process_name, allow_override, target
            )
            success = payload.get("status") in {"updated", "no_update"}
            INSTALL_CACHE.update_operation(
                operation_id,
                stage="complete" if success else "failed",
                status="completed" if success else "failed",
                message=str(payload.get("message") or "")[:1000],
            )
            if protection is not None:
                payload["media_protection"] = protection
            return payload
        finally:
            if not success:
                self._recover_pending_snapshot(process_name)
            if timing_started:
                self._finish_update_timing(process_name, payload)
            self._active_install_operation = None
            if manager is not None and protection is not None:
                manager.complete_planned(protection.get("token"), success=success)

    def _manual_update_install_unprotected(
        self, process_name, allow_override=False, target=None
    ):
        key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
        requested_target = str(target or "").strip().lower()
        apply_configured_target = requested_target == "configured"
        self.logger.info(
            "Manual update install requested for %s (override=%s, target=%s).",
            process_name,
            bool(allow_override),
            target or "default",
        )
        with self.updating:
            # Resolve the live configuration after acquiring the update lock. A
            # queued manual update must not act on source settings captured
            # before an earlier install completed.
            config = CONFIG_MANAGER.get_instance(instance_name, key)
            if not config:
                payload = {
                    "status": "error",
                    "reason": "config_not_found",
                    "message": f"Configuration for {process_name} not found.",
                }
                self._safe_record_update_status(process_name, payload)
                return payload
            if not self.supports_manual_update(key, config):
                payload = {
                    "status": "unsupported",
                    "reason": "unsupported",
                    "message": f"Manual updates are not supported for {process_name}.",
                }
                self._safe_record_update_status(process_name, payload)
                return payload

            block_reason = self._get_update_block_reason(config, key)
            if apply_configured_target and not block_reason:
                payload = {
                    "status": "error",
                    "reason": "configured_target_missing",
                    "message": f"No configured pinned source target for {process_name}.",
                }
                self._safe_record_update_status(process_name, payload)
                return payload
            if block_reason and not allow_override and not apply_configured_target:
                payload = {
                    "status": "blocked",
                    "reason": block_reason,
                    "message": f"Updates blocked for {process_name}.",
                }
                self._safe_record_update_status(process_name, payload)
                return payload

            original = {
                "pinned_version": config.get("pinned_version"),
                "commit_sha": config.get("commit_sha"),
                "release_version_enabled": config.get("release_version_enabled"),
                "release_version": config.get("release_version"),
                "branch_enabled": config.get("branch_enabled"),
                "branch": config.get("branch"),
            }
            temporary_source_guard = {
                source_key: config.get(source_key)
                for source_key in (
                    "pinned_version",
                    "commit_sha",
                    "release_version_enabled",
                    "branch_enabled",
                    "branch",
                )
            }
            try:
                if apply_configured_target:
                    success, message = self._install_configured_target(
                        process_name,
                        config,
                        key,
                        instance_name,
                        block_reason,
                    )
                else:
                    if allow_override:
                        config["pinned_version"] = ""
                        config["commit_sha"] = ""
                        config["branch_enabled"] = False
                        config["release_version_enabled"] = False
                        config["release_version"] = ""
                        temporary_source_guard.update(
                            {
                                source_key: config.get(source_key)
                                for source_key in temporary_source_guard
                            }
                        )
                    if target:
                        target_value = str(target).lower()
                        if target_value in {"prerelease", "nightly"}:
                            config["release_version_enabled"] = True
                            config["release_version"] = target_value
                            temporary_source_guard["release_version_enabled"] = True

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
                source_changed_during_update = bool(
                    any(
                        config.get(source_key) != expected_value
                        for source_key, expected_value in temporary_source_guard.items()
                    )
                )
                if source_changed_during_update:
                    self.logger.info(
                        "Preserving newer source selection for %s saved during manual update.",
                        process_name,
                    )
                else:
                    config["pinned_version"] = original.get("pinned_version")
                    config["commit_sha"] = original.get("commit_sha")
                    config["release_version_enabled"] = original.get(
                        "release_version_enabled"
                    )
                    config["release_version"] = original.get("release_version")
                    config["branch_enabled"] = original.get("branch_enabled")
                    config["branch"] = original.get("branch")

    def _install_configured_target(
        self,
        process_name,
        config,
        key,
        instance_name,
        block_reason,
    ):
        if key == "traefik_proxy_admin":
            source_identity = self._configured_target_label(config, block_reason)

            def install_candidate():
                return setup_project(self.process_handler, process_name)

            return self._transactional_tpa_update(
                process_name,
                config,
                key,
                instance_name,
                source_identity,
                install_candidate,
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
                f"Failed to install configured target for {process_name}: {error}",
            )

        process, error = self.start_process(process_name, config, key, instance_name)
        if not process:
            return False, f"Failed to start {process_name}: {error}"

        target_label = self._configured_target_label(config, block_reason)
        return True, f"Installed configured {target_label} for {process_name}."

    def _transactional_tpa_update(
        self,
        process_name,
        config,
        key,
        instance_name,
        source_identity,
        installer,
    ):
        """Build TPA off-line, then atomically activate and health-check it."""
        original_dir = os.path.realpath(
            config.get("config_dir") or "/traefik-proxy-admin"
        )
        transaction = DirectoryReleaseTransaction(original_dir, process_name)
        operation_id = self._active_install_operation
        original_excludes = list(config.get("exclude_dirs") or [])
        original_env = dict(config.get("env") or {})
        candidate_dir = transaction.prepare()
        artifact_identity = self._immutable_artifact_identity(config, source_identity)
        toolchain = {}
        for name, command in (
            ("node", ["node", "--version"]),
            ("pnpm", ["pnpm", "--version"]),
        ):
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=10
                )
                toolchain[name] = (result.stdout or "unknown").strip()
            except (OSError, subprocess.SubprocessError):
                toolchain[name] = "unknown"
        build_key = INSTALL_CACHE.build_key(
            key,
            artifact_identity,
            inputs=[__file__, os.path.join(os.path.dirname(__file__), "setup.py")],
            toolchain=toolchain,
        )
        artifact_hit = False
        try:
            if operation_id:
                INSTALL_CACHE.update_operation(operation_id, stage="artifact_restore")
            artifact_hit, _ = INSTALL_CACHE.restore_artifact(
                key, build_key, candidate_dir
            )
            if artifact_hit and not (
                os.path.isfile(os.path.join(candidate_dir, "package.json"))
                and os.path.isfile(os.path.join(candidate_dir, ".next", "BUILD_ID"))
                and os.path.isfile(
                    os.path.join(candidate_dir, ".next", "standalone", "server.js")
                )
            ):
                artifact_hit = False
                shutil.rmtree(candidate_dir)
                os.makedirs(candidate_dir)

            if not artifact_hit:
                if operation_id:
                    INSTALL_CACHE.update_operation(
                        operation_id, stage="building", cache_misses=1
                    )
                transaction.mark_building()
                config["config_dir"] = candidate_dir
                config["exclude_dirs"] = [
                    str(value).replace(original_dir, candidate_dir, 1)
                    for value in original_excludes
                ]
                success, error = installer()
                if not success:
                    transaction.abandon(str(error))
                    return (
                        False,
                        f"Candidate build failed; existing runtime retained: {error}",
                    )
                required = (
                    os.path.join(candidate_dir, "package.json"),
                    os.path.join(candidate_dir, ".next", "BUILD_ID"),
                )
                if not all(
                    os.path.isfile(path) and os.path.getsize(path) > 0
                    for path in required
                ):
                    transaction.abandon("candidate verification failed")
                    return (
                        False,
                        "Candidate verification failed; existing runtime retained.",
                    )
                if os.path.isfile(
                    os.path.join(candidate_dir, ".next", "standalone", "server.js")
                ):
                    try:
                        INSTALL_CACHE.store_artifact(
                            key,
                            build_key,
                            candidate_dir,
                            excluded=("node_modules", "cache", ".git"),
                        )
                    except (OSError, ValueError) as error:
                        self.logger.warning(
                            "TPA artifact cache write failed safely: %s", error
                        )
            elif operation_id:
                INSTALL_CACHE.update_operation(
                    operation_id, stage="artifact_restored", cache_hits=1
                )
        finally:
            config["config_dir"] = original_dir
            config["exclude_dirs"] = original_excludes
            restored_env = dict(config.get("env") or original_env)
            config["env"] = {
                env_key: (
                    env_value.replace(candidate_dir, original_dir)
                    if isinstance(env_value, str)
                    else env_value
                )
                for env_key, env_value in restored_env.items()
            }
            CONFIG_MANAGER.save_config(process_name)

        try:
            if operation_id:
                INSTALL_CACHE.update_operation(operation_id, stage="activating")
            if process_name in self.process_handler.process_names:
                self._mark_update_downtime_started(process_name)
                self.process_handler.stop_process(process_name)
            transaction.activate()
            with self.process_handler.setup_tracker_lock:
                self.process_handler.setup_tracker.discard(process_name)
            configured, error = configure_project(self.process_handler, process_name)
            if not configured:
                raise RuntimeError(f"candidate configuration failed: {error}")
            process, error = self.start_process(
                process_name, config, key, instance_name
            )
            if not process:
                raise RuntimeError(error or "candidate process failed to start")
            healthy, health_error = self._wait_for_update_health(process_name)
            if not healthy:
                raise RuntimeError(
                    f"candidate failed health stabilization: {health_error}"
                )
            transaction.commit()
            return (
                True,
                f"Updated {process_name} transactionally ({artifact_identity}).",
            )
        except Exception as error:
            self._mark_update_downtime_started(process_name)
            self.process_handler.stop_process(process_name)
            rolled_back = transaction.rollback()
            if operation_id:
                INSTALL_CACHE.update_operation(
                    operation_id,
                    stage="rolled_back" if rolled_back else "rollback_failed",
                    rollback_performed=1,
                )
            if rolled_back:
                with self.process_handler.setup_tracker_lock:
                    self.process_handler.setup_tracker.discard(process_name)
                configure_project(self.process_handler, process_name)
                self.start_process(process_name, config, key, instance_name)
            return (
                False,
                f"Candidate activation failed ({error}); "
                + (
                    "previous runtime restored."
                    if rolled_back
                    else "automatic rollback also failed."
                ),
            )
        finally:
            if not transaction.activated:
                transaction.abandon()

    def _cancel_auto_update_job(self, process_name):
        existing_job = Update._jobs.get(process_name)
        if existing_job:
            try:
                self.scheduler.cancel_job(existing_job)
            except Exception:
                pass
            Update._jobs.pop(process_name, None)
        Update._next_check_at.pop(process_name, None)

    def update_schedule(self, process_name, config, key, instance_name):
        commit_sha = str(config.get("commit_sha") or "").strip().lower()
        if commit_sha:
            self._cancel_auto_update_job(process_name)
            self._safe_record_update_status(
                process_name,
                {
                    "status": "blocked",
                    "reason": "commit",
                    "message": (
                        f"{process_name} is pinned to commit {commit_sha[:12]}. "
                        "Automatic updates are disabled until the pin is changed or cleared."
                    ),
                    "auto_update_enabled": False,
                    "next_check_at": None,
                },
            )
            return

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
                "auto_update_mode": self.auto_update_mode(config),
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
        block_reason = self._get_update_block_reason(config, key)
        if not config.get("auto_update") or block_reason:
            self._cancel_auto_update_job(process_name)
            blocked_by_source = bool(block_reason)
            target_label = (
                self._configured_target_label(config, block_reason)
                if block_reason
                else None
            )
            self._safe_record_update_status(
                process_name,
                {
                    "status": "blocked" if blocked_by_source else "disabled",
                    "reason": block_reason,
                    "message": (
                        f"{process_name} is pinned to {target_label}. "
                        "Automatic updates are disabled until the source selection is changed."
                        if blocked_by_source
                        else "Auto-update disabled"
                    ),
                    "auto_update_enabled": False,
                    "auto_update_mode": self.auto_update_mode(config),
                    "auto_update_interval": self.auto_update_interval(
                        process_name, config
                    ),
                    "auto_update_start_time": self.auto_update_start_time(
                        process_name, config
                    ),
                    "next_check_at": None,
                },
            )
            if blocked_by_source:
                if block_reason == "commit":
                    return True, "Auto-update disabled by commit pin"
                return True, f"Auto-update disabled by {target_label}"
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

    @staticmethod
    def auto_update_mode(config):
        """Return the scheduled update action, preserving install as the legacy default."""
        mode = str((config or {}).get("auto_update_mode") or "install").strip()
        if mode == "check_only":
            return mode
        return "install"

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

    def _should_run_install_phase_for_preinstalled(
        self, process_name, key, instance_name, config
    ):
        try:
            versions = Versions()
            current_version, _ = versions.version_check(
                process_name, instance_name, key
            )
            current_version = (current_version or "").strip()
        except Exception:
            current_version = ""

        commit_sha = str(config.get("commit_sha") or "").strip().lower()
        if commit_sha:
            return current_version != f"commit-{commit_sha[:12]}"

        if config.get("branch_enabled") and key in {
            "decypharr",
            "nzbdav",
            "profilarr",
        }:
            branch_name = (config.get("branch") or "main").strip() or "main"
            # Source builds persist as "<branch>-<short_sha>" in version.txt.
            if key in {"decypharr", "profilarr"}:
                head_sha, head_err = self._fetch_branch_head_sha(
                    config.get("repo_owner"), config.get("repo_name"), branch_name
                )
                if head_sha:
                    expected = f"{branch_name}-{head_sha[:8]}"
                    if current_version == expected:
                        return False
                    return True
                if head_err:
                    self.logger.debug(
                        "%s branch SHA lookup failed for preinstall check: %s",
                        process_name,
                        head_err,
                    )
                if current_version.startswith(f"{branch_name}-"):
                    return False
                return True
            # NzbDAV branch installs persist as "<branch>-<short_sha>" (or fallback "branch-<name>").
            if key == "nzbdav":
                head_sha, head_err = self._fetch_branch_head_sha(
                    config.get("repo_owner"), config.get("repo_name"), branch_name
                )
                if head_sha:
                    expected = f"{branch_name}-{head_sha[:8]}"
                    if current_version == expected:
                        return False
                    return True
                if head_err:
                    self.logger.debug(
                        "NzbDAV branch SHA lookup failed for preinstall check: %s",
                        head_err,
                    )
                if current_version.startswith(f"{branch_name}-"):
                    return False
                if current_version == f"branch-{branch_name}":
                    return False
                return True
            # For other branch-enabled services, prefer install phase so branch source is applied.
            return True

        if config.get("release_version_enabled"):
            requested_release = (config.get("release_version") or "").strip()
            requested_lower = requested_release.lower()
            if not requested_release:
                return True
            if key == "mediastorm":
                try:
                    selector = mediastorm_install_selector(config)
                except MediaStormInstallError:
                    return True
                runtime_dir = os.path.join(
                    config.get("config_dir", "/mediastorm"), "runtime"
                )
                return not mediastorm_runtime_matches_selection(runtime_dir, selector)
            if (
                requested_lower in {"latest", "prerelease"}
                or "nightly" in requested_lower
            ):
                return False
            if key == "nzbdav":
                expected, ref_error = self._resolve_nzbdav_release_marker(config)
                if expected:
                    return current_version != expected
                if ref_error:
                    self.logger.debug(
                        "NzbDAV release tag SHA lookup failed for preinstall check: %s",
                        ref_error,
                    )
                return True
            return current_version != requested_release

        pinned_version = (config.get("pinned_version") or "").strip()
        if pinned_version:
            return current_version != pinned_version

        return False

    def _calculate_next_symlink_backup_at(self, process_name, config, now_ts=None):
        interval_hours = self.symlink_backup_interval(process_name, config)
        start_time = self.symlink_backup_start_time(process_name, config)
        return self._calculate_next_run_at(interval_hours, start_time, now_ts)

    def _run_scheduled_update_if_due(self, process_name, config, key, instance_name):
        latest_config = CONFIG_MANAGER.get_instance(instance_name, key)
        if not latest_config:
            return
        block_reason = self._get_update_block_reason(latest_config, key)
        if block_reason:
            self._cancel_auto_update_job(process_name)
            target_label = self._configured_target_label(latest_config, block_reason)
            self._safe_record_update_status(
                process_name,
                {
                    "status": "blocked",
                    "reason": block_reason,
                    "message": (
                        f"{process_name} is pinned to {target_label}. "
                        "Automatic updates are disabled until the source selection is changed."
                    ),
                    "auto_update_enabled": False,
                    "next_check_at": None,
                },
            )
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
        update_status = self.scheduled_update_check(
            process_name, latest_config, key, instance_name
        )
        schedule_status = {
            "status": "scheduled",
            "auto_update_enabled": True,
            "auto_update_mode": self.auto_update_mode(latest_config),
            "auto_update_interval": self.auto_update_interval(
                process_name, latest_config
            ),
            "auto_update_start_time": self.auto_update_start_time(
                process_name, latest_config
            ),
            "next_check_at": next_due_at,
        }
        if self.auto_update_mode(latest_config) == "check_only" and isinstance(
            update_status, dict
        ):
            schedule_status = {**update_status, **schedule_status}
            schedule_status["status"] = update_status.get("status", "scheduled")
        self._safe_record_update_status(process_name, schedule_status)

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

        commit_sha = str(config.get("commit_sha") or "").strip().lower()
        if commit_sha:
            enable_update = False
            self._cancel_auto_update_job(process_name)
            self.logger.info(
                "Automatic updates disabled for %s because it is pinned to commit %s.",
                process_name,
                commit_sha[:12],
            )
        elif (
            config.get("pinned_version")
            or (
                config.get("release_version_enabled")
                and str(config.get("release_version") or "").strip().lower() != "latest"
            )
            or config.get("branch_enabled")
        ):
            if not self._release_is_nightly_or_prerelease(
                config
            ) and not self._is_nzbdav_named_release_channel(key, config):
                enable_update = False
                self.logger.info(
                    "Automatic updates disabled for %s due to pinned, release, or branch configuration.",
                    process_name,
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
                    run_install_phase = self._should_run_install_phase_for_preinstalled(
                        process_name, key, instance_name, config
                    )
                    if run_install_phase:
                        self.logger.info(
                            "Preinstalled %s requires install phase to apply branch/release/pinned source settings.",
                            process_name,
                        )
                        success, setup_error = setup_project(
                            self.process_handler, process_name
                        )
                    else:
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
                run_install_phase = self._should_run_install_phase_for_preinstalled(
                    process_name, key, instance_name, config
                )
                if run_install_phase:
                    self.logger.info(
                        "Preinstalled %s requires install phase to apply branch/release/pinned source settings.",
                        process_name,
                    )
                    success, setup_error = setup_project(
                        self.process_handler, process_name
                    )
                else:
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
            if self.auto_update_mode(config) == "check_only":
                update_status = self._manual_update_check_internal(
                    process_name, config, key, instance_name
                )
                self._safe_record_update_status(process_name, update_status)
                if update_status.get("status") == "error":
                    return None, update_status.get("message") or (
                        f"Update check failed for {process_name}."
                    )

                success, error = configure_project(self.process_handler, process_name)
                if not success:
                    return None, f"Failed to configure {process_name}: {error}"
                return self.start_process(process_name, config, key, instance_name)

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
        try:
            update_status = self._manual_update_check_internal(
                process_name, config, key, instance_name
            )
            self._safe_record_update_status(process_name, update_status)
            if (
                update_status.get("status") != "update_available"
                or self.auto_update_mode(config) == "check_only"
            ):
                return update_status
        except Exception as error:
            self.logger.warning(
                "Scheduled update preflight failed for %s: %s", process_name, error
            )
            return {
                "status": "error",
                "message": f"Scheduled update check failed for {process_name}.",
            }
        manager = getattr(self, "media_protection_manager", None)
        protection = None
        if manager is not None:
            protection = manager.begin_planned(process_name, "scheduled_update", "safe")
            if protection["status"] == "deferred":
                payload = {
                    "status": "deferred",
                    "reason": "media_protection",
                    "message": "Scheduled update deferred by media library protection.",
                    "media_protection": protection["preflight"],
                }
                self._safe_record_update_status(process_name, payload)
                return payload
        success = False
        try:
            install_status = self._scheduled_update_check_unprotected(
                process_name, config, key, instance_name
            )
            success = True
        finally:
            if manager is not None and protection is not None:
                manager.complete_planned(protection.get("token"), success=success)
        return install_status or update_status

    def _scheduled_update_check_unprotected(
        self, process_name, config, key, instance_name
    ):
        operation_id = INSTALL_CACHE.begin_operation(process_name)
        self._active_install_operation = operation_id
        success = False
        payload = None
        with self.updating:
            timing_started = self._begin_update_timing(process_name)
            try:
                self.logger.info(
                    f"Performing scheduled update check for {process_name}"
                )
                INSTALL_CACHE.update_operation(operation_id, stage="checking")
                success, error = self.update_check(
                    process_name, config, key, instance_name
                )
                if not success:
                    if "No updates available" in error:
                        self.logger.info(error)
                        payload = {"status": "no_update", "message": error}
                        INSTALL_CACHE.update_operation(
                            operation_id,
                            stage="complete",
                            status="completed",
                            message=error,
                        )
                    else:
                        payload = {"status": "error", "message": str(error)}
                        INSTALL_CACHE.update_operation(
                            operation_id,
                            stage="failed",
                            status="failed",
                            message=str(error)[:1000],
                        )
                        raise RuntimeError(error)
                else:
                    payload = {"status": "updated", "message": str(error)}
                    INSTALL_CACHE.update_operation(
                        operation_id,
                        stage="complete",
                        status="completed",
                        message=str(error)[:1000],
                    )
                self._safe_record_update_status(process_name, payload)
                return payload
            finally:
                if not success:
                    self._recover_pending_snapshot(process_name)
                if timing_started:
                    self._finish_update_timing(process_name, payload)
                self._active_install_operation = None

    def update_check(self, process_name, config, key, instance_name):
        commit_sha = str(config.get("commit_sha") or "").strip().lower()
        if commit_sha:
            return (
                False,
                f"No updates available for {process_name}: pinned to commit {commit_sha[:12]}.",
            )

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

        if config.get("branch_enabled"):
            repo_owner = config.get("repo_owner")
            repo_name = config.get("repo_name")
            if not repo_owner or not repo_name:
                return False, f"{process_name} missing repo configuration."

            branch_name = (config.get("branch") or "main").strip() or "main"
            head_sha, head_error = self._fetch_branch_head_sha(
                repo_owner, repo_name, branch_name
            )
            if not head_sha:
                return False, head_error or "Failed to resolve branch head SHA."

            versions = Versions()
            current_version, version_error = versions.version_check(
                process_name, instance_name, key
            )
            if not current_version:
                return False, version_error or "Failed to read current version."

            target_version = f"{branch_name}-{head_sha[:8]}"
            if current_version == target_version:
                return False, f"No updates available for {process_name}."

            self.logger.info(
                "Updating %s branch %s from %s to %s.",
                process_name,
                branch_name,
                current_version,
                target_version,
            )
            if key == "traefik_proxy_admin":
                return self._transactional_tpa_update(
                    process_name,
                    config,
                    key,
                    instance_name,
                    target_version,
                    lambda: setup_project(self.process_handler, process_name),
                )
            if process_name in self.process_handler.process_names:
                self.stop_process(process_name)
            with self.process_handler.setup_tracker_lock:
                if process_name in self.process_handler.setup_tracker:
                    self.process_handler.setup_tracker.remove(process_name)

            if key != "profilarr":
                success, error = setup_branch_version(
                    self.process_handler, config, process_name, key
                )
                if not success:
                    return (
                        False,
                        f"Failed to update {process_name} to {target_version}: {error}",
                    )
            success, error = setup_project(self.process_handler, process_name)
            if not success:
                return (
                    False,
                    f"Failed to complete setup for {process_name}: {error}",
                )
            return self.start_process(process_name, config, key, instance_name)

        release_value = (config.get("release_version") or "").lower()
        if config.get("release_version_enabled"):
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

        if (
            key == "nzbdav"
            and config.get("release_version_enabled")
            and not nightly
            and not prerelease
            and release_value != "latest"
        ):
            target_version, ref_error = self._resolve_nzbdav_release_marker(config)
            if not target_version:
                return False, ref_error
            versions = Versions()
            current_version, version_error = versions.version_check(
                process_name, instance_name, key
            )
            if not current_version:
                return False, version_error or "Failed to read current version."
            if current_version == target_version:
                return False, f"No updates available for {process_name}."

            self.logger.info(
                "Updating %s release tag %s from %s to %s.",
                process_name,
                config.get("release_version"),
                current_version,
                target_version,
            )
            if process_name in self.process_handler.process_names:
                self.stop_process(process_name)
            with self.process_handler.setup_tracker_lock:
                if process_name in self.process_handler.setup_tracker:
                    self.process_handler.setup_tracker.remove(process_name)
            success, error = setup_release_version(
                self.process_handler, config, process_name, key
            )
            if not success:
                return (
                    False,
                    f"Failed to update {process_name} to {target_version}: {error}",
                )
            success, error = setup_project(self.process_handler, process_name)
            if not success:
                return (
                    False,
                    f"Failed to complete setup for {process_name}: {error}",
                )
            process, error = self.start_process(
                process_name, config, key, instance_name
            )
            if not process:
                return False, f"Failed to start {process_name}: {error}"
            return True, f"Updated {process_name} to {target_version}."

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

            if not isinstance(update_info, dict):
                return (
                    False,
                    f"Failed to check updates for {process_name}: "
                    f"{update_info or 'unknown version comparison error'}",
                )
            if not update_needed:
                return False, f"{update_info.get('message')} for {process_name}."

            self.logger.info(
                f"Updating {process_name} from {update_info.get('current_version')} to {update_info.get('latest_version')}."
            )
            release_version = f"{update_info.get('latest_version')}"
            if (
                not prerelease
                and not nightly
                and not (key == "profilarr" and release_value == "latest")
            ):
                config["release_version"] = release_version
                self.logger.info(
                    f"Updating {process_name} config to {release_version}."
                )
            if key == "traefik_proxy_admin":
                return self._transactional_tpa_update(
                    process_name,
                    config,
                    key,
                    instance_name,
                    release_version,
                    lambda: setup_project(self.process_handler, process_name),
                )
            if process_name in self.process_handler.process_names:
                self.stop_process(process_name)
            with self.process_handler.setup_tracker_lock:
                if process_name in self.process_handler.setup_tracker:
                    self.process_handler.setup_tracker.remove(process_name)
            if key != "profilarr":
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
            process, start_error = self.start_process(
                process_name, config, key, instance_name
            )
            if not process:
                return False, f"Failed to start {process_name}: {start_error}"
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

        process, start_error = self.start_process(
            process_name, config, key, instance_name
        )
        if not process:
            return False, f"Failed to start {process_name}: {start_error}"
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

        process, start_error = self.start_process(
            process_name, config, key, instance_name
        )
        if not process:
            return False, f"Failed to start {process_name}: {start_error}"
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

        process, start_error = self.start_process(
            process_name, config, key, instance_name
        )
        if not process:
            return False, f"Failed to start {process_name}: {start_error}"
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

        process, start_error = self.start_process(
            process_name, config, key, instance_name
        )
        if not process:
            return False, f"Failed to start {process_name}: {start_error}"
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

            process, start_error = self.start_process(
                process_name, config, key, instance_name
            )
            if not process:
                return False, f"Failed to start {process_name}: {start_error}"
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

        process, start_error = self.start_process(
            process_name, config, key, instance_name
        )
        if not process:
            return False, f"Failed to start {process_name}: {start_error}"
        return True, f"Updated {process_name} to {update_info.get('latest_version')}."

    @staticmethod
    def _rollback_persistent_paths(key, config, target_dir):
        values = list(config.get("exclude_dirs") or [])
        defaults = {
            "nzbdav": [
                "blobs",
                "data",
                "data-protection",
                "backups",
                "logs",
                ".dotnet-sdk",
            ],
            "maintainerr": ["data"],
            "tautulli": ["data"],
            "seerr": ["config"],
            "pulsarr": ["data"],
            "profilarr": ["config"],
            "riven_backend": ["data"],
            "zilean": ["data"],
            "traefik_proxy_admin": [".next/cache"],
        }
        values.extend(defaults.get(key, []))
        normalized = []
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            if os.path.isabs(text):
                try:
                    relative = os.path.relpath(text, target_dir)
                except ValueError:
                    continue
                if relative == ".." or relative.startswith(f"..{os.sep}"):
                    continue
                normalized.append(relative)
            else:
                normalized.append(text)
        return normalized

    def _snapshot_target(self, key, config):
        if key in {
            "plex",
            "jellyfin",
            "emby",
            "postgres",
            "pgadmin",
            "mediastorm",
            "cloudflared",
            "authelia",
            "rclone",
            "traefik",
        }:
            return None
        target_dir = config.get("install_dir") or config.get("config_dir")
        if not target_dir or not os.path.isdir(target_dir):
            return None
        return os.path.realpath(target_dir)

    def _capture_rollback_snapshot(self, process_name):
        if process_name in self._rollback_snapshots:
            return
        key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
        config = CONFIG_MANAGER.get_instance(instance_name, key) if key else None
        install_cache = (CONFIG_MANAGER.get("dumb") or {}).get("install_cache") or {}
        if not config or not install_cache.get("enabled", True):
            return
        target_dir = self._snapshot_target(key, config)
        if not target_dir:
            return
        snapshot = RuntimeRollbackSnapshot(
            target_dir,
            process_name,
            self._rollback_persistent_paths(key, config, target_dir),
        )
        try:
            if snapshot.capture():
                self._rollback_snapshots[process_name] = snapshot
                active = getattr(
                    self.process_handler, "transactional_update_snapshots", set()
                )
                active.add(process_name)
                self.process_handler.transactional_update_snapshots = active
                if self._active_install_operation:
                    INSTALL_CACHE.update_operation(
                        self._active_install_operation, stage="snapshot"
                    )
                self.logger.info(
                    "Captured rollback-safe runtime snapshot for %s.", process_name
                )
        except OSError as error:
            self.logger.warning(
                "Unable to capture runtime snapshot for %s; refusing destructive update: %s",
                process_name,
                error,
            )
            raise RuntimeError(
                f"Unable to protect the existing {process_name} runtime: {error}"
            ) from error

    def _recover_pending_snapshot(self, process_name):
        snapshot = self._rollback_snapshots.pop(process_name, None)
        if snapshot is None:
            return False
        active = getattr(self.process_handler, "transactional_update_snapshots", set())
        active.discard(process_name)
        self.logger.warning("Restoring previous runtime for %s.", process_name)
        self._mark_update_downtime_started(process_name)
        self.process_handler.stop_process(process_name)
        restored = snapshot.rollback()
        if not restored:
            if self._active_install_operation:
                INSTALL_CACHE.update_operation(
                    self._active_install_operation,
                    stage="rollback_failed",
                    rollback_performed=1,
                )
            return False
        snapshot.commit()
        key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
        config = CONFIG_MANAGER.get_instance(instance_name, key) if key else None
        if not config:
            self.logger.error(
                "Previous runtime restored for %s but its configuration could not be resolved.",
                process_name,
            )
            if self._active_install_operation:
                INSTALL_CACHE.update_operation(
                    self._active_install_operation,
                    stage="rollback_failed",
                    rollback_performed=1,
                )
            return False
        with self.process_handler.setup_tracker_lock:
            self.process_handler.setup_tracker.discard(process_name)
        configured, error = configure_project(self.process_handler, process_name)
        if not configured:
            self.logger.error(
                "Previous runtime restored for %s but configuration failed: %s",
                process_name,
                error,
            )
            if self._active_install_operation:
                INSTALL_CACHE.update_operation(
                    self._active_install_operation,
                    stage="rollback_failed",
                    rollback_performed=1,
                )
            return False
        process, start_error = self.start_process(
            process_name, config, key, instance_name
        )
        if not process:
            self.logger.error(
                "Previous runtime restored for %s but failed to start: %s",
                process_name,
                start_error,
            )
            if self._active_install_operation:
                INSTALL_CACHE.update_operation(
                    self._active_install_operation,
                    stage="rollback_failed",
                    rollback_performed=1,
                )
            return False
        healthy, health_error = self._wait_for_update_health(process_name)
        if not healthy:
            self.logger.error(
                "Previous runtime restored for %s but failed health stabilization: %s",
                process_name,
                health_error,
            )
            if self._active_install_operation:
                INSTALL_CACHE.update_operation(
                    self._active_install_operation,
                    stage="rollback_failed",
                    rollback_performed=1,
                )
            return False
        if self._active_install_operation:
            INSTALL_CACHE.update_operation(
                self._active_install_operation,
                stage="rolled_back",
                rollback_performed=1,
            )
        return True

    def _wait_for_update_health(self, process_name):
        settings = (CONFIG_MANAGER.get("dumb") or {}).get("install_cache") or {}
        timeout = max(5, int(settings.get("activation_health_timeout_seconds", 120)))
        stabilization = max(
            0, int(settings.get("activation_stabilization_seconds", 15))
        )
        deadline = time.monotonic() + timeout
        ready_since = None
        last_reason = "Service did not become ready."
        while time.monotonic() < deadline:
            readiness = self.process_handler.get_service_readiness(process_name)
            state = readiness.get("state")
            last_reason = readiness.get("reason") or last_reason
            # Application probes are expected to be unhealthy briefly while a
            # newly launched process binds its port and initializes its data.
            # get_service_readiness() deliberately represents that condition
            # as "starting". Only a terminal process failure should bypass the
            # configured readiness timeout; otherwise transactional activation
            # would roll back every service that needs more than one probe to
            # become ready.
            if state == "failed":
                return False, last_reason
            if state == "ready":
                # Downtime ends when the application first answers its
                # readiness probe. The remaining stable-health window is still
                # included in total install time, but is not service outage.
                self._mark_update_service_ready(process_name)
                if ready_since is None:
                    ready_since = time.monotonic()
                if time.monotonic() - ready_since >= stabilization:
                    return True, None
            else:
                # If a replacement briefly became ready and then regressed
                # during stabilization, begin a new observed outage interval.
                self._mark_update_downtime_started(process_name)
                ready_since = None
            time.sleep(2)
        return False, last_reason

    def _finalize_runtime_snapshot(self, process_name, process):
        snapshot = self._rollback_snapshots.get(process_name)
        if snapshot is None:
            return process, None
        if not process:
            recovered = self._recover_pending_snapshot(process_name)
            if not recovered:
                return (
                    False,
                    "Replacement process failed to start; rollback was attempted, "
                    "but the previous runtime did not return to stable health.",
                )
            return (
                False,
                "Replacement process failed to start; previous runtime restored.",
            )
        if self._active_install_operation:
            INSTALL_CACHE.update_operation(
                self._active_install_operation, stage="health_check"
            )
        healthy, error = self._wait_for_update_health(process_name)
        if not healthy:
            recovered = self._recover_pending_snapshot(process_name)
            if not recovered:
                return (
                    False,
                    f"Replacement failed health stabilization ({error}); rollback "
                    "was attempted, but the previous runtime did not return to "
                    "stable health.",
                )
            return (
                False,
                f"Replacement failed health stabilization ({error}); previous runtime restored.",
            )
        snapshot = self._rollback_snapshots.pop(process_name, None)
        if snapshot:
            snapshot.commit()
        active = getattr(self.process_handler, "transactional_update_snapshots", set())
        active.discard(process_name)
        return process, None

    def stop_process(self, process_name):
        self._capture_rollback_snapshot(process_name)
        self._mark_update_downtime_started(process_name)
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
            wait_url_entries = config["wait_for_url"]
            time.sleep(5)
            result, error = wait_for_urls(
                wait_url_entries,
                process_name,
                logger,
                lambda: self.process_handler.shutting_down,
            )
            if not result:
                return False, error

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
        if not process:
            finalized, snapshot_error = self._finalize_runtime_snapshot(
                process_name, process
            )
            return finalized, snapshot_error or error
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
                return self._finalize_runtime_snapshot(process_name, process)

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

        finalized, finalize_error = self._finalize_runtime_snapshot(
            process_name, process
        )
        return finalized, finalize_error or error
