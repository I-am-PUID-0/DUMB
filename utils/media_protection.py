"""Protect media-server libraries while storage dependencies are unavailable.

The coordinator deliberately separates planned maintenance from unexpected
outages. Planned work may stop an idle media server, while outage handling
first inhibits scans and preserves active playback. All vendor mutations are
snapshotted so they can be restored after the dependency is stable again.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from utils.config_loader import CONFIG_MANAGER
from utils.notifications import notify_event
from utils.private_files import atomic_write_private_text

MEDIA_KEYS = ("plex", "jellyfin", "emby")
STORAGE_KEYS = {
    "rclone",
    "nzbdav",
    "decypharr",
    "zurg",
    "altmount",
    "cli_debrid",
}
PLEX_LIBRARY_SETTING_IDS = (
    "autoEmptyTrash",
    "fSEventLibraryUpdatesEnabled",
    "fSEventLibraryPartialScanEnabled",
    "scheduledLibraryUpdatesEnabled",
    "scheduledLibraryUpdateInterval",
)
STATE_PATH = "/config/media-protection/state.json"


def _normalize_name(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _process_config(process_name: str) -> tuple[str | None, dict | None]:
    key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
    if not key:
        return None, None
    return key, CONFIG_MANAGER.get_instance(instance_name, key)


def _media_processes() -> list[tuple[str, str, dict]]:
    result = []
    for key in MEDIA_KEYS:
        config = CONFIG_MANAGER.get(key, {}) or {}
        if not isinstance(config, dict) or not config.get("enabled"):
            continue
        process_name = str(config.get("process_name") or "").strip()
        if process_name:
            result.append((key, process_name, config))
    return result


def _global_config() -> dict:
    dumb = CONFIG_MANAGER.get("dumb", {}) or {}
    root = dumb.get("media_protection")
    if not isinstance(root, dict):
        root = {
            "enabled": True,
            "recovery_stabilization_seconds": 30,
            "recovery_timeout_seconds": 180,
            "monitor_interval_seconds": 5,
            "services": [],
        }
        dumb["media_protection"] = root
    return root


def protection_policy(process_name: str) -> dict:
    root = _global_config()
    policy = {
        "process_name": process_name,
        "enabled": True,
        "api_key": "",
        "stop_when_idle_on_outage": True,
        "protected_mounts": [],
    }
    target = _normalize_name(process_name)
    for entry in root.get("services", []) or []:
        if not isinstance(entry, dict):
            continue
        if _normalize_name(entry.get("process_name")) == target:
            policy.update(entry)
            break
    policy["enabled"] = (
        bool(root.get("enabled", True)) and policy.get("enabled", True) is not False
    )
    policy["api_key_configured"] = bool(str(policy.get("api_key") or "").strip())
    return policy


def public_policy(process_name: str) -> dict:
    policy = copy.deepcopy(protection_policy(process_name))
    policy["api_key"] = ""
    return policy


def save_policy(process_name: str, updates: dict) -> dict:
    root = _global_config()
    services = root.setdefault("services", [])
    target = _normalize_name(process_name)
    current = None
    for entry in services:
        if (
            isinstance(entry, dict)
            and _normalize_name(entry.get("process_name")) == target
        ):
            current = entry
            break
    if current is None:
        current = {"process_name": process_name}
        services.append(current)

    allowed = {
        "enabled",
        "api_key",
        "stop_when_idle_on_outage",
        "protected_mounts",
    }
    for key, value in updates.items():
        if key not in allowed:
            continue
        if key == "api_key":
            value = str(value or "").strip()
        if key == "protected_mounts":
            value = sorted(
                {
                    str(path).strip()
                    for path in (value or [])
                    if str(path).strip().startswith("/")
                }
            )
        current[key] = value
    CONFIG_MANAGER.save_config()
    return public_policy(process_name)


def save_global_settings(updates: dict) -> dict:
    root = _global_config()
    bounds = {
        "recovery_stabilization_seconds": (5, 600),
        "recovery_timeout_seconds": (30, 3600),
        "monitor_interval_seconds": (2, 60),
    }
    if "enabled" in updates:
        root["enabled"] = bool(updates["enabled"])
    for key, (minimum, maximum) in bounds.items():
        if key in updates and updates[key] is not None:
            root[key] = max(minimum, min(maximum, int(updates[key])))
    CONFIG_MANAGER.save_config()
    return {
        "enabled": bool(root.get("enabled", True)),
        **{key: int(root.get(key, minimum)) for key, (minimum, _) in bounds.items()},
    }


class MediaServerAdapter:
    def __init__(self, key: str, process_name: str, config: dict, policy: dict, logger):
        self.key = key
        self.process_name = process_name
        self.config = config
        self.policy = policy
        self.logger = logger

    def activity(self) -> dict:
        raise NotImplementedError

    def enter_scan_guard(self) -> dict:
        raise NotImplementedError

    def restore_scan_guard(self, snapshot: dict) -> list[str]:
        raise NotImplementedError


class PlexAdapter(MediaServerAdapter):
    def _connect(self):
        from utils.plex_dbrepair import _plex_token, _plex_url
        from plexapi.server import PlexServer

        dumb = CONFIG_MANAGER.get("dumb", {}) or {}
        token = _plex_token(self.config, dumb)
        if not token:
            raise RuntimeError("Plex token is not configured")
        return PlexServer(_plex_url(self.config, dumb), token, timeout=8)

    def activity(self) -> dict:
        try:
            from utils.plex_dbrepair import _has_scheduled_recordings

            plex = self._connect()
            sessions = plex.sessions()
            recording = _has_scheduled_recordings(plex)
            if recording is None:
                raise RuntimeError("Plex DVR activity could not be determined")
            return {
                "state": "busy" if sessions or recording else "idle",
                "active_sessions": len(sessions),
                "recording": bool(recording),
                "reason": (
                    "Active playback or DVR recording"
                    if sessions or recording
                    else None
                ),
            }
        except Exception as error:
            return {
                "state": "unknown",
                "active_sessions": None,
                "recording": None,
                "reason": str(error),
            }

    def library_settings(self) -> dict:
        plex = self._connect()
        values = {}
        for setting_id in PLEX_LIBRARY_SETTING_IDS:
            setting = plex.settings.get(setting_id)
            values[setting_id] = {
                "value": setting.value,
                "label": setting.label,
                "summary": setting.summary,
                "type": setting.type,
                "choices": setting.enumValues,
            }
        return values

    def update_library_settings(self, updates: dict) -> dict:
        plex = self._connect()
        changed = False
        for setting_id, value in updates.items():
            if setting_id not in PLEX_LIBRARY_SETTING_IDS:
                continue
            setting = plex.settings.get(setting_id)
            if setting.value != value:
                setting.set(value)
                changed = True
        if changed:
            plex.settings.save()
        return self.library_settings()

    def enter_scan_guard(self) -> dict:
        plex = self._connect()
        snapshot = {"settings": {}, "changed": []}
        for setting_id in (
            "autoEmptyTrash",
            "fSEventLibraryUpdatesEnabled",
            "scheduledLibraryUpdatesEnabled",
        ):
            setting = plex.settings.get(setting_id)
            snapshot["settings"][setting_id] = setting.value
            if setting.value is not False:
                setting.set(False)
                snapshot["changed"].append(setting_id)
        if snapshot["changed"]:
            plex.settings.save()
        try:
            plex.library.cancelUpdate()
        except Exception as error:
            self.logger.warning("Could not cancel Plex library scan: %s", error)
        return snapshot

    def restore_scan_guard(self, snapshot: dict) -> list[str]:
        plex = self._connect()
        restored = []
        for setting_id in snapshot.get("changed", []):
            original = (snapshot.get("settings") or {}).get(setting_id)
            setting = plex.settings.get(setting_id)
            if setting.value is False and original is not False:
                setting.set(original)
                restored.append(setting_id)
        if restored:
            plex.settings.save()
        return restored


class MediaBrowserAdapter(MediaServerAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        host = str(self.config.get("host") or "127.0.0.1")
        port = int(self.config.get("port") or 8096)
        self.base_url = f"http://{host}:{port}"
        self.api_key = str(self.policy.get("api_key") or "").strip()

    def _request(self, method: str, path: str, **kwargs):
        if not self.api_key:
            raise RuntimeError(f"{self.key.title()} API key is not configured")
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["X-Emby-Token"] = self.api_key
        response = requests.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            timeout=8,
            **kwargs,
        )
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    @staticmethod
    def _scan_tasks(tasks: list[dict]) -> list[dict]:
        matches = []
        for task in tasks:
            label = " ".join(
                str(task.get(field) or "")
                for field in ("Key", "Name", "Description", "Category")
            ).lower()
            if "scan media library" in label or "refreshlibrary" in label:
                matches.append(task)
        return matches

    def activity(self) -> dict:
        try:
            sessions = self._request("GET", "/Sessions") or []
            active = [session for session in sessions if session.get("NowPlayingItem")]
            tasks = self._request("GET", "/ScheduledTasks") or []
            running = [
                task
                for task in self._scan_tasks(tasks)
                if str(task.get("State") or "").lower() in {"running", "cancelling"}
            ]
            busy = bool(active or running)
            return {
                "state": "busy" if busy else "idle",
                "active_sessions": len(active),
                "active_scans": len(running),
                "reason": "Active playback or library scan" if busy else None,
            }
        except Exception as error:
            return {
                "state": "unknown",
                "active_sessions": None,
                "active_scans": None,
                "reason": str(error),
            }

    def _virtual_folders(self) -> list[dict]:
        try:
            result = self._request("GET", "/Library/VirtualFolders")
        except Exception:
            result = self._request("GET", "/Library/VirtualFolders/Query")
        if isinstance(result, dict):
            return result.get("Items") or result.get("items") or []
        return result or []

    def _update_library_options(self, folder: dict, options: dict) -> None:
        item_id = folder.get("ItemId") or folder.get("Id")
        if not item_id:
            return
        self._request(
            "POST",
            "/Library/VirtualFolders/LibraryOptions",
            json={"Id": item_id, "LibraryOptions": options},
        )

    def enter_scan_guard(self) -> dict:
        tasks = self._request("GET", "/ScheduledTasks") or []
        snapshot = {"tasks": [], "libraries": []}
        for task in self._scan_tasks(tasks):
            task_id = task.get("Id") or task.get("Key")
            if not task_id:
                continue
            triggers = copy.deepcopy(task.get("Triggers") or [])
            snapshot["tasks"].append({"id": task_id, "triggers": triggers})
            if str(task.get("State") or "").lower() in {"running", "cancelling"}:
                try:
                    self._request("DELETE", f"/ScheduledTasks/Running/{task_id}")
                except Exception as error:
                    self.logger.warning(
                        "Could not stop %s library scan: %s", self.key, error
                    )
            if triggers:
                self._request("POST", f"/ScheduledTasks/{task_id}/Triggers", json=[])

        for folder in self._virtual_folders():
            options = copy.deepcopy(folder.get("LibraryOptions") or {})
            if options.get("EnableRealtimeMonitor") is not True:
                continue
            snapshot["libraries"].append(
                {
                    "id": folder.get("ItemId") or folder.get("Id"),
                    "options": options,
                }
            )
            guarded = copy.deepcopy(options)
            guarded["EnableRealtimeMonitor"] = False
            self._update_library_options(folder, guarded)
        return snapshot

    def restore_scan_guard(self, snapshot: dict) -> list[str]:
        restored = []
        current_tasks = self._request("GET", "/ScheduledTasks") or []
        current_by_id = {
            str(task.get("Id") or task.get("Key")): task for task in current_tasks
        }
        for task in snapshot.get("tasks", []):
            task_id = str(task.get("id") or "")
            current = current_by_id.get(task_id) or {}
            if not (current.get("Triggers") or []) and task.get("triggers"):
                self._request(
                    "POST",
                    f"/ScheduledTasks/{task_id}/Triggers",
                    json=task["triggers"],
                )
                restored.append(f"task:{task_id}")
        current_folders = {
            str(folder.get("ItemId") or folder.get("Id")): folder
            for folder in self._virtual_folders()
        }
        for library in snapshot.get("libraries", []):
            item_id = str(library.get("id") or "")
            current = current_folders.get(item_id) or {}
            options = copy.deepcopy(current.get("LibraryOptions") or {})
            if options.get("EnableRealtimeMonitor") is False:
                self._update_library_options(current, library.get("options") or options)
                restored.append(f"library:{item_id}")
        return restored


def build_adapter(key: str, process_name: str, logger) -> MediaServerAdapter:
    config = CONFIG_MANAGER.get(key, {}) or {}
    policy = protection_policy(process_name)
    if key == "plex":
        return PlexAdapter(key, process_name, config, policy, logger)
    return MediaBrowserAdapter(key, process_name, config, policy, logger)


class MediaProtectionManager:
    def __init__(self, process_handler, logger, state_path: str = STATE_PATH):
        self.process_handler = process_handler
        self.logger = logger
        self.state_path = Path(state_path)
        self.lock = threading.RLock()
        self.incidents: dict[str, dict] = {}
        self._stop_event = threading.Event()
        self._monitor_thread = None
        self._load()

    def start(self):
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor,
            daemon=True,
            name="media-protection-monitor",
        )
        self._monitor_thread.start()

    def shutdown(self):
        self._stop_event.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=3)

    def _load(self):
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            incidents = payload.get("incidents") if isinstance(payload, dict) else None
            if isinstance(incidents, dict):
                self.incidents = incidents
                for incident in self.incidents.values():
                    if incident.get("status") in {"recovering", "recovery_failed"}:
                        incident["status"] = "waiting_for_recovery"
        except FileNotFoundError:
            return
        except Exception as error:
            self.logger.warning("Failed to load media protection state: %s", error)

    def _save(self):
        payload = {"version": 1, "incidents": self.incidents}
        atomic_write_private_text(
            self.state_path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )

    def _is_running(self, process_name: str) -> bool:
        target = _normalize_name(process_name)
        for info in list(self.process_handler.processes.values()):
            if _normalize_name(info.get("name")) != target:
                continue
            process = info.get("process_obj")
            return bool(process and process.poll() is None)
        return False

    @staticmethod
    def _mounts_for_target(key: str, config: dict) -> set[str]:
        mounts: set[str] = set()
        if key == "rclone":
            mount_dir = config.get("mount_dir")
            mount_name = config.get("mount_name")
            if mount_dir and mount_name:
                mounts.add(os.path.normpath(os.path.join(mount_dir, mount_name)))
        elif key == "decypharr":
            if config.get("mount_path"):
                mounts.add(os.path.normpath(config["mount_path"]))
        elif key == "altmount":
            for field in ("mount_path", "mount_dir"):
                if config.get(field):
                    mounts.add(os.path.normpath(config[field]))
        elif key in {"nzbdav", "zurg"}:
            rclone_instances = (CONFIG_MANAGER.get("rclone", {}) or {}).get(
                "instances", {}
            ) or {}
            for instance in rclone_instances.values():
                if not isinstance(instance, dict) or not instance.get("enabled"):
                    continue
                matches = key == "nzbdav" and (
                    str(instance.get("key_type") or "").lower() == "nzbdav"
                    or str(instance.get("core_service") or "").lower() == "nzbdav"
                )
                matches = matches or (key == "zurg" and instance.get("zurg_enabled"))
                if matches and instance.get("mount_dir") and instance.get("mount_name"):
                    mounts.add(
                        os.path.normpath(
                            os.path.join(instance["mount_dir"], instance["mount_name"])
                        )
                    )
        return mounts

    def affected_media_servers(self, process_name: str) -> list[dict]:
        key, config = _process_config(process_name)
        if (
            key not in STORAGE_KEYS
            or not config
            or not _global_config().get("enabled", True)
        ):
            return []
        target_mounts = self._mounts_for_target(key, config)
        affected = []
        for media_key, media_name, media_config in _media_processes():
            policy = protection_policy(media_name)
            if not policy.get("enabled"):
                continue
            # A media server that was already stopped has no active playback
            # or scan behavior to guard, and DUMB must not create a protection
            # incident that implies it changed or will recover that server.
            if not self._is_running(media_name):
                continue
            configured_mounts = {
                os.path.normpath(path)
                for path in (policy.get("protected_mounts") or [])
                if str(path).startswith("/")
            }
            media_mounts = configured_mounts or {
                os.path.normpath(path)
                for path in (media_config.get("wait_for_mounts") or [])
                if str(path).startswith("/")
            }
            if (
                target_mounts
                and media_mounts
                and target_mounts.isdisjoint(media_mounts)
            ):
                continue
            affected.append(
                {
                    "key": media_key,
                    "process_name": media_name,
                    "running": True,
                    "protected_mounts": sorted(media_mounts),
                    "target_mounts": sorted(target_mounts),
                    "policy": public_policy(media_name),
                }
            )
        return affected

    def preflight(self, process_name: str, action: str) -> dict:
        media = self.affected_media_servers(process_name)
        busy = False
        unknown = False
        for entry in media:
            if not entry["running"]:
                activity = {"state": "stopped", "active_sessions": 0}
            else:
                activity = build_adapter(
                    entry["key"], entry["process_name"], self.logger
                ).activity()
            entry["activity"] = activity
            busy = busy or activity.get("state") == "busy"
            unknown = unknown or activity.get("state") == "unknown"
        return {
            "protected": bool(media),
            "process_name": process_name,
            "action": action,
            "blocked": bool(media) and (busy or unknown),
            "busy": busy,
            "unknown": unknown,
            "media_servers": media,
        }

    def begin_planned(
        self, process_name: str, action: str, override: str | None = None
    ) -> dict:
        override = str(override or "safe").strip().lower()
        if override not in {"safe", "keep_running", "stop_now"}:
            override = "safe"
        preflight = self.preflight(process_name, action)
        if not preflight["protected"]:
            return {"status": "not_applicable", "token": None, "preflight": preflight}
        if preflight["blocked"] and override == "safe":
            return {"status": "deferred", "token": None, "preflight": preflight}

        token = uuid.uuid4().hex
        incident = {
            "id": token,
            "kind": "planned",
            "target_process": process_name,
            "action": action,
            "status": "active",
            "started_at": time.time(),
            "hold_until_recovery": action == "stop",
            "media_servers": [],
        }
        with self.lock:
            self.incidents[token] = incident
            self._save()
        for entry in preflight["media_servers"]:
            state = {
                "key": entry["key"],
                "process_name": entry["process_name"],
                "was_running": entry["running"],
                "stopped_by_dumb": False,
                "guard_snapshot": None,
                "guard_error": None,
            }
            if entry["running"]:
                adapter = build_adapter(
                    entry["key"], entry["process_name"], self.logger
                )
                try:
                    state["guard_snapshot"] = adapter.enter_scan_guard()
                except Exception as error:
                    state["guard_error"] = str(error)
                    self.logger.warning(
                        "Could not fully guard %s scans: %s",
                        entry["process_name"],
                        error,
                    )
                if override != "keep_running":
                    state["stopped_by_dumb"] = True
            incident["media_servers"].append(state)
            with self.lock:
                self._save()
            if state["stopped_by_dumb"]:
                self.process_handler.stop_process(entry["process_name"])
        notify_event(
            "media.protection.activated",
            "warning",
            f"Media protection active for {process_name}",
            f"DUMB protected {len(incident['media_servers'])} downstream media server(s) before {action}.",
            service_name=process_name,
        )
        return {"status": "protected", "token": token, "preflight": preflight}

    def begin_unplanned(self, process_name: str, reason: str):
        target = _normalize_name(process_name)
        with self.lock:
            for token, incident in self.incidents.items():
                if _normalize_name(
                    incident.get("target_process")
                ) == target and incident.get("status") in {
                    "active",
                    "waiting_for_recovery",
                }:
                    return token
        preflight = self.preflight(process_name, "unexpected_outage")
        if not preflight["protected"]:
            return None
        token = uuid.uuid4().hex
        incident = {
            "id": token,
            "kind": "unexpected",
            "target_process": process_name,
            "action": "unexpected_outage",
            "status": "active",
            "reason": reason,
            "started_at": time.time(),
            "hold_until_recovery": True,
            "media_servers": [],
        }
        with self.lock:
            self.incidents[token] = incident
            self._save()
        for entry in preflight["media_servers"]:
            state = {
                "key": entry["key"],
                "process_name": entry["process_name"],
                "was_running": entry["running"],
                "stopped_by_dumb": False,
                "guard_snapshot": None,
                "guard_error": None,
            }
            if entry["running"]:
                adapter = build_adapter(
                    entry["key"], entry["process_name"], self.logger
                )
                try:
                    state["guard_snapshot"] = adapter.enter_scan_guard()
                except Exception as error:
                    state["guard_error"] = str(error)
                activity = entry.get("activity") or {}
                if (
                    entry["policy"].get("stop_when_idle_on_outage", True)
                    and activity.get("state") == "idle"
                ):
                    state["stopped_by_dumb"] = True
            incident["media_servers"].append(state)
            with self.lock:
                self._save()
            if state["stopped_by_dumb"]:
                self.process_handler.stop_process(entry["process_name"])
        notify_event(
            "media.protection.outage",
            "critical",
            f"Storage outage protection active for {process_name}",
            "DUMB inhibited downstream library scans and preserved active playback where possible.",
            service_name=process_name,
            metadata={"reason": reason},
        )
        return token

    def handle_unexpected_exit(self, process_name: str, reason: str):
        if self.process_handler.startup_phase not in {"ready", "degraded"}:
            return
        if not self.affected_media_servers(process_name):
            return
        threading.Thread(
            target=self.begin_unplanned,
            args=(process_name, reason),
            daemon=True,
            name=f"media-protection-{_normalize_name(process_name)}",
        ).start()

    def complete_planned(self, token: str | None, success: bool = True):
        if not token:
            return
        with self.lock:
            incident = self.incidents.get(token)
            if not incident:
                return
            if incident.get("hold_until_recovery") or not self._dependency_ready(
                incident
            ):
                incident["operation_success"] = bool(success)
                incident["status"] = "waiting_for_recovery"
                self._save()
                return
        self._recover(token)

    def _dependency_ready(self, incident: dict) -> bool:
        process_name = incident.get("target_process")
        if not self._is_running(process_name):
            return False
        try:
            pid, _ = self.process_handler._find_process_entry(process_name)
            health = self.process_handler.get_process_health(process_name, pid)
            if health.get("status") in {"unhealthy", "starting"}:
                return False
        except Exception:
            return False
        key, config = _process_config(process_name)
        for mount_path in self._mounts_for_target(key, config or {}):
            if not os.path.ismount(mount_path):
                return False
            try:
                with os.scandir(mount_path) as entries:
                    next(entries, None)
            except OSError:
                return False
        return True

    def _recover(self, token: str):
        with self.lock:
            incident = self.incidents.get(token)
            if not incident:
                return
            first_failure = not incident.get("recovery_failure_notified")
            incident["status"] = "recovering"
            self._save()
        errors = []
        for state in incident.get("media_servers", []):
            process_name = state.get("process_name")
            if (
                state.get("stopped_by_dumb")
                and state.get("was_running")
                and not self._is_running(process_name)
            ):
                success, error = self.process_handler.start_process(process_name)
                if not success:
                    errors.append(f"{process_name} start: {error}")
                    continue
            snapshot = state.get("guard_snapshot")
            if snapshot:
                try:
                    build_adapter(
                        state["key"], process_name, self.logger
                    ).restore_scan_guard(snapshot)
                except Exception as error:
                    errors.append(f"{process_name} scan settings: {error}")
        with self.lock:
            incident["status"] = "recovery_failed" if errors else "recovered"
            incident["recovered_at"] = time.time()
            incident["errors"] = errors
            if errors:
                incident["recovery_failure_notified"] = True
                incident["next_recovery_attempt_at"] = time.time() + 60
            else:
                incident.pop("next_recovery_attempt_at", None)
            self._save()
        if not errors or first_failure:
            severity = "critical" if errors else "success"
            notify_event(
                (
                    "media.protection.recovery_failed"
                    if errors
                    else "media.protection.recovered"
                ),
                severity,
                f"Media protection recovery {'needs attention' if errors else 'completed'}",
                (
                    "; ".join(errors)
                    if errors
                    else "Scan settings and DUMB-stopped media servers were restored."
                ),
                service_name=incident.get("target_process"),
            )

    def _monitor(self):
        stable_since: dict[str, float] = {}
        while not self._stop_event.is_set():
            root = _global_config()
            interval = max(1.0, float(root.get("monitor_interval_seconds", 5) or 5))
            stabilization = max(
                0.0, float(root.get("recovery_stabilization_seconds", 30) or 30)
            )
            recovery_timeout = max(
                30.0, float(root.get("recovery_timeout_seconds", 180) or 180)
            )
            with self.lock:
                active = [
                    (token, copy.deepcopy(incident))
                    for token, incident in self.incidents.items()
                    if incident.get("status")
                    in {"active", "waiting_for_recovery", "recovery_failed"}
                ]
            for token, incident in active:
                if incident.get("status") == "recovery_failed" and time.time() < float(
                    incident.get("next_recovery_attempt_at") or 0
                ):
                    continue
                timed_out = (
                    incident.get("action") != "stop"
                    and time.time() - float(incident.get("started_at") or time.time())
                    >= recovery_timeout
                    and not incident.get("recovery_timeout_notified")
                )
                if timed_out:
                    with self.lock:
                        current = self.incidents.get(token)
                        if current and not current.get("recovery_timeout_notified"):
                            current["recovery_timeout_notified"] = time.time()
                            self._save()
                    notify_event(
                        "media.protection.recovery_failed",
                        "critical",
                        "Media protection recovery is delayed",
                        "The storage dependency has not remained healthy long enough to restore its media servers.",
                        service_name=incident.get("target_process"),
                    )
                # During an unexpected outage, stop a guarded server once its
                # users have naturally finished, if that policy remains enabled.
                if incident.get("kind") == "unexpected":
                    for state in incident.get("media_servers", []):
                        if state.get("stopped_by_dumb") or not self._is_running(
                            state.get("process_name")
                        ):
                            continue
                        policy = protection_policy(state.get("process_name"))
                        if not policy.get("stop_when_idle_on_outage", True):
                            continue
                        activity = build_adapter(
                            state.get("key"), state.get("process_name"), self.logger
                        ).activity()
                        if activity.get("state") == "idle":
                            self.process_handler.stop_process(state.get("process_name"))
                            with self.lock:
                                current = self.incidents.get(token)
                                if current:
                                    for current_state in current.get(
                                        "media_servers", []
                                    ):
                                        if current_state.get(
                                            "process_name"
                                        ) == state.get("process_name"):
                                            current_state["stopped_by_dumb"] = True
                                    self._save()

                if self._dependency_ready(incident):
                    stable_since[token] = stable_since.get(token) or time.monotonic()
                    if time.monotonic() - stable_since[token] >= stabilization:
                        self._recover(token)
                        stable_since.pop(token, None)
                else:
                    stable_since.pop(token, None)
            self._stop_event.wait(interval)

    def status(self, process_name: str | None = None) -> dict:
        root = _global_config()
        with self.lock:
            incidents = list(copy.deepcopy(self.incidents).values())
        if process_name:
            target = _normalize_name(process_name)
            incidents = [
                incident
                for incident in incidents
                if _normalize_name(incident.get("target_process")) == target
                or any(
                    _normalize_name(entry.get("process_name")) == target
                    for entry in incident.get("media_servers", [])
                )
            ]
        for incident in incidents:
            for entry in incident.get("media_servers", []):
                entry.pop("guard_snapshot", None)
        return {
            "enabled": bool(root.get("enabled", True)),
            "recovery_stabilization_seconds": int(
                root.get("recovery_stabilization_seconds", 30)
            ),
            "recovery_timeout_seconds": int(root.get("recovery_timeout_seconds", 180)),
            "monitor_interval_seconds": int(root.get("monitor_interval_seconds", 5)),
            "active": [
                incident
                for incident in incidents
                if incident.get("status") not in {"recovered"}
            ],
            "recent": sorted(
                incidents, key=lambda item: item.get("started_at", 0), reverse=True
            )[:20],
        }


def plex_library_settings(logger) -> dict:
    config = CONFIG_MANAGER.get("plex", {}) or {}
    process_name = config.get("process_name", "Plex Media Server")
    adapter = build_adapter("plex", process_name, logger)
    return adapter.library_settings()


def update_plex_library_settings(updates: dict, logger) -> dict:
    config = CONFIG_MANAGER.get("plex", {}) or {}
    process_name = config.get("process_name", "Plex Media Server")
    adapter = build_adapter("plex", process_name, logger)
    return adapter.update_library_settings(updates)
