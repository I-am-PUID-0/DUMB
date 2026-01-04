import os
import shlex
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from plexapi.exceptions import BadRequest, NotFound, Unauthorized
from plexapi.server import PlexServer

from utils.config_loader import CONFIG_MANAGER
from utils.global_logger import logger
from utils.processes import ProcessHandler
from utils.user_management import chown_recursive


def _plex_url(plex_cfg, dumb_cfg):
    address = dumb_cfg.get("plex_address") or ""
    port = plex_cfg.get("port", 32400)
    if address:
        if "://" not in address:
            address = f"http://{address}"
        parsed = urlparse(address)
        scheme = parsed.scheme or "http"
        host = parsed.hostname or parsed.path or "127.0.0.1"
        plex_port = parsed.port or port
        return f"{scheme}://{host}:{plex_port}"
    return f"http://127.0.0.1:{port}"


def _plex_token(plex_cfg, dumb_cfg):
    token = dumb_cfg.get("plex_token") or ""
    if token:
        return token
    preferences_path = plex_cfg.get(
        "config_file", "/plex/Plex Media Server/Preferences.xml"
    )
    if not os.path.exists(preferences_path):
        return ""
    try:
        tree = ET.parse(preferences_path)
        root = tree.getroot()
        return root.attrib.get("PlexOnlineToken", "") or ""
    except Exception as exc:
        logger.warning(
            "Failed to read Plex token from %s: %s", preferences_path, exc
        )
        return ""


def _container_has_items(root):
    if root is None:
        return False
    size = root.attrib.get("size")
    if size is not None:
        try:
            return int(size) > 0
        except ValueError:
            pass
    return len(list(root)) > 0


def _has_active_sessions(plex):
    try:
        return len(plex.sessions()) > 0
    except Exception as exc:
        logger.warning("Failed to query Plex sessions: %s", exc)
        return None


def _has_scheduled_recordings(plex):
    try:
        root = plex.query("/livetv/dvrs")
    except NotFound:
        return False
    except (Unauthorized, BadRequest) as exc:
        logger.warning("Failed to query Plex DVRs: %s", exc)
        return None
    except Exception as exc:
        logger.warning("Failed to query Plex DVRs: %s", exc)
        return None

    if not _container_has_items(root):
        return False

    dvrs = root.findall(".//Dvr")
    if not dvrs:
        return False

    for dvr in dvrs:
        key = dvr.attrib.get("key")
        dvr_id = dvr.attrib.get("id")
        schedule_path = None
        if key:
            schedule_path = f"{key}/schedule"
        elif dvr_id:
            schedule_path = f"/livetv/dvrs/{dvr_id}/schedule"
        if not schedule_path:
            continue
        try:
            schedule_root = plex.query(schedule_path)
        except NotFound:
            continue
        except (Unauthorized, BadRequest) as exc:
            logger.warning("Failed to query Plex DVR schedule: %s", exc)
            return None
        except Exception as exc:
            logger.warning("Failed to query Plex DVR schedule: %s", exc)
            return None
        if _container_has_items(schedule_root):
            return True
    return False


def _wait_for_idle(plex, db_cfg):
    wait_minutes = float(db_cfg.get("idle_check_minutes", 5))
    backoff_multiplier = float(db_cfg.get("backoff_multiplier", 2.0))
    max_backoff_minutes = float(db_cfg.get("max_backoff_minutes", 60))
    max_wait_minutes = float(db_cfg.get("max_wait_minutes", 0))
    total_wait = 0.0
    current_wait = max(wait_minutes, 0.1)

    while True:
        sessions_active = _has_active_sessions(plex)
        scheduled_active = _has_scheduled_recordings(plex)
        if sessions_active is None or scheduled_active is None:
            logger.warning(
                "Unable to verify Plex idle state; skipping this DBRepair run."
            )
            return False
        if not sessions_active and not scheduled_active:
            return True

        if max_wait_minutes and total_wait >= max_wait_minutes:
            logger.info(
                "Plex still in use after %.1f minutes; skipping DBRepair run.",
                total_wait,
            )
            return False

        logger.info(
            "Plex in use (sessions=%s scheduled=%s). Waiting %.1f minutes before recheck.",
            sessions_active,
            scheduled_active,
            current_wait,
        )
        time.sleep(current_wait * 60)
        total_wait += current_wait
        current_wait = min(current_wait * backoff_multiplier, max_backoff_minutes)


def _format_command(command, plex_cfg, db_cfg):
    db_path = db_cfg.get(
        "db_path",
        "/plex/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db",
    )
    install_dir = db_cfg.get("install_dir", "/data/dbrepair")
    executable = db_cfg.get("executable", "DBRepair.sh")
    sqlite_path = db_cfg.get("sqlite_path", "/usr/lib/plexmediaserver/Plex SQLite")
    databases_path = db_cfg.get(
        "databases_path",
        "/plex/Plex Media Server/Plug-in Support/Databases",
    )
    replacements = {
        "plex_config_dir": plex_cfg.get("config_dir", "/plex"),
        "plex_db_path": db_path,
        "dbrepair_install_dir": install_dir,
        "dbrepair_executable": executable,
        "dbrepair_sqlite_path": sqlite_path,
        "dbrepair_databases_path": databases_path,
    }

    if isinstance(command, str):
        command = shlex.split(command)
    formatted = []
    for part in command:
        try:
            formatted.append(part.format(**replacements))
        except Exception:
            formatted.append(part)
    return formatted, db_path


def _default_dbrepair_command(db_cfg, plex_cfg):
    install_dir = db_cfg.get("install_dir", "/data/dbrepair")
    executable = db_cfg.get("executable", "DBRepair.sh")
    sqlite_path = db_cfg.get("sqlite_path", "/usr/lib/plexmediaserver/Plex SQLite")
    databases_path = db_cfg.get(
        "databases_path",
        "/plex/Plex Media Server/Plug-in Support/Databases",
    )
    command = [os.path.join(install_dir, executable)]
    if sqlite_path and databases_path:
        command.extend(["--sqlite", sqlite_path, "--databases", databases_path])
    script_args = db_cfg.get("script_args") or []
    filtered = []
    for arg in script_args:
        if str(arg).lower() in {"start", "stop"}:
            logger.warning("DBRepair script arg '%s' removed; Plex is managed by DUMB.", arg)
            continue
        filtered.append(arg)
    command.extend(filtered)
    return command


def _apply_required_args(command, db_cfg):
    required_args = db_cfg.get("required_args", ["--backup", "--verify"]) or []
    existing = set(command)
    for arg in required_args:
        if arg not in existing:
            command.append(arg)
    return command


def _ensure_dbrepair_installed(db_cfg):
    install_dir = db_cfg.get("install_dir", "/data/dbrepair")
    executable = db_cfg.get("executable", "DBRepair.sh")
    executable_path = os.path.join(install_dir, executable)
    if os.path.exists(executable_path):
        try:
            os.chmod(executable_path, 0o755)
        except Exception as exc:
            logger.warning(
                "Failed to set DBRepair executable permissions for %s: %s",
                executable_path,
                exc,
            )
        return True

    logger.warning(
        "DBRepair executable not found at %s. Install it manually and retry.",
        executable_path,
    )
    return False


def _run_dbrepair(process_handler, plex_cfg, db_cfg):
    command = db_cfg.get("command") or []
    if not command:
        command = _default_dbrepair_command(db_cfg, plex_cfg)
        logger.info("DBRepair command not configured; using default command.")
    command = _apply_required_args(command, db_cfg)

    command, db_path = _format_command(command, plex_cfg, db_cfg)
    if db_path and not os.path.exists(db_path):
        logger.warning("Plex DB path not found: %s", db_path)

    env = os.environ.copy()
    env.update(db_cfg.get("env", {}))
    work_dir = db_cfg.get("work_dir") or plex_cfg.get("config_dir") or "/"

    plex_process_name = plex_cfg.get("process_name", "Plex Media Server")
    plex_running = plex_process_name in process_handler.process_names

    if plex_running:
        process_handler.stop_process(plex_process_name)
        process_handler.wait(plex_process_name)

    log_file = db_cfg.get("log_file") or ""
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(log_file, "a", encoding="utf-8") as log_target:
                log_target.write(f"[{timestamp}] command: {' '.join(command)}\n")
        except Exception as exc:
            logger.warning("Failed to write DBRepair log header %s: %s", log_file, exc)

    process_name = db_cfg.get("process_name") or "Plex DBRepair"
    run_command = command
    if log_file:
        command_str = " ".join(shlex.quote(part) for part in command)
        run_command = [
            "/bin/sh",
            "-c",
            f"{command_str} 2>&1 | tee -a {shlex.quote(log_file)}",
        ]

    try:
        logger.info("Running DBRepair: %s", " ".join(command))
        process_handler.start_process(
            process_name,
            config_dir=work_dir,
            command=run_command,
            suppress_logging=False,
            env=env,
        )
        process_handler.wait(process_name)
        return_code = process_handler.returncode
        if log_file:
            try:
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                with open(log_file, "a", encoding="utf-8") as log_target:
                    log_target.write(f"[{timestamp}] exit_code: {return_code}\n")
            except Exception as exc:
                logger.warning("Failed to write DBRepair exit code: %s", exc)
        if return_code != 0:
            logger.error("DBRepair failed with exit code %s", return_code)
            return False
        logger.info("DBRepair completed successfully.")
        return True
    finally:
        if plex_running:
            process_handler.start_process(plex_process_name)


def plex_dbrepair_worker():
    process_handler = ProcessHandler(logger)
    next_run = time.monotonic()
    first_run = True

    while True:
        plex_cfg = CONFIG_MANAGER.get("plex", {}) or {}
        db_cfg = plex_cfg.get("dbrepair", {}) or {}

        if not plex_cfg.get("enabled") or not db_cfg.get("enabled"):
            time.sleep(60)
            next_run = time.monotonic() + 60
            first_run = True
            continue

        interval_minutes = float(db_cfg.get("interval_minutes", 1440))
        if first_run:
            if db_cfg.get("run_before_start"):
                next_run = time.monotonic() + interval_minutes * 60
            first_run = False
        now = time.monotonic()
        if now < next_run:
            time.sleep(min(30, next_run - now))
            continue

        if not _ensure_dbrepair_installed(db_cfg):
            next_run = time.monotonic() + interval_minutes * 60
            continue

        dumb_cfg = CONFIG_MANAGER.get("dumb", {}) or {}
        plex_url = _plex_url(plex_cfg, dumb_cfg)
        plex_token = _plex_token(plex_cfg, dumb_cfg)
        if not plex_token:
            logger.warning("Plex token not found; skipping DBRepair run.")
            next_run = time.monotonic() + interval_minutes * 60
            continue

        try:
            plex = PlexServer(plex_url, plex_token)
        except Exception as exc:
            logger.warning("Failed to connect to Plex at %s: %s", plex_url, exc)
            next_run = time.monotonic() + interval_minutes * 60
            continue

        if _wait_for_idle(plex, db_cfg):
            _run_dbrepair(process_handler, plex_cfg, db_cfg)

        next_run = time.monotonic() + interval_minutes * 60


def start_plex_dbrepair_worker():
    thread = threading.Thread(target=plex_dbrepair_worker, daemon=True)
    thread.start()
    return thread


def run_dbrepair_once(run_before_start=False):
    plex_cfg = CONFIG_MANAGER.get("plex", {}) or {}
    db_cfg = plex_cfg.get("dbrepair", {}) or {}
    if not plex_cfg.get("enabled") or not db_cfg.get("enabled"):
        return False

    if run_before_start and not db_cfg.get("run_before_start"):
        return False

    if not _ensure_dbrepair_installed(db_cfg):
        return False

    if run_before_start:
        logger.info("Running DBRepair before Plex startup; skipping idle checks.")
        process_handler = ProcessHandler(logger)
        return _run_dbrepair(process_handler, plex_cfg, db_cfg)

    dumb_cfg = CONFIG_MANAGER.get("dumb", {}) or {}
    plex_url = _plex_url(plex_cfg, dumb_cfg)
    plex_token = _plex_token(plex_cfg, dumb_cfg)
    if not plex_token:
        logger.warning("Plex token not found; skipping DBRepair run.")
        return False

    try:
        plex = PlexServer(plex_url, plex_token)
    except Exception as exc:
        logger.warning("Failed to connect to Plex at %s: %s", plex_url, exc)
        return False

    if _wait_for_idle(plex, db_cfg):
        process_handler = ProcessHandler(logger)
        return _run_dbrepair(process_handler, plex_cfg, db_cfg)
    return False
