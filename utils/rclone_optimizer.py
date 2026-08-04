"""Background, provider-conscious rclone streaming optimization for NzbDAV mounts.

The optimizer never benchmarks against the production mount directly.  Each candidate
uses a short-lived read-only shadow mount with its own VFS cache and loopback RC port.
This makes candidate results comparable without evicting or warming the live cache.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import random
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psutil

from utils.config_loader import CONFIG_MANAGER
from utils.nzbdav_settings import get_nzbdav_arr_categories
from utils.notifications import notify_event
from utils.port_probe import is_port_available
from utils.url_security import safe_request, safe_urlopen

ACTIVE_STATUSES = {
    "queued",
    "preflight",
    "benchmarking",
    "reporting",
    "applying",
    "rolling_back",
}
TERMINAL_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "applied",
    "rolled_back",
}
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
DURATION_RE = re.compile(r"(?:\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h|d|w))+", re.I)
MEDIA_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".asf",
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
}
MANAGED_FLAGS = {
    "--buffer-size",
    "--dir-cache-time",
    "--vfs-cache-max-age",
    "--vfs-cache-max-size",
    "--vfs-cache-mode",
    "--vfs-read-ahead",
    "--vfs-read-chunk-size",
    "--vfs-read-chunk-size-limit",
    "--transfers",
}
VARIED_STREAMING_FLAGS = {
    "--buffer-size",
    "--vfs-read-ahead",
    "--vfs-read-chunk-size",
    "--vfs-read-chunk-size-limit",
}
FIXED_CONSTRAINT_FLAGS = {"--vfs-cache-max-size"}
BUNDLED_ASSUMPTION_FLAGS = {
    "--vfs-cache-mode",
}
NZBDAV_RECOMMENDED_FLAGS = {
    "--dir-cache-time",
    "--vfs-cache-max-age",
}
SETTING_DISPLAY_ORDER = (
    "--vfs-cache-mode",
    "--vfs-cache-max-size",
    "--vfs-cache-max-age",
    "--dir-cache-time",
    "--buffer-size",
    "--vfs-read-chunk-size",
    "--vfs-read-chunk-size-limit",
    "--vfs-read-ahead",
    "--transfers",
)
PRIVATE_JOB_KEYS = {"previous_command", "recommended_command"}


def _setting_role(flag: str) -> str:
    if flag in VARIED_STREAMING_FLAGS:
        return "actually_varied"
    if flag in FIXED_CONSTRAINT_FLAGS:
        return "fixed_constraint"
    if flag in NZBDAV_RECOMMENDED_FLAGS:
        return "nzbdav_recommended"
    if flag in BUNDLED_ASSUMPTION_FLAGS:
        return "bundled_assumption"
    return "preserved"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bytes_label(value: int | float) -> str:
    value = float(value or 0)
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or suffix == "TiB":
            return f"{value:.1f} {suffix}"
        value /= 1024
    return f"{value:.1f} TiB"


def _duration_seconds(value: Any) -> float | None:
    text = str(value or "").strip().lower()
    if text == "0":
        return 0.0
    if not text or not DURATION_RE.fullmatch(text):
        return None
    units = {
        "ns": 1e-9,
        "us": 1e-6,
        "µs": 1e-6,
        "ms": 1e-3,
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 604800,
    }
    total = 0.0
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h|d|w)", text):
        total += float(amount) * units[unit]
    return total


def _nzbdav_timeout_recommendations(command: list[str]) -> dict[str, str]:
    current = _parse_flag_map(command)[1]
    minimum_seconds = 7 * 86400
    recommendations = {}
    for flag in NZBDAV_RECOMMENDED_FLAGS:
        value = current.get(flag)
        seconds = _duration_seconds(value)
        recommendations[flag] = (
            str(value) if seconds is not None and seconds >= minimum_seconds else "1w"
        )
    return recommendations


def _parse_flag_map(command: list[str]) -> tuple[list[str], dict[str, str | None]]:
    prefix: list[str] = []
    flags: dict[str, str | None] = {}
    index = 0
    while index < len(command):
        item = str(command[index])
        if not item.startswith("--"):
            prefix.append(item)
            index += 1
            continue
        if "=" in item:
            key, value = item.split("=", 1)
            flags[key] = value
        elif index + 1 < len(command) and not str(command[index + 1]).startswith("--"):
            flags[item] = str(command[index + 1])
            index += 1
        else:
            flags[item] = None
        index += 1
    return prefix, flags


def _build_command(prefix: list[str], flags: dict[str, str | None]) -> list[str]:
    command = list(prefix)
    for key, value in flags.items():
        command.append(key if value is None else f"{key}={value}")
    return command


def merge_managed_flags(command: list[str], settings: dict[str, str]) -> list[str]:
    """Merge only optimizer-owned flags, preserving all other user arguments."""
    prefix, flags = _parse_flag_map(command)
    for flag in MANAGED_FLAGS:
        if flag in settings:
            flags[flag] = settings[flag]
    return _build_command(prefix, flags)


def _candidate_profiles(
    depth: str,
    limits: dict[str, Any],
    current_command: list[str] | None = None,
) -> list[dict[str, Any]]:
    max_cache = max(1, int(limits["max_vfs_cache_gib"]))
    recommended_timeouts = _nzbdav_timeout_recommendations(current_command or [])
    common = {
        "--vfs-cache-mode": "full",
        "--vfs-cache-max-size": f"{max_cache}G",
        **recommended_timeouts,
    }
    candidates = [
        {
            "id": "baseline",
            "label": "Current streaming knobs (bounded + NzbDAV guidance)",
            "settings": {
                "--vfs-cache-max-size": f"{max_cache}G",
                **recommended_timeouts,
            },
        },
        {
            "id": "balanced",
            "label": "Balanced streaming",
            "settings": {
                **common,
                "--buffer-size": "64M",
                "--vfs-read-chunk-size": "32M",
                "--vfs-read-chunk-size-limit": "1G",
                "--vfs-read-ahead": "128M",
            },
        },
        {
            "id": "low-memory",
            "label": "Lower memory",
            "settings": {
                **common,
                "--buffer-size": "16M",
                "--vfs-read-chunk-size": "16M",
                "--vfs-read-chunk-size-limit": "512M",
                "--vfs-read-ahead": "32M",
            },
        },
        {
            "id": "fast-start",
            "label": "Fast startup",
            "settings": {
                **common,
                "--buffer-size": "32M",
                "--vfs-read-chunk-size": "8M",
                "--vfs-read-chunk-size-limit": "512M",
                "--vfs-read-ahead": "64M",
            },
        },
    ]
    if depth == "thorough":
        candidates.extend(
            [
                {
                    "id": "high-throughput",
                    "label": "High throughput",
                    "settings": {
                        **common,
                        "--buffer-size": "128M",
                        "--vfs-read-chunk-size": "64M",
                        "--vfs-read-chunk-size-limit": "2G",
                        "--vfs-read-ahead": "256M",
                    },
                },
                {
                    "id": "large-chunks",
                    "label": "Large chunks",
                    "settings": {
                        **common,
                        "--buffer-size": "64M",
                        "--vfs-read-chunk-size": "128M",
                        "--vfs-read-chunk-size-limit": "off",
                        "--vfs-read-ahead": "128M",
                    },
                },
            ]
        )
    elif depth == "quick":
        candidates = candidates[:2]
    return candidates


def _candidate_setting_comparison(
    command: list[str], settings: dict[str, str]
) -> list[dict[str, Any]]:
    current_flags = _parse_flag_map(command)[1]
    effective_flags = _parse_flag_map(merge_managed_flags(command, settings))[1]
    comparison = []
    for flag in SETTING_DISPLAY_ORDER:
        if (
            flag == "--transfers"
            and flag not in current_flags
            and flag not in effective_flags
        ):
            continue
        role = _setting_role(flag)
        current_value = current_flags.get(flag)
        tested_value = effective_flags.get(flag)
        comparison.append(
            {
                "flag": flag,
                "current_value": current_value,
                "tested_value": tested_value,
                "changed_from_current": current_value != tested_value,
                "role": role,
                "varied_across_candidates": role == "actually_varied",
                "independently_evaluated": False,
            }
        )
    return comparison


class RcloneOptimizerError(RuntimeError):
    pass


class RcloneOptimizerManager:
    def __init__(self, process_handler, logger, base_dir: str | None = None):
        self.process_handler = process_handler
        self.logger = logger
        self.base_dir = Path(base_dir or "/config/rclone-optimizer")
        self.jobs_dir = self.base_dir / "jobs"
        self.runtime_dir = self.base_dir / "runtime"
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._shutdown = threading.Event()
        self.storage_error: str | None = None
        try:
            self._prepare_storage()
            self._load_jobs()
        except OSError as error:
            self.storage_error = str(error)
            warning = getattr(self.logger, "warning", None)
            if callable(warning):
                warning("Rclone optimizer persistence is unavailable: %s", error)

    def _prepare_storage(self) -> None:
        for path in (self.base_dir, self.jobs_dir, self.runtime_dir):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)

    def _job_path(self, job_id: str) -> Path:
        if not JOB_ID_RE.fullmatch(str(job_id or "")):
            raise RcloneOptimizerError("Invalid optimizer job ID.")
        # The validated name is joined only after rejecting path syntax.
        return self.jobs_dir / f"{job_id}.json"

    def _load_jobs(self) -> None:
        for entry in self.jobs_dir.iterdir():
            if (
                not entry.is_file()
                or entry.is_symlink()
                or not JOB_ID_RE.fullmatch(entry.stem)
            ):
                continue
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
                if data.get("job_id") != entry.stem:
                    continue
                if data.get("status") in ACTIVE_STATUSES:
                    data["status"] = "interrupted"
                    data["finished_at"] = _utcnow()
                    data["error"] = (
                        "DUMB restarted while the optimizer job was active. Start a new test; interrupted benchmarks are not resumed."
                    )
                    self._write_job(data)
                self._jobs[entry.stem] = data
            except (OSError, ValueError, TypeError):
                self.logger.warning(
                    "Ignoring unreadable rclone optimizer job %s", entry.name
                )
        self._cleanup_runtime_root()

    def _cleanup_runtime_root(self) -> None:
        for entry in self.runtime_dir.iterdir():
            if not entry.is_dir() or entry.is_symlink():
                continue
            self._cleanup_job_runtime(entry)
        instances = (CONFIG_MANAGER.get("rclone") or {}).get("instances") or {}
        for instance in instances.values():
            if not isinstance(instance, dict):
                continue
            if str(instance.get("key_type") or "").lower() != "nzbdav":
                continue
            cache_root = Path(str(instance.get("cache_dir") or "/cache"))
            shadow_root = cache_root / ".dumb-rclone-optimizer"
            if shadow_root.is_dir() and not shadow_root.is_symlink():
                shutil.rmtree(shadow_root, ignore_errors=True)

    def _cleanup_job_runtime(self, runtime: Path) -> bool:
        if not runtime.is_dir() or runtime.is_symlink():
            return True
        cleanup_verified = True
        for candidate in runtime.iterdir():
            if candidate.is_dir() and not candidate.is_symlink():
                mount_path = candidate / "mount"
                if not self._unmount(mount_path):
                    cleanup_verified = False
                    self.logger.error(
                        "Could not verify optimizer shadow mount cleanup at %s; leaving the runtime directory in place for a later retry.",
                        mount_path,
                    )
        if not cleanup_verified:
            return False
        shutil.rmtree(runtime, ignore_errors=True)
        return not runtime.exists()

    def _cleanup_job_artifacts(self, job: dict[str, Any], runtime: Path) -> dict:
        runtime_verified = self._cleanup_job_runtime(runtime)
        cache_verified = False
        try:
            _instance_name, cleanup_instance = self._resolve_instance(
                job["process_name"]
            )
            cache_job_root = (
                Path(str(cleanup_instance.get("cache_dir") or "/cache"))
                / ".dumb-rclone-optimizer"
                / job["job_id"]
            )
            if (
                runtime_verified
                and cache_job_root.is_dir()
                and not cache_job_root.is_symlink()
            ):
                shutil.rmtree(cache_job_root, ignore_errors=True)
            cache_verified = not cache_job_root.exists()
        except RcloneOptimizerError:
            cache_verified = False
        return {
            "shadow_mounts_verified": runtime_verified,
            "runtime_removed": runtime_verified and not runtime.exists(),
            "cache_removed": cache_verified,
        }

    def _write_job(self, job: dict[str, Any]) -> None:
        if self.storage_error:
            raise RcloneOptimizerError(
                "Rclone optimizer persistence is unavailable: " + self.storage_error
            )
        path = self._job_path(job["job_id"])
        temp = path.with_suffix(".tmp")
        encoded = json.dumps(job, indent=2, sort_keys=True)
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            os.chmod(path, 0o600)
        except OSError as error:
            raise RcloneOptimizerError(
                f"Could not persist optimizer job state: {error}"
            ) from None
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def _update(self, job: dict[str, Any], **changes: Any) -> None:
        with self._lock:
            job.update(changes)
            job["updated_at"] = _utcnow()
            self._jobs[job["job_id"]] = job
            self._write_job(job)

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(job)
        for key in PRIVATE_JOB_KEYS:
            result.pop(key, None)
        return result

    def list_instances(self) -> list[dict[str, Any]]:
        instances = (CONFIG_MANAGER.get("rclone") or {}).get("instances") or {}
        result = []
        for instance_name, instance in instances.items():
            if not isinstance(instance, dict):
                continue
            if str(instance.get("key_type") or "").lower() != "nzbdav":
                continue
            mount_path = os.path.join(
                str(instance.get("mount_dir") or "/mnt/debrid"),
                str(instance.get("mount_name") or instance_name),
            )
            result.append(
                {
                    "instance_name": instance_name,
                    "process_name": instance.get("process_name")
                    or f"rclone w/ {instance_name}",
                    "enabled": instance.get("enabled") is True,
                    "mount_path": mount_path,
                    "mounted": os.path.ismount(mount_path),
                    "source_service": "NzbDAV",
                }
            )
        return result

    def _resolve_instance(self, process_name: str) -> tuple[str, dict[str, Any]]:
        for name, instance in (
            (CONFIG_MANAGER.get("rclone") or {}).get("instances") or {}
        ).items():
            if not isinstance(instance, dict):
                continue
            if instance.get("process_name") != process_name:
                continue
            if str(instance.get("key_type") or "").lower() != "nzbdav":
                raise RcloneOptimizerError(
                    "Only NzbDAV-backed rclone instances are supported."
                )
            if instance.get("enabled") is not True:
                raise RcloneOptimizerError(
                    "The rclone instance must be enabled before testing."
                )
            return name, instance
        raise RcloneOptimizerError("NzbDAV-backed rclone instance was not found.")

    @staticmethod
    def _mount_path(instance: dict[str, Any]) -> Path:
        return Path(str(instance.get("mount_dir") or "/mnt/debrid")) / str(
            instance.get("mount_name") or ""
        )

    def discover_content(
        self, process_name: str, max_files: int = 4000
    ) -> dict[str, Any]:
        _name, instance = self._resolve_instance(process_name)
        root = self._mount_path(instance).resolve()
        if not root.is_dir():
            raise RcloneOptimizerError("The production rclone mount is not available.")
        active_categories = [
            item
            for item in get_nzbdav_arr_categories()
            if CATEGORY_RE.fullmatch(str(item.get("category") or ""))
        ]
        if not active_categories:
            raise RcloneOptimizerError(
                "No enabled Arr instances are linked to NzbDAV, so no active content categories could be discovered."
            )
        started = time.monotonic()
        items: list[dict[str, Any]] = []
        seen_targets: set[str] = set()
        category_results = []
        scanned = 0
        for category_item in active_categories:
            category = category_item["category"]
            category_root = root / "content" / category
            available = category_root.is_dir()
            category_result = {**category_item, "available": available, "found": 0}
            category_results.append(category_result)
            if not available:
                continue
            try:
                category_root.resolve(strict=True).relative_to(root)
            except (OSError, ValueError):
                category_result["available"] = False
                continue
            for current_root, directories, files in os.walk(category_root):
                directories[:] = [
                    directory
                    for directory in sorted(directories)
                    if not directory.startswith(".")
                ][:250]
                for filename in sorted(files):
                    scanned += 1
                    if scanned > max_files or time.monotonic() - started > 15:
                        break
                    source_path = Path(current_root) / filename
                    if source_path.suffix.lower() not in MEDIA_EXTENSIONS:
                        continue
                    try:
                        target_path = source_path.resolve(strict=True)
                        relative = target_path.relative_to(root).as_posix()
                        if relative in seen_targets:
                            continue
                        stat = target_path.stat()
                        if not target_path.is_file() or stat.st_size < 8 * 1024 * 1024:
                            continue
                        source_stat = source_path.lstat()
                        display_path = source_path.relative_to(root).as_posix()
                    except (OSError, ValueError):
                        continue
                    seen_targets.add(relative)
                    category_result["found"] += 1
                    items.append(
                        {
                            "path": relative,
                            "display_path": display_path,
                            "size_bytes": stat.st_size,
                            "size_label": _bytes_label(stat.st_size),
                            "modified_at": datetime.fromtimestamp(
                                source_stat.st_mtime, timezone.utc
                            ).isoformat(),
                            "mtime": source_stat.st_mtime,
                        }
                    )
                if scanned > max_files or time.monotonic() - started > 15:
                    break
            if scanned > max_files or time.monotonic() - started > 15:
                break
        if not items:
            categories = ", ".join(item["category"] for item in active_categories)
            raise RcloneOptimizerError(
                f"No suitable media files were found in the active NzbDAV categories: {categories}."
            )
        ordered = sorted(items, key=lambda item: item["mtime"], reverse=True)
        picks: list[tuple[str, str, str, dict[str, Any]]] = [
            (
                "recent_likely_warm",
                "Recent / likely warm",
                "Most recently modified file discovered; more likely to have warm metadata or cached provider data.",
                ordered[0],
            ),
            (
                "older_likely_cold",
                "Older / likely cold",
                "Oldest modified file discovered; more likely to exercise a cold or less recently used path.",
                ordered[-1],
            ),
            (
                "large_high_bitrate",
                "Largest / high-bandwidth candidate",
                "Largest file discovered; used as a practical high-bandwidth and sustained-throughput candidate.",
                max(items, key=lambda item: item["size_bytes"]),
            ),
            (
                "typical",
                "Typical / median-age candidate",
                "File near the middle of the discovered modification-age range; used as a representative everyday sample.",
                ordered[len(ordered) // 2],
            ),
        ]
        for item in items:
            item["age_bucket"] = (
                "recent" if item["mtime"] >= time.time() - 7 * 86400 else "older"
            )
            item.pop("mtime", None)
        selected = []
        selection_order: dict[str, int] = {}
        seen = set()
        for selection_key, selection_label, selection_reason, item in picks:
            if item["path"] in seen:
                continue
            seen.add(item["path"])
            selection_order[item["path"]] = len(selection_order)
            selected.append(
                {
                    **item,
                    "category": selection_key,
                    "selection_key": selection_key,
                    "selection_label": selection_label,
                    "selection_reason": selection_reason,
                }
            )
        visible_items = sorted(
            enumerate(items),
            key=lambda pair: (
                selection_order.get(pair[1]["path"], len(selection_order)),
                0 if pair[1]["path"] in selection_order else 1,
                pair[0],
            ),
        )
        return {
            "process_name": process_name,
            "mount_path": str(root),
            "discovery_mode": "active_arr_categories",
            "content_base": str(root / "content"),
            "active_categories": category_results,
            "scanned": scanned,
            "truncated": scanned > max_files or time.monotonic() - started > 15,
            "selection_note": "The files at the top are representative automatic suggestions, not required choices. Keep them, replace them, or mix in your own files (up to eight total). Each entry is resolved to a safe mount-relative read path and opened on every isolated rclone shadow mount. Recent/older are cache-likelihood heuristics; NzbDAV telemetry determines what actually happened during each read.",
            "automatic_selection": selected,
            "files": [item for _index, item in visible_items[:500]],
        }

    def create_job(
        self,
        process_name: str,
        selected_paths: list[str],
        depth: str,
        limits: dict[str, Any],
    ) -> dict[str, Any]:
        instance_name, instance = self._resolve_instance(process_name)
        validated_paths = self._validate_paths(instance, selected_paths)
        job_id = uuid4().hex
        job = {
            "job_id": job_id,
            "process_name": process_name,
            "instance_name": instance_name,
            "source_service": "NzbDAV",
            "status": "queued",
            "stage": "Queued",
            "progress": 0,
            "created_at": _utcnow(),
            "updated_at": _utcnow(),
            "started_at": None,
            "finished_at": None,
            "depth": depth,
            "limits": limits,
            "setting_model": {
                "actually_varied": sorted(VARIED_STREAMING_FLAGS),
                "fixed_constraints": [
                    "--vfs-cache-max-size",
                    "bandwidth_limit_mbps",
                    "max_memory_mib",
                    "min_free_disk_gib",
                    "max_duration_minutes",
                    "concurrent_streams",
                ],
                "bundled_assumptions": sorted(BUNDLED_ASSUMPTION_FLAGS),
                "nzbdav_recommended": sorted(NZBDAV_RECOMMENDED_FLAGS),
                "preserved": "Apart from the isolated mount/cache/RC/read-only/log plumbing required for safe testing, all other existing user flags are carried into every shadow command unchanged and are not evaluated by this optimizer.",
                "confidence_note": "The recommendation identifies the best-performing predefined bundle. It does not prove that every value in that bundle is individually optimal.",
            },
            "selected_content": validated_paths,
            "results": [],
            "recommendation": None,
            "warnings": [
                "The test reads real data through NzbDAV and its configured Usenet providers.",
                "For reliable results, stop Plex, Jellyfin, Emby, and other media servers, then wait until NzbDAV is idle with no imports, library ingestion, or unrelated active reads before starting the test.",
                "No provider cache purge is performed; recent and older samples are reported separately.",
            ],
            "error": None,
            "live": {},
        }
        with self._lock:
            if any(
                existing.get("status") in ACTIVE_STATUSES
                for existing in self._jobs.values()
            ):
                raise RcloneOptimizerError(
                    "Another rclone optimizer job is already active. Wait for it to finish so provider load remains bounded."
                )
            self._update(job)
            cancel = threading.Event()
            self._cancel[job_id] = cancel
        worker = threading.Thread(
            target=self._run_job,
            args=(job_id,),
            name=f"rclone-optimizer-{job_id[:8]}",
            daemon=True,
        )
        self._threads[job_id] = worker
        worker.start()
        return self._public(job)

    def _validate_paths(
        self, instance: dict[str, Any], selected_paths: list[str]
    ) -> list[dict[str, Any]]:
        root = self._mount_path(instance).resolve()
        if not 1 <= len(selected_paths) <= 8:
            raise RcloneOptimizerError("Select between one and eight media files.")
        result = []
        seen = set()
        for raw in selected_paths:
            relative = str(raw or "").strip().replace("\\", "/")
            if not relative or relative.startswith("/") or ".." in Path(relative).parts:
                raise RcloneOptimizerError(
                    "Selected content contains an invalid relative path."
                )
            if relative in seen:
                continue
            path = (root / relative).resolve()
            try:
                path.relative_to(root)
                stat = path.stat()
            except (OSError, ValueError):
                raise RcloneOptimizerError(
                    f"Selected content is unavailable: {relative}"
                ) from None
            if not path.is_file():
                raise RcloneOptimizerError(
                    f"Selected content is not a regular file: {relative}"
                )
            seen.add(relative)
            result.append(
                {
                    "path": relative,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime, timezone.utc
                    ).isoformat(),
                    "age_bucket": (
                        "recent"
                        if stat.st_mtime >= time.time() - 7 * 86400
                        else "older"
                    ),
                }
            )
        return result

    def get_job(self, job_id: str) -> dict[str, Any]:
        self._job_path(job_id)
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            raise RcloneOptimizerError("Optimizer job was not found.")
        return self._public(job)

    def latest_job(
        self, process_name: str | None = None, active_only: bool = False
    ) -> dict[str, Any] | None:
        with self._lock:
            candidates = list(self._jobs.values())
        if process_name:
            candidates = [
                job for job in candidates if job.get("process_name") == process_name
            ]
        if active_only:
            candidates = [
                job for job in candidates if job.get("status") in ACTIVE_STATUSES
            ]
        if not candidates:
            return None
        return self._public(
            max(candidates, key=lambda job: job.get("created_at") or "")
        )

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(
                self._jobs.values(),
                key=lambda job: job.get("created_at") or "",
                reverse=True,
            )
        return [self._public(job) for job in jobs[: max(1, min(limit, 100))]]

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        self._job_path(job_id)
        event = self._cancel.get(job_id)
        if not event:
            raise RcloneOptimizerError("The optimizer job is not active.")
        event.set()
        return self.get_job(job_id)

    def _run_job(self, job_id: str) -> None:
        job = self._jobs[job_id]
        cancel = self._cancel[job_id]
        runtime = self.runtime_dir / job_id
        trace_enabled_by_job = False
        try:
            self._update(
                job,
                status="preflight",
                stage="Checking limits and NzbDAV",
                progress=2,
                started_at=_utcnow(),
            )
            _instance_name, instance = self._resolve_instance(job["process_name"])
            self._preflight(instance, job["limits"])
            overview_path = "/api/get-overview-stats?window=1h&sections=window,detail"
            nzbdav_before = self._nzbdav_json(overview_path)
            trace_before = self._nzbdav_json("/api/get-stream-traces?limit=1")
            trace_was_enabled = bool((trace_before or {}).get("enabled"))
            if not trace_was_enabled:
                trace_enable = self._nzbdav_json(
                    "/api/set-stream-tracing?enabled=true&minutes=30&capacity=20000",
                    method="POST",
                )
                trace_enabled_by_job = bool(
                    isinstance(trace_enable, dict)
                    and trace_enable.get("status") is True
                    and trace_enable.get("enabled") is True
                )
                if not trace_enabled_by_job:
                    job["warnings"].append(
                        "NzbDAV stream tracing could not be enabled. Performance measurements will continue, but provider trace matching will be marked unavailable."
                    )
                    self._update(job, warnings=job["warnings"])
            rc_healthy, rc_detail = self._nzbdav_rc_health(instance)
            if not rc_healthy:
                job["warnings"].append(
                    "NzbDAV rclone RC notifications are not healthy: "
                    f"{rc_detail}. The one-week directory-cache recommendation assumes "
                    "NzbDAV can invalidate changed paths; repair RC or choose a shorter "
                    "--dir-cache-time fallback."
                )
                self._update(job, warnings=job["warnings"])
            profiles = _candidate_profiles(
                job["depth"], job["limits"], instance.get("command") or []
            )
            deadline = (
                time.monotonic() + int(job["limits"]["max_duration_minutes"]) * 60
            )
            budget = {
                "remaining": int(job["limits"]["max_test_download_gib"] * 1024**3)
            }
            results = []
            for index, profile in enumerate(profiles):
                if cancel.is_set():
                    raise InterruptedError("Optimizer job cancelled by the user.")
                if time.monotonic() >= deadline or budget["remaining"] <= 0:
                    job["warnings"].append(
                        "The configured duration or download budget ended the candidate matrix early."
                    )
                    break
                progress = 5 + int(index / max(1, len(profiles)) * 80)
                self._update(
                    job,
                    status="benchmarking",
                    stage=f"Testing {profile['label']}",
                    progress=progress,
                )
                result = self._benchmark_candidate(
                    job, instance, profile, runtime, budget, deadline, cancel
                )
                results.append(result)
                self._update(job, results=results)
                if result.get("provider_guard_stop"):
                    job["warnings"].append(
                        "Testing stopped early because NzbDAV reported provider errors, retries, failover, or an open circuit."
                    )
                    break
                if result.get("resource_limit_stop"):
                    job["warnings"].append(
                        "Testing stopped early because the configured memory or free-disk limit was reached."
                    )
                    break
            if not results:
                raise RcloneOptimizerError(
                    "No candidate completed within the configured limits."
                )
            self._update(
                job, status="reporting", stage="Building recommendation", progress=90
            )
            recommendation = self._recommend(instance, results)
            nzbdav_after = self._nzbdav_json(overview_path)
            if trace_enabled_by_job:
                self._nzbdav_json(
                    "/api/set-stream-tracing?enabled=false", method="POST"
                )
                trace_enabled_by_job = False
            cleanup = self._cleanup_job_artifacts(job, runtime)
            if not all(cleanup.values()):
                raise RcloneOptimizerError(
                    "The benchmark finished, but isolated shadow mount/cache cleanup could not be verified. The job was not marked complete and cleanup will be retried during finalization and the next DUMB startup."
                )
            self._update(
                job,
                status="completed",
                stage="Complete",
                progress=100,
                finished_at=_utcnow(),
                recommendation=recommendation,
                nzbdav_summary={
                    "before": self._summarize_nzbdav(nzbdav_before),
                    "after": self._summarize_nzbdav(nzbdav_after),
                },
                cleanup=cleanup,
                live={},
            )
            notify_event(
                "rclone.optimizer.completed",
                "success",
                f"Rclone optimization completed for {job['process_name']}",
                "The streaming benchmark report and recommended settings are ready for review. Settings were not applied automatically.",
                service_name=job["process_name"],
                metadata={"job_id": job_id},
            )
        except InterruptedError as error:
            interrupted = self._shutdown.is_set()
            self._update(
                job,
                status="interrupted" if interrupted else "cancelled",
                stage="Interrupted" if interrupted else "Cancelled",
                finished_at=_utcnow(),
                error=(
                    "DUMB stopped during this benchmark. The job was not resumed; start a fresh test."
                    if interrupted
                    else str(error)
                ),
                live={},
            )
        except Exception as error:
            self.logger.error("Rclone optimizer job %s failed: %s", job_id, error)
            self._update(
                job,
                status="failed",
                stage="Failed",
                finished_at=_utcnow(),
                error=str(error)[:1000],
                live={},
            )
            notify_event(
                "rclone.optimizer.failed",
                "warning",
                f"Rclone optimization failed for {job['process_name']}",
                "The benchmark stopped safely. Review the optimizer report for details; production settings were not changed.",
                service_name=job["process_name"],
                metadata={"job_id": job_id},
            )
        finally:
            if trace_enabled_by_job:
                self._nzbdav_json(
                    "/api/set-stream-tracing?enabled=false", method="POST"
                )
            process = self._processes.pop(job_id, None)
            if process:
                self._terminate_process(process)
            cleanup = self._cleanup_job_artifacts(job, runtime)
            if job.get("cleanup") != cleanup:
                changes: dict[str, Any] = {"cleanup": cleanup}
                if job.get("status") == "completed" and not all(cleanup.values()):
                    changes.update(
                        status="failed",
                        stage="Cleanup failed",
                        error="The benchmark completed, but isolated optimizer cleanup could not be verified.",
                    )
                try:
                    self._update(job, **changes)
                except (OSError, RcloneOptimizerError):
                    self.logger.error(
                        "Could not persist final optimizer cleanup status for job %s",
                        job_id,
                    )
            self._cancel.pop(job_id, None)
            self._threads.pop(job_id, None)

    def _preflight(self, instance: dict[str, Any], limits: dict[str, Any]) -> None:
        if not os.path.ismount(self._mount_path(instance)):
            raise RcloneOptimizerError("The production rclone mount is not mounted.")
        cache_root = Path(str(instance.get("cache_dir") or "/cache"))
        usage = shutil.disk_usage(cache_root)
        min_free = int(limits["min_free_disk_gib"] * 1024**3)
        if usage.free < min_free:
            raise RcloneOptimizerError(
                f"Cache storage has {_bytes_label(usage.free)} free; the configured minimum is {_bytes_label(min_free)}."
            )
        if psutil.virtual_memory().available < 256 * 1024**2:
            raise RcloneOptimizerError(
                "Less than 256 MiB of memory is available for a safe optimizer run."
            )
        data = self._nzbdav_json(
            "/api/get-overview-stats?window=1h&sections=window,detail"
        )
        if data is None:
            raise RcloneOptimizerError(
                "NzbDAV metrics API is unavailable or authentication failed."
            )

    def _shadow_command(
        self,
        instance: dict[str, Any],
        profile: dict[str, Any],
        mount_path: Path,
        cache_path: Path,
        rc_port: int,
        bandwidth_mbps: int,
    ) -> list[str]:
        command = merge_managed_flags(
            instance.get("command") or [], profile["settings"]
        )
        prefix, flags = _parse_flag_map(command)
        if len(prefix) < 4 or prefix[1] != "mount":
            raise RcloneOptimizerError(
                "The configured rclone command is not a supported mount command."
            )
        prefix[3] = str(mount_path)
        flags["--cache-dir"] = str(cache_path)
        flags["--rc"] = None
        flags["--rc-addr"] = f"127.0.0.1:{rc_port}"
        flags["--rc-no-auth"] = None
        flags["--read-only"] = None
        flags["--log-level"] = "NOTICE"
        flags.pop("--rc-user", None)
        flags.pop("--rc-pass", None)
        if bandwidth_mbps > 0:
            flags["--bwlimit"] = f"{max(0.125, bandwidth_mbps / 8):g}M"
        return _build_command(prefix, flags)

    def _benchmark_candidate(
        self,
        job: dict[str, Any],
        instance: dict[str, Any],
        profile: dict[str, Any],
        runtime: Path,
        budget: dict[str, int],
        deadline: float,
        cancel: threading.Event,
    ) -> dict[str, Any]:
        candidate_root = runtime / profile["id"]
        setting_comparison = _candidate_setting_comparison(
            instance.get("command") or [], profile["settings"]
        )
        mount_path = candidate_root / "mount"
        cache_root = Path(str(instance.get("cache_dir") or "/cache"))
        optimizer_cache_root = cache_root / ".dumb-rclone-optimizer"
        cache_job_root = optimizer_cache_root / job["job_id"]
        cache_path = cache_job_root / profile["id"]
        uid = int(CONFIG_MANAGER.get("puid") or 1000)
        gid = int(CONFIG_MANAGER.get("pgid") or 1000)
        for directory in (
            self.runtime_dir,
            runtime,
            candidate_root,
            mount_path,
            optimizer_cache_root,
            cache_job_root,
            cache_path,
        ):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chown(directory, uid, gid)
                os.chmod(directory, 0o700)
            except OSError as error:
                raise RcloneOptimizerError(
                    f"Could not prepare the isolated optimizer runtime: {error}"
                ) from None
        rc_port = self._free_rc_port()
        command = self._shadow_command(
            instance,
            profile,
            mount_path,
            cache_path,
            rc_port,
            int(job["limits"].get("bandwidth_limit_mbps") or 0),
        )
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
            user=uid,
            group=gid,
        )
        self._processes[job["job_id"]] = process
        self._wait_for_mount(process, mount_path, rc_port, cancel)
        candidate_started = time.monotonic()
        candidate_started_epoch_ms = int(time.time() * 1000)
        overview_path = "/api/get-overview-stats?window=1h&sections=window,detail"
        before = self._nzbdav_json(overview_path)
        samples = []
        peak_rss_mib = 0.0
        resource_limit_triggered = False
        budget_lock = threading.Lock()
        workers = min(
            int(job["limits"].get("concurrent_streams") or 1),
            len(job["selected_content"]),
        )
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            pending = {
                executor.submit(
                    self._read_sample,
                    mount_path / selected["path"],
                    selected,
                    int(job["limits"]["startup_buffer_mib"]) * 1024**2,
                    budget,
                    budget_lock,
                    deadline,
                    cancel,
                )
                for selected in job["selected_content"]
                if time.monotonic() < deadline and budget["remaining"] > 0
            }
            while pending:
                if cancel.is_set():
                    self._terminate_process(process)
                    self._unmount(mount_path)
                    for future in pending:
                        future.cancel()
                    raise InterruptedError("Optimizer job cancelled by the user.")
                if time.monotonic() >= deadline:
                    self._terminate_process(process)
                    self._unmount(mount_path)
                    for future in pending:
                        future.cancel()
                    job["warnings"].append(
                        f"{profile['label']} reached the configured job deadline; incomplete reads were excluded."
                    )
                    break
                live_resource = self._resource_snapshot(process, cache_path)
                peak_rss_mib = max(
                    peak_rss_mib, float(live_resource.get("rss_mib") or 0)
                )
                try:
                    live_free_disk = shutil.disk_usage(cache_path).free
                except OSError:
                    live_free_disk = 0
                if peak_rss_mib > float(job["limits"]["max_memory_mib"]) or (
                    live_free_disk < int(job["limits"]["min_free_disk_gib"] * 1024**3)
                ):
                    resource_limit_triggered = True
                    self._terminate_process(process)
                    self._unmount(mount_path)
                    for future in pending:
                        future.cancel()
                    break
                completed, pending = wait(
                    pending, timeout=1, return_when=FIRST_COMPLETED
                )
                for future in completed:
                    sample = future.result()
                    samples.append(sample)
                    live_resource = self._resource_snapshot(process, cache_path)
                    peak_rss_mib = max(
                        peak_rss_mib, float(live_resource.get("rss_mib") or 0)
                    )
                    self._update(
                        job,
                        live={
                            "candidate": profile["label"],
                            "content": sample["path"],
                            "active_stream_limit": workers,
                            "bytes_read": sum(
                                item.get("bytes_read", 0) for item in samples
                            ),
                            "rclone": self._rclone_json(rc_port, "/core/stats"),
                            "memory": self._rclone_json(rc_port, "/core/memstats"),
                            "resources": live_resource,
                            "nzbdav": self._summarize_nzbdav(
                                self._nzbdav_json(overview_path)
                            ),
                        },
                    )
        after = self._nzbdav_json(overview_path)
        traces = self._nzbdav_json("/api/get-stream-traces?limit=100")
        trace_capture = self._trace_capture_summary(traces)
        trace_summaries = self._collect_trace_summaries(
            traces,
            [item["path"] for item in job["selected_content"]],
            candidate_started_epoch_ms,
        )
        resource = self._resource_snapshot(process, cache_path)
        resource["rss_mib"] = max(float(resource.get("rss_mib") or 0), peak_rss_mib)
        try:
            free_disk = shutil.disk_usage(cache_path).free
        except OSError:
            free_disk = 0
        resource["free_disk_bytes"] = free_disk
        resource_limit_stop = (
            resource_limit_triggered
            or float(resource.get("rss_mib") or 0)
            > float(job["limits"]["max_memory_mib"])
            or free_disk < int(job["limits"]["min_free_disk_gib"] * 1024**3)
        )
        self._terminate_process(process)
        self._processes.pop(job["job_id"], None)
        if not self._unmount(mount_path):
            raise RcloneOptimizerError(
                f"Could not verify cleanup of the isolated shadow mount for {profile['label']}. The job was stopped and will retry cleanup during finalization and the next DUMB startup."
            )
        values = [sample for sample in samples if sample.get("scored")]
        local_bytes_read = sum(int(sample.get("bytes_read") or 0) for sample in samples)
        provider_bytes_delta = max(
            0,
            int((after or {}).get("totalBytesFetched") or 0)
            - int((before or {}).get("totalBytesFetched") or 0),
        )
        with budget_lock:
            budget["remaining"] = max(
                0,
                budget["remaining"] - max(0, provider_bytes_delta - local_bytes_read),
            )
        avg = lambda key: (
            (sum(float(item.get(key) or 0) for item in values) / len(values))
            if values
            else None
        )
        provider_guard_stop = self._provider_guard(before, after, trace_summaries)
        return {
            "id": profile["id"],
            "label": profile["label"],
            "settings": profile["settings"],
            "setting_comparison": setting_comparison,
            "shadow_mount_cleanup_verified": True,
            "duration_seconds": round(time.monotonic() - candidate_started, 3),
            "samples": samples,
            "summary": {
                "startup_ms": round(avg("startup_ms"), 2) if values else None,
                "ttfb_ms": round(avg("ttfb_ms"), 2) if values else None,
                "throughput_mib_s": (
                    round(avg("throughput_mib_s"), 2) if values else None
                ),
                "seek_ms": round(avg("seek_ms"), 2) if values else None,
                "scored_samples": len(values),
                "excluded_samples": len(samples) - len(values),
            },
            "resources": resource,
            "nzbdav": {
                "before": self._summarize_nzbdav(before),
                "after": self._summarize_nzbdav(after),
            },
            "trace_count": len(trace_summaries),
            "stream_traces": trace_summaries,
            "trace_capture": trace_capture,
            "provider_bytes_delta": provider_bytes_delta,
            "provider_guard_stop": provider_guard_stop,
            "resource_limit_stop": resource_limit_stop,
        }

    def _wait_for_mount(
        self,
        process: subprocess.Popen,
        mount_path: Path,
        rc_port: int,
        cancel: threading.Event,
    ) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if cancel.is_set():
                raise InterruptedError("Optimizer job cancelled by the user.")
            if process.poll() is not None:
                stderr = (process.stderr.read(1000) if process.stderr else "").strip()
                raise RcloneOptimizerError(
                    f"Shadow rclone mount exited during startup: {stderr or 'unknown error'}"
                )
            if (
                os.path.ismount(mount_path)
                and self._rclone_json(rc_port, "/core/version") is not None
            ):
                return
            time.sleep(0.25)
        raise RcloneOptimizerError(
            "Shadow rclone mount did not become ready within 30 seconds."
        )

    @staticmethod
    def _read_sample(
        path: Path,
        selected: dict[str, Any],
        startup_bytes: int,
        budget: dict[str, int],
        budget_lock: threading.Lock,
        deadline: float,
        cancel: threading.Event,
    ) -> dict[str, Any]:
        result = {**selected, "scored": True, "error": None}
        seek_allowance = 1024 * 1024 if selected["size_bytes"] > 32 * 1024**2 else 0
        with budget_lock:
            read_limit = min(
                max(startup_bytes * 2, 64 * 1024**2) + seek_allowance,
                budget["remaining"],
                selected["size_bytes"],
            )
            budget["remaining"] = max(0, budget["remaining"] - read_limit)
        if read_limit <= 0:
            return {
                **result,
                "scored": False,
                "error": "Download budget exhausted before this sample started.",
                "bytes_read": 0,
            }
        started = time.monotonic()
        total = 0
        sequential_limit = max(1, read_limit - seek_allowance)
        try:
            with path.open("rb", buffering=0) as handle:
                opened = time.monotonic()
                first = handle.read(min(1024 * 1024, sequential_limit))
                first_at = time.monotonic()
                if not first:
                    raise OSError("No data was returned.")
                total = len(first)
                startup_at = first_at if total >= startup_bytes else None
                while total < sequential_limit and time.monotonic() < deadline:
                    if cancel.is_set():
                        raise InterruptedError
                    block = handle.read(min(4 * 1024**2, sequential_limit - total))
                    if not block:
                        break
                    total += len(block)
                    if startup_at is None and total >= startup_bytes:
                        startup_at = time.monotonic()
                sequential_done = time.monotonic()
                seek_ms = None
                if (
                    seek_allowance
                    and read_limit - total >= seek_allowance
                    and time.monotonic() < deadline
                ):
                    seek_started = time.monotonic()
                    handle.seek(max(0, selected["size_bytes"] - 16 * 1024**2))
                    seek_data = handle.read(seek_allowance)
                    seek_ms = (time.monotonic() - seek_started) * 1000
                    total += len(seek_data)
                elapsed = max(0.001, sequential_done - first_at)
                result.update(
                    {
                        "open_ms": round((opened - started) * 1000, 2),
                        "ttfb_ms": round((first_at - started) * 1000, 2),
                        "startup_ms": round(
                            ((startup_at or sequential_done) - started) * 1000, 2
                        ),
                        "throughput_mib_s": round((total / 1024**2) / elapsed, 2),
                        "seek_ms": round(seek_ms, 2) if seek_ms is not None else None,
                        "bytes_read": total,
                    }
                )
        except InterruptedError:
            raise
        except OSError as error:
            result.update({"scored": False, "error": str(error)[:500], "bytes_read": 0})
        finally:
            with budget_lock:
                budget["remaining"] += max(0, read_limit - total)
        return result

    def _recommend(
        self, instance: dict[str, Any], results: list[dict[str, Any]]
    ) -> dict[str, Any]:
        viable = [
            result
            for result in results
            if result.get("summary", {}).get("scored_samples", 0) > 0
            and not result.get("provider_guard_stop")
            and not result.get("resource_limit_stop")
        ] or [
            result
            for result in results
            if result.get("summary", {}).get("scored_samples", 0) > 0
        ]
        if not viable:
            raise RcloneOptimizerError(
                "All samples failed or were excluded; no safe recommendation can be made."
            )

        def score(result: dict[str, Any]) -> float:
            summary = result["summary"]
            resource = result.get("resources") or {}
            return (
                float(summary.get("startup_ms") or 1_000_000) * 0.45
                + float(summary.get("ttfb_ms") or 1_000_000) * 0.25
                + float(summary.get("seek_ms") or 0) * 0.15
                - min(float(summary.get("throughput_mib_s") or 0), 500) * 4
                + float(resource.get("rss_mib") or 0) * 0.15
                + int(summary.get("excluded_samples") or 0) * 10_000
            )

        winner = min(viable, key=score)
        settings = winner["settings"] or {
            key: value
            for key, value in _parse_flag_map(instance.get("command") or [])[1].items()
            if key in MANAGED_FLAGS and value is not None
        }
        return {
            "candidate_id": winner["id"],
            "label": winner["label"],
            "settings": settings,
            "setting_comparison": winner.get("setting_comparison") or [],
            "summary": winner["summary"],
            "reason": "Best-performing predefined streaming bundle by the bounded score across startup time, first byte, seek latency, sustained throughput, resource use, and excluded/error samples. NzbDAV's one-week directory and VFS cache recommendations are architecture guidance shared by every candidate, not score-selected values.",
            "confidence_note": "This result compares complete profiles. It does not prove that every individual value in the winning bundle is optimal; the one-week NzbDAV cache timeouts are recommended operational policy rather than benchmark findings.",
            "requires_review": True,
            "applied": False,
        }

    def apply(self, job_id: str) -> dict[str, Any]:
        self._job_path(job_id)
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.get("status") not in {"completed", "rolled_back"}:
                raise RcloneOptimizerError(
                    "Only a completed recommendation can be applied."
                )
            self._update(job, status="applying", stage="Applying recommendation")
        try:
            _name, instance = self._resolve_instance(job["process_name"])
            previous = copy.deepcopy(instance.get("command") or [])
            recommendation_settings = copy.deepcopy(
                (job.get("recommendation") or {}).get("settings") or {}
            )
            recommendation_settings.update(_nzbdav_timeout_recommendations(previous))
            recommended = merge_managed_flags(
                previous,
                recommendation_settings,
            )
            if not recommended:
                raise RcloneOptimizerError(
                    "The job does not contain an applicable recommendation."
                )
            job["previous_command"] = previous
            instance["command"] = recommended
            CONFIG_MANAGER.save_config(job["process_name"])
            self._restart_production_mount(job["process_name"], instance)
            recommendation = copy.deepcopy(job["recommendation"])
            recommendation["settings"] = recommendation_settings
            recommendation["setting_comparison"] = _candidate_setting_comparison(
                previous, recommendation_settings
            )
            recommendation["applied"] = True
            recommendation["applied_at"] = _utcnow()
            self._update(
                job,
                status="applied",
                stage="Applied",
                recommendation=recommendation,
                finished_at=_utcnow(),
            )
            return self._public(job)
        except Exception as error:
            rollback_error = None
            try:
                self._restore_previous(job)
            except Exception as restore_error:
                rollback_error = str(restore_error)
            detail = f"Apply failed and the previous command was restored: {error}"
            if rollback_error:
                detail = (
                    f"Apply failed, and automatic rollback also needs attention: "
                    f"{error}; rollback: {rollback_error}"
                )
            self._update(
                job,
                status="completed",
                stage="Complete",
                error=detail,
            )
            raise RcloneOptimizerError(str(error)) from None

    def rollback(self, job_id: str) -> dict[str, Any]:
        self._job_path(job_id)
        job = self._jobs.get(job_id)
        if not job or job.get("status") != "applied" or "previous_command" not in job:
            raise RcloneOptimizerError(
                "This optimizer job has no applied settings to roll back."
            )
        self._update(job, status="rolling_back", stage="Restoring previous settings")
        try:
            self._restore_previous(job)
        except Exception as error:
            self._update(
                job,
                status="applied",
                stage="Applied",
                error=f"Rollback did not complete: {error}",
            )
            raise RcloneOptimizerError(str(error)) from None
        recommendation = copy.deepcopy(job["recommendation"])
        recommendation["applied"] = False
        recommendation["rolled_back_at"] = _utcnow()
        self._update(
            job,
            status="rolled_back",
            stage="Rolled back",
            recommendation=recommendation,
            finished_at=_utcnow(),
        )
        return self._public(job)

    def _restore_previous(self, job: dict[str, Any]) -> None:
        previous = job.get("previous_command")
        if previous is None:
            return
        _name, instance = self._resolve_instance(job["process_name"])
        instance["command"] = copy.deepcopy(previous)
        CONFIG_MANAGER.save_config(job["process_name"])
        self._restart_production_mount(job["process_name"], instance)

    def _restart_production_mount(
        self, process_name: str, instance: dict[str, Any]
    ) -> None:
        """Restart rclone only after its previous FUSE mount is detached."""
        self.process_handler.stop_process(process_name)
        mount_path = self._mount_path(instance)
        if not self._unmount(mount_path):
            raise RcloneOptimizerError(
                "rclone stopped, but DUMB could not detach its production mount at "
                f"{mount_path}. The saved command was not restarted because the stale "
                "FUSE mount would prevent a safe remount."
            )
        success, error = self.process_handler.start_process(process_name)
        if not success:
            raise RcloneOptimizerError(error or "rclone failed to restart")
        self._wait_for_production_mount(instance)

    @staticmethod
    def _mount_is_accessible(path: Path) -> bool:
        try:
            if not RcloneOptimizerManager._mount_is_registered(path):
                return False
            path.stat()
        except OSError:
            return False
        return True

    @staticmethod
    def _mount_is_registered(path: Path) -> bool:
        target = os.path.normpath(os.path.abspath(os.fspath(path)))
        try:
            with open("/proc/self/mountinfo", encoding="utf-8") as mountinfo:
                for line in mountinfo:
                    fields = line.split()
                    if len(fields) < 5:
                        continue
                    mount_path = fields[4]
                    for escaped, value in (
                        ("\\040", " "),
                        ("\\011", "\t"),
                        ("\\012", "\n"),
                        ("\\134", "\\"),
                    ):
                        mount_path = mount_path.replace(escaped, value)
                    if os.path.normpath(mount_path) == target:
                        return True
            return False
        except OSError:
            try:
                return os.path.ismount(path)
            except OSError:
                return True

    @staticmethod
    def _wait_for_production_mount(instance: dict[str, Any]) -> None:
        path = RcloneOptimizerManager._mount_path(instance)
        deadline = time.monotonic() + 30
        stable_since = None
        while time.monotonic() < deadline:
            if RcloneOptimizerManager._mount_is_accessible(path):
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 2:
                    return
            else:
                stable_since = None
            time.sleep(0.25)
        raise RcloneOptimizerError(
            "rclone restarted but its production mount did not become accessible and remain stable."
        )

    @staticmethod
    def _resource_snapshot(
        process: subprocess.Popen, cache_path: Path
    ) -> dict[str, Any]:
        rss = 0
        cpu = 0.0
        try:
            parent = psutil.Process(process.pid)
            processes = [parent, *parent.children(recursive=True)]
            rss = sum(item.memory_info().rss for item in processes if item.is_running())
            cpu = sum(
                item.cpu_percent(interval=None)
                for item in processes
                if item.is_running()
            )
        except (psutil.Error, OSError):
            pass
        try:
            cache_bytes = sum(
                item.stat().st_size for item in cache_path.rglob("*") if item.is_file()
            )
        except OSError:
            cache_bytes = 0
        return {
            "rss_mib": round(rss / 1024**2, 2),
            "cpu_percent": round(cpu, 2),
            "cache_bytes": cache_bytes,
        }

    def _collect_trace_summaries(
        self, traces: Any, selected_paths: list[str], started_at_ms: int
    ) -> list[dict[str, Any]]:
        if not isinstance(traces, dict):
            return []
        normalized_paths = {
            str(path).replace("\\", "/").strip("/").lower()
            for path in selected_paths
            if isinstance(path, str) and path
        }
        matches = []
        for session in traces.get("sessions") or []:
            if not isinstance(session, dict):
                continue
            path = urllib.parse.unquote(str(session.get("path") or ""))
            if int(session.get("firstAt") or 0) < started_at_ms - 5000:
                continue
            normalized_trace_path = path.replace("\\", "/").strip("/").lower()
            if normalized_paths and not any(
                normalized_trace_path == selected
                or normalized_trace_path.endswith(f"/{selected}")
                for selected in normalized_paths
            ):
                continue
            raw_id = str(session.get("sessionId") or "")
            try:
                session_id = str(UUID(raw_id))
            except (ValueError, TypeError, AttributeError):
                continue
            detail = self._nzbdav_json(
                "/api/get-stream-trace?"
                + urllib.parse.urlencode({"sessionId": session_id})
            )
            events = detail.get("events") if isinstance(detail, dict) else []
            providers = set()
            statuses: dict[str, int] = {}
            kinds: dict[str, int] = {}
            retries = 0
            bytes_served = 0
            provider_wait_ms = 0
            connection_wait_ms = 0
            for event in events or []:
                if not isinstance(event, dict):
                    continue
                provider = str(event.get("provider") or "").strip()
                if provider:
                    providers.add(provider)
                status = str(event.get("status") or "").strip()
                if status:
                    statuses[status] = statuses.get(status, 0) + 1
                kind = str(event.get("kind") or "").strip()
                if kind:
                    kinds[kind] = kinds.get(kind, 0) + 1
                retries += int(event.get("retries") or 0)
                bytes_served += int(event.get("bytesServed") or 0)
                provider_wait_ms += int(event.get("providerWaitMs") or 0)
                connection_wait_ms += int(event.get("connWaitMs") or 0)
            matches.append(
                {
                    "session_id": session_id,
                    "path": path,
                    "first_at": session.get("firstAt"),
                    "last_at": session.get("lastAt"),
                    "event_count": int(session.get("eventCount") or len(events or [])),
                    "providers": sorted(providers),
                    "statuses": statuses,
                    "event_kinds": kinds,
                    "retries": retries,
                    "bytes_served": bytes_served,
                    "provider_wait_ms": provider_wait_ms,
                    "connection_wait_ms": connection_wait_ms,
                }
            )
        return matches[:20]

    @staticmethod
    def _trace_capture_summary(traces: Any) -> dict[str, Any]:
        if not isinstance(traces, dict):
            return {
                "available": False,
                "enabled": False,
                "retained": False,
                "session_count": 0,
                "overflowed": False,
            }
        enabled = traces.get("enabled") is True
        retained = traces.get("retained") is True
        return {
            "available": enabled or retained,
            "enabled": enabled,
            "retained": retained,
            "session_count": int(traces.get("sessionCount") or 0),
            "overflowed": traces.get("overflowed") is True,
        }

    @staticmethod
    def _provider_guard(before: Any, after: Any, traces: Any) -> bool:
        encoded = json.dumps(traces, default=str).lower()
        danger_words = (
            "circuitopen",
            "rate limit",
            "throttled",
            "too many requests",
            "authentication failed",
        )
        if any(word in encoded for word in danger_words):
            return True
        if isinstance(after, dict) and any(
            str(provider.get("circuitState") or "").lower() == "open"
            for provider in (after.get("providers") or [])
            if isinstance(provider, dict)
        ):
            return True

        def counters(value: Any) -> tuple[int, int, int, int]:
            if not isinstance(value, dict):
                return (0, 0, 0, 0)
            providers = value.get("providers") or []
            total_errors = int(value.get("totalErrors") or 0)
            provider_errors = 0
            retries = 0
            for provider in providers:
                if not isinstance(provider, dict):
                    continue
                provider_errors += int(provider.get("errors") or 0)
                retries += int(provider.get("retries") or 0)
            errors = max(total_errors, provider_errors)
            failovers = int((value.get("failover") or {}).get("readsSaved") or 0)
            throttles = int(
                (value.get("tiles") or {}).get("inFlightArticleThrottleEvents") or 0
            )
            return errors, retries, failovers, throttles

        before_errors, before_retries, before_failovers, before_throttles = counters(
            before
        )
        after_errors, after_retries, after_failovers, after_throttles = counters(after)
        return (
            after_errors - before_errors >= 5
            or after_retries - before_retries >= 20
            or after_failovers - before_failovers >= 5
            or after_throttles - before_throttles > 0
        )

    def _nzbdav_json(self, path: str, method: str = "GET") -> Any:
        config = CONFIG_MANAGER.get("nzbdav") or {}
        api_key = (config.get("env") or {}).get("FRONTEND_BACKEND_API_KEY")
        if not api_key:
            try:
                from utils import nzbdav_db

                api_key = nzbdav_db.get_config_value("api.key")
            except (OSError, ValueError, TypeError):
                api_key = None
        if not api_key:
            return None
        port = int(config.get("backend_port") or 8080)
        headers = {"Accept": "application/json", "x-api-key": str(api_key)}
        request_options: dict[str, Any] = {
            "headers": headers,
            "method": method,
        }
        if method.upper() == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            request_options["data"] = b""
        request = safe_request(f"http://127.0.0.1:{port}{path}", **request_options)
        try:
            with safe_urlopen(request, timeout=5) as response:
                raw = response.read(2 * 1024 * 1024)
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None

    def _nzbdav_rc_health(self, instance: dict[str, Any]) -> tuple[bool, str]:
        try:
            enabled = (
                str(self._nzbdav_config_value("rclone.rc-enabled") or "").lower()
                == "true"
            )
            configured_host = str(
                self._nzbdav_config_value("rclone.host") or ""
            ).strip()
            configured_user = str(self._nzbdav_config_value("rclone.user") or "")
            configured_pass = str(self._nzbdav_config_value("rclone.pass") or "")
        except (ImportError, OSError, ValueError, TypeError):
            return False, "DUMB could not read NzbDAV's saved RC configuration"
        if not enabled:
            return False, "RC notifications are disabled in NzbDAV"
        if not configured_host:
            return False, "NzbDAV has no configured rclone RC host"

        flags = _parse_flag_map(instance.get("command") or [])[1]
        if "--rc" not in flags:
            return False, "the production rclone command does not enable RC"
        rc_address = str(flags.get("--rc-addr") or "127.0.0.1:5572").strip()
        if rc_address.startswith(":"):
            rc_address = f"127.0.0.1{rc_address}"
        try:
            rc_url = urllib.parse.urlparse(f"http://{rc_address}")
            rc_port = int(rc_url.port or 5572)
        except (TypeError, ValueError):
            return False, "the production rclone RC address is invalid"
        if rc_url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return False, "the production rclone RC listener is not loopback-only"

        try:
            configured_url = urllib.parse.urlparse(configured_host)
            configured_port = int(configured_url.port or 80)
        except (TypeError, ValueError):
            return False, "NzbDAV's configured rclone RC host is invalid"
        if (
            configured_url.scheme != "http"
            or configured_url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or configured_port != rc_port
        ):
            return False, "NzbDAV's RC host does not match this rclone instance"

        rc_user = str(flags.get("--rc-user") or "")
        rc_pass = str(flags.get("--rc-pass") or "")
        if configured_user != rc_user or configured_pass != rc_pass:
            return False, "NzbDAV's RC credentials do not match this rclone instance"
        if self._rclone_json(rc_port, "/core/version", rc_user, rc_pass) is None:
            return False, "the configured production rclone RC endpoint is unreachable"
        return True, "NzbDAV RC notifications are enabled and reachable"

    @staticmethod
    def _nzbdav_config_value(key: str) -> Any:
        from utils import nzbdav_db

        return nzbdav_db.get_config_value(key)

    @staticmethod
    def _rclone_json(port: int, path: str, user: str = "", password: str = "") -> Any:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if user or password:
            token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        request = safe_request(
            f"http://127.0.0.1:{port}{path}",
            data=b"",
            headers=headers,
            method="POST",
        )
        try:
            with safe_urlopen(request, timeout=2) as response:
                raw = response.read(1024 * 1024)
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None

    @staticmethod
    def _summarize_nzbdav(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {"available": False}
        tiles = data.get("tiles") if isinstance(data.get("tiles"), dict) else {}
        latency = data.get("latency") if isinstance(data.get("latency"), dict) else {}
        failover = (
            data.get("failover") if isinstance(data.get("failover"), dict) else {}
        )
        sessions = (
            data.get("sessions") if isinstance(data.get("sessions"), dict) else {}
        )
        summary = {
            "available": True,
            "window": data.get("window"),
            "active_reads": tiles.get("activeReads"),
            "errors_per_minute": tiles.get("errorsPerMinute"),
            "bytes_served_per_minute": tiles.get("bytesServedPerMinute"),
            "throttle_events": tiles.get("inFlightArticleThrottleEvents"),
            "total_errors": data.get("totalErrors"),
            "total_bytes_fetched": data.get("totalBytesFetched"),
            "provider_latency_p50_ms": latency.get("p50Ms"),
            "provider_latency_p95_ms": latency.get("p95Ms"),
            "provider_latency_p99_ms": latency.get("p99Ms"),
            "session_count": sessions.get("count"),
            "failover": failover,
        }
        providers = data.get("providers")
        if isinstance(providers, list):
            summary["providers"] = [
                {
                    "provider": provider.get("nickname") or provider.get("provider"),
                    "bytes_fetched": provider.get("bytesFetched"),
                    "errors": provider.get("errors"),
                    "retries": provider.get("retries"),
                    "average_duration_ms": provider.get("avgDurationMs"),
                    "circuit_state": provider.get("circuitState"),
                }
                for provider in providers[:20]
                if isinstance(provider, dict)
            ]
        return summary

    @staticmethod
    def _free_rc_port() -> int:
        for _attempt in range(200):
            port = random.randint(40000, 59999)
            if is_port_available(port):
                return port
        raise RcloneOptimizerError(
            "No loopback RC port is available for the shadow mount."
        )

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except OSError:
                process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _unmount(path: Path) -> bool:
        if not RcloneOptimizerManager._mount_is_registered(path):
            return True
        for command in (
            ["fusermount3", "-uz", str(path)],
            ["fusermount", "-uz", str(path)],
            ["umount", "-l", str(path)],
        ):
            if not shutil.which(command[0]):
                continue
            try:
                result = subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode == 0:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if not RcloneOptimizerManager._mount_is_registered(path):
                        return True
                    time.sleep(0.1)
            elif not RcloneOptimizerManager._mount_is_registered(path):
                return True
        return not RcloneOptimizerManager._mount_is_registered(path)

    def shutdown(self) -> None:
        self._shutdown.set()
        for event in list(self._cancel.values()):
            event.set()
        for process in list(self._processes.values()):
            self._terminate_process(process)
        for worker in list(self._threads.values()):
            worker.join(timeout=5)
        self._cleanup_runtime_root()
