from utils.config_loader import CONFIG_MANAGER as config
from utils.global_logger import logger, websocket_manager
from utils import user_management
from api.api_service import start_fastapi_process
from api.connection_manager import ConnectionManager
from utils.metrics_history import MetricsHistoryWriter
from utils.processes import ProcessHandler
from utils.auto_update import Update
from utils.dependencies import initialize_dependencies
from utils.plex_dbrepair import start_plex_dbrepair_worker
import subprocess, threading, time, tomllib, os


def log_ascii_art():
    with open("pyproject.toml", "rb") as file:
        pyproject = tomllib.load(file)
        version = pyproject["tool"]["poetry"]["version"]

    ascii_art = f"""
                                                                       
DDDDDDDDDDDDD       UUUUUUUU     UUUUUUUUMMMMMMMM               MMMMMMMMBBBBBBBBBBBBBBBBB   
D::::::::::::DDD    U::::::U     U::::::UM:::::::M             M:::::::MB::::::::::::::::B  
D:::::::::::::::DD  U::::::U     U::::::UM::::::::M           M::::::::MB::::::BBBBBB:::::B 
DDD:::::DDDDD:::::D UU:::::U     U:::::UUM:::::::::M         M:::::::::MBB:::::B     B:::::B
  D:::::D    D:::::D U:::::U     U:::::U M::::::::::M       M::::::::::M  B::::B     B:::::B
  D:::::D     D:::::DU:::::D     D:::::U M:::::::::::M     M:::::::::::M  B::::B     B:::::B
  D:::::D     D:::::DU:::::D     D:::::U M:::::::M::::M   M::::M:::::::M  B::::BBBBBB:::::B 
  D:::::D     D:::::DU:::::D     D:::::U M::::::M M::::M M::::M M::::::M  B:::::::::::::BB  
  D:::::D     D:::::DU:::::D     D:::::U M::::::M  M::::M::::M  M::::::M  B::::BBBBBB:::::B 
  D:::::D     D:::::DU:::::D     D:::::U M::::::M   M:::::::M   M::::::M  B::::B     B:::::B
  D:::::D     D:::::DU:::::D     D:::::U M::::::M    M:::::M    M::::::M  B::::B     B:::::B
  D:::::D    D:::::D U::::::U   U::::::U M::::::M     MMMMM     M::::::M  B::::B     B:::::B
DDD:::::DDDDD:::::D  U:::::::UUU:::::::U M::::::M               M::::::MBB:::::BBBBBB::::::B
D:::::::::::::::DD    UU:::::::::::::UU  M::::::M               M::::::MB:::::::::::::::::B 
D::::::::::::DDD        UU:::::::::UU    M::::::M               M::::::MB::::::::::::::::B  
DDDDDDDDDDDDD             UUUUUUUUU      MMMMMMMM               MMMMMMMMBBBBBBBBBBBBBBBBB   

                             Version: {version}                                    
"""
    logger.info(ascii_art + "\n")


def _find_free_port(start_port: int, used_ports: set[int]) -> int:
    port = start_port
    while port in used_ports:
        port += 1
    return port


def _reserve_port(
    used_ports: dict[int, str], desired: int, owner: str, label: str
) -> tuple[int | None, bool]:
    if not isinstance(desired, int) or desired <= 0:
        return None, False

    existing_owner = used_ports.get(desired)
    if existing_owner and existing_owner != owner:
        new_port = _find_free_port(desired + 1, set(used_ports.keys()))
        logger.info(
            "Port %s already in use by %s; assigning %s for %s.",
            desired,
            existing_owner,
            new_port,
            label,
        )
        used_ports[new_port] = owner
        return new_port, True

    used_ports[desired] = owner
    return desired, False


def _reserve_config_port(
    cfg: dict,
    field: str,
    used_ports: dict[int, str],
    owner: str,
    label: str,
) -> bool:
    desired = cfg.get(field)
    chosen, changed = _reserve_port(used_ports, desired, owner, label)
    if chosen is None:
        return False
    if cfg.get(field) != chosen:
        cfg[field] = chosen
        return True
    return changed


def _seed_used_ports(config_obj: dict, used_ports: dict[int, str]) -> None:
    if not isinstance(config_obj, dict):
        return

    def _add(port: int | None, owner: str) -> None:
        if not isinstance(port, int) or port <= 0:
            return
        if port in used_ports and used_ports[port] != owner:
            logger.warning(
                "Port %s already reserved by %s; %s may be auto-shifted.",
                port,
                used_ports[port],
                owner,
            )
            return
        used_ports[port] = owner

    for key, cfg in config_obj.items():
        if not isinstance(cfg, dict):
            continue

        if key == "dumb":
            for subkey in ("api_service", "frontend"):
                subcfg = cfg.get(subkey, {})
                if isinstance(subcfg, dict) and subcfg.get("enabled"):
                    _add(subcfg.get("port"), f"dumb_{subkey}:port")
            continue

        if "instances" in cfg and isinstance(cfg["instances"], dict):
            for inst_name, inst_cfg in cfg["instances"].items():
                if isinstance(inst_cfg, dict) and inst_cfg.get("enabled"):
                    _add(inst_cfg.get("port"), f"{key}:{inst_name}")
            continue

        if cfg.get("enabled"):
            if key == "nzbdav":
                _add(cfg.get("frontend_port"), "nzbdav:frontend_port")
                _add(cfg.get("backend_port"), "nzbdav:backend_port")
            _add(cfg.get("port"), f"{key}:port")


def _apply_global_port_reservations(config_manager) -> None:
    used_ports: dict[int, str] = {}
    _seed_used_ports(config_manager.config, used_ports)
    changed = False

    dumb_cfg = config_manager.get("dumb", {})
    for subkey in ("api_service", "frontend"):
        subcfg = dumb_cfg.get(subkey, {})
        if isinstance(subcfg, dict) and subcfg.get("enabled"):
            changed |= _reserve_config_port(
                subcfg,
                "port",
                used_ports,
                f"dumb_{subkey}:port",
                f"DUMB {subkey} port",
            )

    for key, cfg in config_manager.config.items():
        if key == "dumb" or not isinstance(cfg, dict):
            continue

        if "instances" in cfg and isinstance(cfg["instances"], dict):
            for inst_name, inst_cfg in cfg["instances"].items():
                if isinstance(inst_cfg, dict) and inst_cfg.get("enabled"):
                    changed |= _reserve_config_port(
                        inst_cfg,
                        "port",
                        used_ports,
                        f"{key}:{inst_name}",
                        f"{key} {inst_name} port",
                    )
            continue

        if cfg.get("enabled"):
            if key == "nzbdav":
                changed |= _reserve_config_port(
                    cfg,
                    "frontend_port",
                    used_ports,
                    "nzbdav:frontend_port",
                    "NzbDAV frontend port",
                )
                changed |= _reserve_config_port(
                    cfg,
                    "backend_port",
                    used_ports,
                    "nzbdav:backend_port",
                    "NzbDAV backend port",
                )
            changed |= _reserve_config_port(
                cfg,
                "port",
                used_ports,
                f"{key}:port",
                f"{key} port",
            )

    if changed:
        config_manager.save_config()


def start_configured_process(config_obj, updater, key_name, exit_on_error=True):
    try:
        if "instances" in config_obj:
            any_enabled = False
            for name, instance in config_obj["instances"].items():
                if instance.get("enabled"):
                    process_name = instance.get("process_name", name)
                    auto = instance.get("auto_update", False)
                    updater.auto_update(process_name, auto)
                    any_enabled = True
            if not any_enabled:
                logger.debug(f"No enabled instances found in {key_name}. Skipping.")
        elif config_obj.get("enabled"):
            process_name = config_obj.get("process_name", key_name)
            auto = config_obj.get("auto_update", False)
            updater.auto_update(process_name, auto)
        else:
            logger.debug(f"{key_name} is disabled. Skipping process start.")
    except Exception as e:
        logger.error(f"An error occurred in setup for {key_name}: {e}")
        if exit_on_error:
            raise


def main():
    log_ascii_art()

    process_handler = ProcessHandler(logger)
    updater = Update(process_handler)
    metrics_manager = ConnectionManager()
    status_manager = ConnectionManager()

    initialize_dependencies(
        process_handler=process_handler,
        updater=updater,
        websocket_manager=websocket_manager,
        metrics_manager=metrics_manager,
        status_manager=status_manager,
        logger=logger,
    )
    process_handler.start_auto_restart_monitor()

    try:
        user_management.create_system_user()
    except Exception as e:
        logger.error(f"An error occurred while creating system user: {e}")
        process_handler.shutdown(exit_code=1)

    _apply_global_port_reservations(config)

    if config.get("dumb", {}).get("api_service", {}).get("enabled"):
        start_fastapi_process()
        api_cfg = config.get("dumb", {}).get("api_service", {})
        api_name = api_cfg.get("process_name", "DUMB API")
        process_handler.register_external_process(api_name, os.getpid())

    try:
        dumb_config = config.get("dumb", {})
        start_configured_process(dumb_config.get("frontend", {}), updater, "frontend")
    except Exception:
        process_handler.shutdown(exit_code=1)

    def metrics_history_worker():
        def _get_metrics_cfg():
            cfg_root = config.config if hasattr(config, "config") else config
            return (cfg_root.get("dumb", {}) or {}).get("metrics", {})

        from utils.dependencies import get_metrics_collector

        collector = get_metrics_collector()
        writer = None
        last_writer_cfg = None
        while True:
            try:
                metrics_cfg = _get_metrics_cfg()
                enabled = metrics_cfg.get("history_enabled", True)
                interval = metrics_cfg.get("history_interval_sec", 5)
                retention_days = metrics_cfg.get("history_retention_days", 7)
                max_file_mb = metrics_cfg.get("history_max_file_mb", 50)
                max_total_mb = metrics_cfg.get("history_max_total_mb", 100)
                history_dir = metrics_cfg.get("history_dir", "/config/metrics")

                try:
                    interval = float(interval)
                except (TypeError, ValueError):
                    interval = 5.0
                interval = max(0.5, interval)

                writer_cfg = (history_dir, retention_days, max_file_mb, max_total_mb)
                if enabled:
                    if writer is None or writer_cfg != last_writer_cfg:
                        writer = MetricsHistoryWriter(
                            base_dir=history_dir,
                            retention_days=retention_days,
                            max_file_mb=max_file_mb,
                            max_total_mb=max_total_mb,
                            logger=logger,
                        )
                        last_writer_cfg = writer_cfg

                    snapshot = collector.snapshot()
                    writer.write(snapshot)
                else:
                    writer = None
                    last_writer_cfg = None
            except Exception as e:
                logger.error(f"Metrics history worker error: {e}")
            time.sleep(interval)

    try:
        grouped_keys = [
            "zurg",
            "prowlarr",
            "radarr",
            "sonarr",
            "lidarr",
            "whisparr",
            "decypharr",
            "nzbdav",
            "rclone",
            "postgres",
            "pgadmin",
            "zilean",
            "plex_debrid",
            "phalanx_db",
            "cli_debrid",
            "cli_battery",
            "riven_backend",
            "riven_frontend",
            "plex",
            "jellyfin",
            "emby",
            "tautulli",
            "seerr",
        ]
        for key in grouped_keys:
            cfg = config.get(key, {})
            start_configured_process(cfg, updater, key)

    except Exception as e:
        logger.error(e)
        process_handler.shutdown(exit_code=1)

    def healthcheck():
        time.sleep(60)
        while True:
            time.sleep(10)
            try:
                result = subprocess.run(
                    ["python", "healthcheck.py"], capture_output=True, text=True
                )
                if result.stderr:
                    logger.error(result.stderr.strip())
            except Exception as e:
                logger.error("Error running healthcheck.py: %s", e)
            time.sleep(50)

    thread = threading.Thread(target=healthcheck, daemon=True)
    thread.start()

    metrics_thread = threading.Thread(target=metrics_history_worker, daemon=True)
    metrics_thread.start()

    plex_cfg = config.get("plex", {}) or {}
    if plex_cfg.get("dbrepair", {}).get("enabled"):
        start_plex_dbrepair_worker()

    threading.Event().wait()


if __name__ == "__main__":
    main()
