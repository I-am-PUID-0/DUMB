"""Scoped native configuration, with verified stop and compensation."""

from __future__ import annotations

import copy
import base64
import json
import os
from pathlib import Path
import stat

from utils.private_files import atomic_write_private_text


def _state(execution, reason, *, retryable=False):
    return {
        "execution": execution,
        "verification": "not_checked",
        "reason": reason,
        "retryable": retryable,
    }


def _tracked(handler, name):
    processes = getattr(handler, "process_names", {})
    process = processes.get(name)
    if process is None and hasattr(handler, "_prefixed_name"):
        process = processes.get(handler._prefixed_name(name))
    return process


def _stop_verified(handler, name):
    process = _tracked(handler, name)
    if process is None:
        return False
    try:
        group = os.getpgid(process.pid) if process.poll() is None else None
    except ProcessLookupError:
        group = None
    try:
        handler.stop_process(name, disable_restart=True)
    except Exception:
        return False
    if process.poll() is None:
        return False
    # ProcessHandler can swallow a failed stop. Independently check the captured
    # process group rather than relying on its now-removed tracking entry.
    if group is not None:
        if group == os.getpgrp():
            return False
        alive = getattr(handler, "_process_group_alive", None)
        if alive is None or alive(group):
            return False
    return True


def _snapshot(path):
    with open(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb", closefd=True) as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("Not a regular configuration")
        value = handle.read(2 * 1024 * 1024 + 1)
    if len(value) > 2 * 1024 * 1024:
        raise ValueError("Configuration too large")
    value.decode("utf-8")
    return value


def _discard_backup(path):
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _capture_sidecars(paths):
    result = []
    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            result.append({"path": str(path), "present": False})
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Unsafe setup sidecar")
        result.append(
            {
                "path": str(path),
                "present": True,
                "content": base64.b64encode(_snapshot(path)).decode("ascii"),
                "owner": [info.st_uid, info.st_gid],
            }
        )
    return result


def _restore_sidecars(snapshots):
    for snapshot in snapshots:
        path = Path(snapshot["path"])
        if snapshot["present"]:
            atomic_write_private_text(
                path,
                base64.b64decode(snapshot["content"]).decode("utf-8"),
                owner=tuple(snapshot["owner"]),
            )
        else:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Unsafe new setup sidecar")
            path.unlink()


def configure_tautulli(action, config, payload, handler, updater, inspect, apply):
    cfg = config.get("tautulli") or {}
    return _configure_stopped(
        action,
        config,
        payload,
        handler,
        updater,
        inspect,
        apply,
        cfg,
        Path(cfg.get("config_file") or "/tautulli/data/config.ini"),
        "Tautulli",
    )


def configure_discovery(
    action, config, payload, handler, updater, inspect, apply, *, manager=None
):
    from functools import partial
    from utils.arrangement_discovery import native_config_path
    from utils.arrangement_plan import resource_config

    if (action.get("service_key"), action.get("action")) not in {
        ("cli_debrid", "companion_connection"),
        ("zurg", "library_hook"),
    }:
        raise ValueError("Unsupported controlled setup action")
    cfg = resource_config(config, action["source"])
    path = native_config_path(action, config)
    if not path:
        return _state(
            "waiting_input", "Wait for the service's generated native configuration."
        )
    return _configure_stopped(
        action,
        config,
        payload,
        handler,
        updater,
        inspect,
        (
            partial(apply, manager=manager)
            if action["service_key"] == "cli_debrid"
            else apply
        ),
        cfg,
        Path(path),
        "CLI Debrid" if action["service_key"] == "cli_debrid" else "Zurg",
        manager=manager if action["service_key"] == "cli_debrid" else None,
        sidecars=[
            Path(path).parent / name
            for name in (
                (".dumb-zurg-library-hook.json", ".dumb-zurg-hook-ownership.json")
                if action["service_key"] == "zurg"
                else (".dumb-companion-ownership.json",)
            )
        ]
        + (
            [
                Path(cfg.get("config_dir") or "/cli_debrid")
                / ".dumb-phalanx-environment.json"
            ]
            if action["service_key"] == "cli_debrid"
            else []
        ),
    )


def configure_altmount_webdav(
    action, config, payload, handler, updater, inspect, apply
):
    from utils.arrangement_altmount_account import native_config_path

    path = native_config_path(action, config)
    return _configure_stopped(
        action,
        config,
        payload,
        handler,
        updater,
        inspect,
        apply,
        config.get("altmount") or {},
        path,
        "AltMount",
        sidecars=[
            path.parent / ".dumb-altmount-webdav-ownership.json",
            path.parent / ".dumb-altmount-webdav-preview.json",
        ],
    )


def _configure_stopped(
    action,
    config,
    payload,
    handler,
    updater,
    inspect,
    apply,
    cfg,
    path,
    label,
    *,
    manager=None,
    sidecars=(),
):
    def state(execution, reason, **kwargs):
        if label != "Tautulli":
            reason = (
                reason.replace("Tautulli", label)
                .replace("Plex connection", "service bindings")
                .replace("empty Plex connection", "service bindings")
                .replace("empty service bindings", "service bindings")
            )
        return _state(execution, reason, **kwargs)

    before = inspect(action, config, payload)
    if before is None or before.get("execution") != "planned":
        return before
    values = (payload.get("arrangement") or {}).get("values") or {}
    if values.get(f"{action['source']}::restart_after_config") is not True:
        return state(
            "waiting_input",
            "Authorize the Tautulli-only restart before applying its empty Plex connection.",
        )
    name = cfg.get("process_name")
    if handler is None or not name or _tracked(handler, name) is None:
        return state(
            "waiting_input",
            "Tautulli is not tracked by the controller. Finish its first setup and start it through DUMB before applying this connection.",
        )
    try:
        _snapshot(path)
    except (OSError, ValueError, UnicodeError):
        return state(
            "waiting_input",
            "Tautulli needs a regular, readable initial configuration before it can be connected.",
        )
    if not _stop_verified(handler, name):
        return state(
            "failed",
            "Tautulli did not fully stop. Its configuration was preserved; inspect the controller before retrying.",
        )
    backup = Path(str(path) + ".dumb-setup-recovery")
    manifest = Path(str(backup) + "-state.json")
    backup_created = False
    manifest_created = False
    snapshots = []
    env_present = "env" in cfg
    original_env = copy.deepcopy(cfg.get("env"))
    try:
        # Tautulli saves its own settings on shutdown. The compensation baseline
        # must include those changes, rather than a stale pre-shutdown snapshot.
        original = _snapshot(path)
        descriptor = os.open(
            backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        backup_created = True
        with open(descriptor, "wb", closefd=True) as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        if sidecars or manager is not None:
            snapshots = _capture_sidecars(sidecars)
            descriptor = os.open(
                manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            manifest_created = True
            recovery = {"sidecars": snapshots}
            if manager is not None:
                recovery.update(env_present=env_present, env=original_env)
            with open(descriptor, "w", closefd=True) as handle:
                json.dump(recovery, handle)
                handle.flush()
                os.fsync(handle.fileno())
    except (OSError, ValueError, UnicodeError):
        if backup_created:
            _discard_backup(backup)
        if manifest_created:
            _discard_backup(manifest)
        try:
            process, error = updater.auto_update(name, enable_update=False)
        except Exception:
            process, error = None, True
        return state(
            "waiting_input" if process and not error else "failed",
            "Tautulli's stopped configuration could not be safely backed up, or an earlier .dumb-setup-recovery needs review. No connection changes were applied; check the Tautulli service before retrying.",
        )
    stopped_payload = copy.deepcopy(payload)
    stopped_payload["_service_stopped"] = True
    restart_attempted = False
    try:
        result = apply(action, config, stopped_payload)
        if result and result.get("execution") == "failed":
            raise RuntimeError("Connection apply failed")
        restart_attempted = True
        process, error = updater.auto_update(name, enable_update=False)
        if not process or error:
            raise RuntimeError("Restart failed")
        after = inspect(action, config, payload)
        _discard_backup(backup)
        if manifest_created:
            _discard_backup(manifest)
        if after and after.get("verification") == "passed":
            return {**after, "execution": "applied"}
        if after and after.get("execution") in {"waiting_input", "unsupported"}:
            return after
        return state(
            "waiting_dependency",
            "Tautulli was restarted with the saved Plex connection; waiting for authenticated identity readback.",
            retryable=True,
        )
    except Exception:
        # Never replace application-owned configuration while a partial restart
        # may still be live. Compensation uses the same verified stop contract.
        running = _tracked(handler, name)
        if running is None and restart_attempted:
            # A launcher that failed before tracking its child cannot establish
            # that the application is stopped. Preserve recovery evidence, but
            # do not overwrite its live configuration based on that assumption.
            return state(
                "failed",
                "The Tautulli restart failed before child tracking was established. Review the service and its private .dumb-setup-recovery configuration before retrying; the potentially live configuration was preserved.",
            )
        if running is not None and not _stop_verified(handler, name):
            return state(
                "failed",
                "Tautulli could not be stopped for recovery. Its current configuration was preserved; review the Tautulli service before retrying.",
            )
        try:
            _restore_sidecars(snapshots)
            if manager is not None and (
                cfg.get("env") != original_env or ("env" in cfg) != env_present
            ):
                if env_present:
                    cfg["env"] = original_env
                else:
                    cfg.pop("env", None)
                manager.save_config()
            atomic_write_private_text(path, original.decode("utf-8"))
            process, error = updater.auto_update(name, enable_update=False)
            if not process or error:
                raise RuntimeError("Recovery restart failed")
        except Exception:
            return state(
                "failed",
                "Tautulli configuration recovery or restart needs attention. Its private .dumb-setup-recovery was retained; review the Tautulli service before retrying.",
            )
        _discard_backup(backup)
        if manifest_created:
            _discard_backup(manifest)
        return state(
            "failed",
            "The connection could not be applied. Tautulli's previous configuration was restored and its configured target restarted.",
        )
