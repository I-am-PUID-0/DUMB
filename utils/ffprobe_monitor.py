import os
import time
import subprocess
import threading
import psutil

from utils.config_loader import CONFIG_MANAGER


def _service_has_enabled_instance(config_obj: dict) -> bool:
    if not isinstance(config_obj, dict):
        return False
    if "instances" in config_obj and isinstance(config_obj["instances"], dict):
        return any(
            isinstance(inst, dict) and inst.get("enabled")
            for inst in config_obj["instances"].values()
        )
    return bool(config_obj.get("enabled"))


def _collect_enabled_process_names(config_root: dict) -> set[str]:
    names = set()
    for svc_key in ("sonarr", "radarr"):
        svc_cfg = config_root.get(svc_key, {}) if isinstance(config_root, dict) else {}
        if not _service_has_enabled_instance(svc_cfg):
            continue
        if "instances" in svc_cfg and isinstance(svc_cfg["instances"], dict):
            for inst in svc_cfg["instances"].values():
                if not isinstance(inst, dict) or not inst.get("enabled"):
                    continue
                name = inst.get("process_name") or svc_key.capitalize()
                names.add(name)
        else:
            name = svc_cfg.get("process_name") or svc_key.capitalize()
            names.add(name)
    return names


def _get_monitor_cfg(config_root: dict) -> dict:
    dumb_cfg = config_root.get("dumb", {}) if isinstance(config_root, dict) else {}
    return dumb_cfg.get("ffprobe_monitor", {}) if isinstance(dumb_cfg, dict) else {}


def _get_proc_state(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", "r") as stat_file:
            stat = stat_file.read()
    except FileNotFoundError:
        return None
    except Exception:
        return None

    parts = stat.split(") ", 1)
    if len(parts) != 2:
        return None
    remainder = parts[1].split(" ", 1)
    if not remainder:
        return None
    return remainder[0]


def _extract_input_path(cmdline: list[str] | None) -> str | None:
    if not cmdline:
        return None
    for idx, arg in enumerate(cmdline):
        if arg == "-i" and idx + 1 < len(cmdline):
            return cmdline[idx + 1]
    for arg in reversed(cmdline):
        if arg and not arg.startswith("-"):
            return arg
    return None


def _collect_descendant_pids(root_pids: set[int]) -> set[int]:
    descendants = set()
    for pid in root_pids:
        try:
            proc = psutil.Process(pid)
            descendants.add(pid)
            for child in proc.children(recursive=True):
                descendants.add(child.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return descendants


def _is_target_process_name(internal_name: str, targets: set[str]) -> bool:
    if internal_name in targets:
        return True
    for target in targets:
        if internal_name.endswith(f":{target}"):
            return True
    return False


def _poke_ffprobe(ffprobe_cmd: list[str], filepath: str, timeout_sec: float, logger):
    cmd = (
        ffprobe_cmd
        + [
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            filepath,
        ]
    )
    try:
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_sec,
            check=False,
        )
        logger.info("ffprobe monitor poked stuck probe for %s", filepath)
        return True
    except subprocess.TimeoutExpired:
        logger.warning("ffprobe monitor timed out while poking %s", filepath)
    except Exception as exc:
        logger.warning("ffprobe monitor failed to poke %s: %s", filepath, exc)
    return False


def ffprobe_monitor_worker(process_handler, logger):
    last_poked = {}
    last_state = None
    while not process_handler.shutting_down:
        config_root = CONFIG_MANAGER.config if hasattr(CONFIG_MANAGER, "config") else {}
        monitor_cfg = _get_monitor_cfg(config_root)
        enabled = monitor_cfg.get("enabled", True)
        interval = monitor_cfg.get("interval_sec", 10)
        min_age = monitor_cfg.get("min_process_age_sec", 30)
        min_poke = monitor_cfg.get("min_poke_interval_sec", 60)
        poke_timeout = monitor_cfg.get("poke_timeout_sec", 30)
        max_pokes = monitor_cfg.get("max_pokes_per_cycle", 3)
        cache_ttl = monitor_cfg.get("cache_ttl_sec", 3600)
        ffprobe_path = monitor_cfg.get("ffprobe_path", "ffprobe")

        if not enabled:
            if last_state != "disabled":
                logger.info("ffprobe monitor disabled by configuration.")
                last_state = "disabled"
            time.sleep(max(1, interval))
            continue

        enabled_names = _collect_enabled_process_names(config_root)
        if not enabled_names:
            if last_state != "no_targets":
                logger.info("ffprobe monitor idle; no enabled Sonarr/Radarr instances.")
                last_state = "no_targets"
            time.sleep(max(1, interval))
            continue

        if last_state != "monitoring":
            logger.info(
                "ffprobe monitor active for: %s", ", ".join(sorted(enabled_names))
            )
            last_state = "monitoring"

        root_pids = set()
        for internal_name, proc in list(process_handler.process_names.items()):
            if not _is_target_process_name(internal_name, enabled_names):
                continue
            if proc and proc.poll() is None:
                root_pids.add(proc.pid)

        if not root_pids:
            time.sleep(max(1, interval))
            continue

        now = time.time()
        cutoff = now - max(cache_ttl, min_poke)
        last_poked = {path: ts for path, ts in last_poked.items() if ts >= cutoff}

        descendants = _collect_descendant_pids(root_pids)
        poked = 0

        if isinstance(ffprobe_path, list):
            ffprobe_cmd = ffprobe_path[:]
        else:
            ffprobe_cmd = [ffprobe_path]

        for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            if poked >= max_pokes:
                break
            pid = proc.info.get("pid")
            if pid not in descendants:
                continue
            name = proc.info.get("name") or ""
            cmdline = proc.info.get("cmdline") or [""]
            cmd_base = os.path.basename(cmdline[0]) if cmdline else ""
            if name != "ffprobe" and cmd_base != "ffprobe":
                continue
            if _get_proc_state(pid) != "D":
                continue
            create_time = proc.info.get("create_time") or 0
            if now - create_time < min_age:
                continue
            filepath = _extract_input_path(cmdline)
            if not filepath:
                continue
            last = last_poked.get(filepath)
            if last and now - last < min_poke:
                continue
            if _poke_ffprobe(ffprobe_cmd, filepath, poke_timeout, logger):
                last_poked[filepath] = now
                poked += 1

        time.sleep(max(1, interval))


def start_ffprobe_monitor(process_handler, logger):
    thread = threading.Thread(
        target=ffprobe_monitor_worker,
        args=(process_handler, logger),
        daemon=True,
        name="ffprobe-monitor",
    )
    thread.start()
    logger.info("ffprobe monitor thread started.")
    return thread
