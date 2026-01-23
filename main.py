from utils.config_loader import CONFIG_MANAGER as config
from utils.global_logger import logger, websocket_manager
from utils import user_management
from api.api_service import start_fastapi_process
from api.connection_manager import ConnectionManager
from utils.metrics_history import MetricsHistoryWriter
from utils.processes import ProcessHandler
from utils.auto_update import Update
from utils.dependencies import initialize_dependencies
from utils.core_services import has_core_service
from utils.plex_dbrepair import start_plex_dbrepair_worker
from utils.ffprobe_monitor import start_ffprobe_monitor
from utils.setup import setup_project
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, as_completed
import subprocess, threading, time, tomllib, os, socket, errno, psutil, json, urllib.parse


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
    while port in used_ports or not _is_port_available(port):
        port += 1
    return port


def _check_bind(family: int, addr: str, port: int) -> bool | None:
    sock = None
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            try:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            except OSError:
                pass
        sock.bind((addr, port))
        return True
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES, errno.EPERM):
            return False
        if exc.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, errno.EINVAL):
            return None
        return False
    finally:
        if sock is not None:
            sock.close()


def _is_port_available(port: int) -> bool:
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr:
                if conn.laddr.port == port:
                    return False
    except Exception:
        pass
    checks = [
        (socket.AF_INET, "0.0.0.0"),
        (socket.AF_INET6, "::"),
    ]
    for family, addr in checks:
        result = _check_bind(family, addr, port)
        if result is False:
            return False
    return True


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
    if not _is_port_available(desired):
        new_port = _find_free_port(desired + 1, set(used_ports.keys()))
        logger.info(
            "Port %s already in use by another process; assigning %s for %s.",
            desired,
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
                    success, error = updater.auto_update(process_name, auto)
                    if not success and error:
                        logger.error(
                            "Startup for %s failed (instance %s): %s",
                            process_name,
                            name,
                            error,
                        )
                    any_enabled = True
            if not any_enabled:
                logger.debug(f"No enabled instances found in {key_name}. Skipping.")
        elif config_obj.get("enabled"):
            process_name = config_obj.get("process_name", key_name)
            auto = config_obj.get("auto_update", False)
            success, error = updater.auto_update(process_name, auto)
            if not success and error:
                logger.error("Startup for %s failed: %s", process_name, error)
        else:
            logger.debug(f"{key_name} is disabled. Skipping process start.")
    except Exception as e:
        logger.error(f"An error occurred in setup for {key_name}: {e}")
        if exit_on_error:
            raise


def _service_has_enabled_instance(config_obj: dict) -> bool:
    if not isinstance(config_obj, dict):
        return False
    if "instances" in config_obj and isinstance(config_obj["instances"], dict):
        return any(
            isinstance(inst, dict) and inst.get("enabled")
            for inst in config_obj["instances"].values()
        )
    return bool(config_obj.get("enabled"))


def _service_has_huntarr_instance(config_obj: dict) -> bool:
    if not isinstance(config_obj, dict):
        return False
    if "instances" not in config_obj or not isinstance(config_obj["instances"], dict):
        return False
    return any(
        isinstance(inst, dict) and inst.get("enabled") and inst.get("use_huntarr")
        for inst in config_obj["instances"].values()
    )


def _enable_huntarr_if_needed(config_manager) -> None:
    if not any(
        _service_has_huntarr_instance(config_manager.get(svc, {}))
        for svc in ("sonarr", "radarr", "lidarr", "whisparr")
    ):
        return

    huntarr_cfg = config_manager.get("huntarr", {})
    if not isinstance(huntarr_cfg, dict):
        return
    instances = huntarr_cfg.get("instances", {}) or {}
    if not isinstance(instances, dict) or not instances:
        return

    if any(
        isinstance(inst, dict) and inst.get("enabled") for inst in instances.values()
    ):
        return

    first = next(iter(instances.values()))
    if isinstance(first, dict):
        first["enabled"] = True
        config_manager.save_config()


def _read_decypharr_mount_path(decypharr_cfg: dict) -> str | None:
    if not decypharr_cfg.get("use_embedded_rclone"):
        return None
    config_file = decypharr_cfg.get("config_file")
    if config_file and os.path.exists(config_file):
        try:
            with open(config_file, "r") as handle:
                data = json.load(handle)
            mount_path = (data.get("rclone") or {}).get("mount_path")
            if isinstance(mount_path, str) and mount_path.strip():
                return mount_path
        except Exception as e:
            logger.debug("Failed to read Decypharr mount path: %s", e)
    return "/mnt/debrid/decypharr"


def _extract_decypharr_debrid_mount(
    folder_path: str, mount_base: str | None
) -> str | None:
    if not folder_path:
        return None
    norm_path = os.path.normpath(folder_path)
    candidates = []
    if mount_base:
        candidates.append(os.path.normpath(mount_base))
    candidates.append("/mnt/debrid/decypharr")
    for base in candidates:
        if norm_path.startswith(base + os.sep):
            rel = norm_path[len(base) + 1 :]
            parts = [p for p in rel.split(os.sep) if p]
            if parts:
                return os.path.join(base, parts[0])
    return None


def _collect_decypharr_mount_paths(decypharr_cfg: dict) -> list[str]:
    if not decypharr_cfg.get("use_embedded_rclone"):
        return []
    config_file = decypharr_cfg.get("config_file")
    if not config_file or not os.path.exists(config_file):
        return []
    try:
        with open(config_file, "r") as handle:
            data = json.load(handle)
    except Exception as e:
        logger.debug("Failed to read Decypharr config: %s", e)
        return []

    mount_base = (data.get("rclone") or {}).get("mount_path")
    if not isinstance(mount_base, str):
        mount_base = None
    mounts = set()
    for debrid in data.get("debrids") or []:
        if not isinstance(debrid, dict):
            continue
        folder = debrid.get("folder")
        mount_path = _extract_decypharr_debrid_mount(folder, mount_base)
        if mount_path:
            mounts.add(mount_path)
        elif mount_base and debrid.get("name"):
            mounts.add(os.path.join(mount_base, str(debrid["name"])))
    return sorted(mounts)


def _collect_mount_paths(config_manager) -> list[str]:
    mount_paths = set()
    rclone_instances = config_manager.get("rclone", {}).get("instances", {}) or {}
    for instance in rclone_instances.values():
        if not isinstance(instance, dict) or not instance.get("enabled"):
            continue
        mount_dir = instance.get("mount_dir")
        mount_name = instance.get("mount_name")
        if mount_dir and mount_name:
            mount_paths.add(os.path.join(mount_dir, mount_name))

    decypharr_cfg = config_manager.get("decypharr", {}) or {}
    if decypharr_cfg.get("enabled") and decypharr_cfg.get("use_embedded_rclone"):
        decypharr_mounts = _collect_decypharr_mount_paths(decypharr_cfg)
        if decypharr_mounts:
            mount_paths.update(decypharr_mounts)
        else:
            mount_path = _read_decypharr_mount_path(decypharr_cfg)
            if mount_path:
                mount_paths.add(mount_path)

    return sorted(mount_paths)


def _merge_wait_for_mounts(config_obj: dict, mount_paths: list[str]) -> None:
    existing = config_obj.get("wait_for_mounts") or []
    merged = sorted(set(existing) | set(mount_paths))
    if merged:
        config_obj["wait_for_mounts"] = merged


def _set_wait_for_urls(config_obj: dict, wait_entries: list[dict]) -> None:
    existing = []
    seen = set()
    for entry in wait_entries:
        url = entry.get("url")
        if url and url not in seen:
            existing.append(entry)
            seen.add(url)
    if existing:
        config_obj["wait_for_url"] = existing
    else:
        config_obj.pop("wait_for_url", None)


def _apply_mount_waits(config_manager, mount_paths: list[str]) -> None:
    if not mount_paths:
        return
    mount_wait_keys = {
        "plex",
        "jellyfin",
        "emby",
    }
    for key in mount_wait_keys:
        cfg = config_manager.get(key, {})
        if not isinstance(cfg, dict):
            continue
        if "instances" in cfg and isinstance(cfg["instances"], dict):
            for inst in cfg["instances"].values():
                if isinstance(inst, dict) and inst.get("enabled"):
                    _merge_wait_for_mounts(inst, mount_paths)
        elif cfg.get("enabled"):
            _merge_wait_for_mounts(cfg, mount_paths)


def _collect_arr_ping_waits(config_manager) -> list[dict]:
    wait_entries = []
    for service in ("sonarr", "radarr", "lidarr", "whisparr"):
        instances = config_manager.get(service, {}).get("instances", {}) or {}
        for instance in instances.values():
            if not isinstance(instance, dict) or not instance.get("enabled"):
                continue
            port = instance.get("port")
            if not port:
                continue
            host = instance.get("host") or "127.0.0.1"
            base_url = instance.get("base_url") or instance.get("url_base") or ""
            if isinstance(base_url, str):
                base_url = base_url.strip()
                if base_url in ("", "/"):
                    base_url = ""
                else:
                    base_url = "/" + base_url.lstrip("/")
            else:
                base_url = ""
            wait_entries.append({"url": f"http://{host}:{port}{base_url}/ping"})
    return wait_entries


def _apply_prowlarr_waits(config_manager, wait_entries: list[dict]) -> None:
    if not wait_entries:
        return
    cfg = config_manager.get("prowlarr", {})
    if not isinstance(cfg, dict):
        return
    wait_urls = [entry.get("url") for entry in wait_entries if entry.get("url")]
    has_enabled = _service_has_enabled_instance(cfg)
    if wait_urls and has_enabled:
        logger.info(
            "Prowlarr will wait for Arr services to be ready: %s",
            ", ".join(wait_urls),
        )
    if not has_enabled:
        return

    if "instances" in cfg and isinstance(cfg["instances"], dict):
        for inst in cfg["instances"].values():
            if isinstance(inst, dict) and inst.get("enabled"):
                _set_wait_for_urls(inst, wait_entries)
    elif cfg.get("enabled"):
        _set_wait_for_urls(cfg, wait_entries)


def _build_plex_wait_entries(config_manager) -> list[dict]:
    plex_cfg = config_manager.get("plex", {}) or {}
    if not _service_has_enabled_instance(plex_cfg):
        return []
    plex_address = (config_manager.get("dumb", {}) or {}).get("plex_address") or ""
    plex_port = plex_cfg.get("port", 32400)
    base_url = ""
    if plex_address:
        parsed = urllib.parse.urlparse(plex_address)
        if parsed.scheme and parsed.hostname:
            host = parsed.hostname
            if parsed.port:
                base_url = f"{parsed.scheme}://{host}:{parsed.port}"
            else:
                base_url = f"{parsed.scheme}://{host}"
    if not base_url:
        base_url = f"http://127.0.0.1:{plex_port}"
    return [{"url": f"{base_url}/identity"}]


def _build_media_wait_entries(config_manager) -> list[dict]:
    wait_entries = []
    plex_entries = _build_plex_wait_entries(config_manager)
    wait_entries.extend(plex_entries)
    for key in ("jellyfin", "emby"):
        cfg = config_manager.get(key, {}) or {}
        if not _service_has_enabled_instance(cfg):
            continue
        port = cfg.get("port")
        if not port:
            continue
        wait_entries.append({"url": f"http://127.0.0.1:{port}/System/Info/Public"})
    return wait_entries


def _apply_waits_to_service(config_manager, key: str, wait_entries: list[dict]) -> None:
    if not wait_entries:
        return
    cfg = config_manager.get(key, {})
    if not isinstance(cfg, dict):
        return
    if "instances" in cfg and isinstance(cfg["instances"], dict):
        for inst in cfg["instances"].values():
            if isinstance(inst, dict) and inst.get("enabled"):
                _set_wait_for_urls(inst, wait_entries)
    elif cfg.get("enabled"):
        _set_wait_for_urls(cfg, wait_entries)


def _collect_preinstall_targets(config_manager) -> list[tuple[str, str]]:
    targets = []
    for key, cfg in config_manager.config.items():
        if not isinstance(cfg, dict):
            continue
        if key == "dumb":
            continue
        if "instances" in cfg and isinstance(cfg["instances"], dict):
            for inst_cfg in cfg["instances"].values():
                if isinstance(inst_cfg, dict) and inst_cfg.get("enabled"):
                    process_name = inst_cfg.get("process_name")
                    if process_name:
                        targets.append((key, process_name))
                    break
            continue
        if cfg.get("enabled"):
            process_name = cfg.get("process_name")
            if process_name:
                targets.append((key, process_name))
    return targets


def _preinstall_enabled_services(process_handler, config_manager) -> None:
    targets = _collect_preinstall_targets(config_manager)
    if not targets:
        return
    logger.info("Pre-installing enabled services before startup.")
    max_workers = min(4, max(1, len(targets)))

    def _run_preinstall(key: str, name: str) -> None:
        if process_handler.shutting_down:
            return
        with process_handler.process_context(name):
            if key in {"pgadmin", "postgres"}:
                logger.info(
                    "Preinstall skip for %s; requires running dependency.", name
                )
                return
            logger.info("Preinstall start: %s", name)
            success, error = setup_project(process_handler, name, preinstall=True)
            if not success:
                raise RuntimeError(error)
            process_handler.preinstalled_processes.add(name)
            logger.info("Preinstall done: %s", name)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_run_preinstall, key, name): name for key, name in targets
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                future.result()
            except Exception as e:
                logger.error("Pre-install failed for %s: %s", name, e)
                process_handler.shutdown(exit_code=1)
                raise
    logger.info("Pre-install phase complete.")
    process_handler.preinstall_complete = True


def _build_dependency_map(config_manager) -> dict[str, set[str]]:
    deps = {
        "riven_backend": {"postgres"},
        "riven_frontend": {"riven_backend"},
        "zilean": {"postgres"},
        "pgadmin": {"postgres"},
    }

    if _service_has_enabled_instance(config_manager.get("plex", {})):
        deps["tautulli"] = {"plex"}
        deps.setdefault("seerr", set()).add("plex")
    if _service_has_enabled_instance(config_manager.get("jellyfin", {})):
        deps.setdefault("seerr", set()).add("jellyfin")
    if _service_has_enabled_instance(config_manager.get("emby", {})):
        deps.setdefault("seerr", set()).add("emby")

    prowlarr_deps = set()
    if _service_has_enabled_instance(config_manager.get("sonarr", {})):
        prowlarr_deps.add("sonarr")
    if _service_has_enabled_instance(config_manager.get("radarr", {})):
        prowlarr_deps.add("radarr")
    if _service_has_enabled_instance(config_manager.get("lidarr", {})):
        prowlarr_deps.add("lidarr")
    if _service_has_enabled_instance(config_manager.get("whisparr", {})):
        prowlarr_deps.add("whisparr")
    if prowlarr_deps:
        deps["prowlarr"] = prowlarr_deps

    huntarr_deps = set()
    if _service_has_huntarr_instance(config_manager.get("sonarr", {})):
        huntarr_deps.add("sonarr")
    if _service_has_huntarr_instance(config_manager.get("radarr", {})):
        huntarr_deps.add("radarr")
    if _service_has_huntarr_instance(config_manager.get("lidarr", {})):
        huntarr_deps.add("lidarr")
    if _service_has_huntarr_instance(config_manager.get("whisparr", {})):
        huntarr_deps.add("whisparr")
    if huntarr_deps:
        deps["huntarr"] = huntarr_deps

    rclone_deps = set()
    rclone_instances = config_manager.get("rclone", {}).get("instances", {}) or {}
    for instance in rclone_instances.values():
        if not isinstance(instance, dict) or not instance.get("enabled"):
            continue
        if instance.get("zurg_enabled"):
            rclone_deps.add("zurg")
        if instance.get("decypharr_enabled"):
            rclone_deps.add("decypharr")
        key_type = (instance.get("key_type") or "").lower()
        if key_type == "nzbdav" or has_core_service(instance, "nzbdav"):
            rclone_deps.add("nzbdav")
    if rclone_deps:
        deps["rclone"] = rclone_deps

    return deps


def _start_processes_with_dependencies(
    process_handler, updater, config_manager, keys: list[str], dependency_map
) -> None:
    enabled = {
        key: _service_has_enabled_instance(config_manager.get(key, {})) for key in keys
    }
    pending = {key for key in keys if enabled.get(key)}
    deps = {
        key: {d for d in dependency_map.get(key, set()) if enabled.get(d)}
        for key in pending
    }

    def _start_key(key: str) -> None:
        cfg = config_manager.get(key, {})
        start_configured_process(cfg, updater, key)

    in_progress = {}
    completed = set()
    with ThreadPoolExecutor() as executor:
        while pending or in_progress:
            if process_handler.shutting_down:
                for future in list(in_progress):
                    future.cancel()
                return
            ready = [key for key in list(pending) if deps.get(key, set()) <= completed]
            for key in ready:
                if process_handler.shutting_down:
                    break
                pending.remove(key)
                in_progress[executor.submit(_start_key, key)] = key

            if not in_progress:
                raise RuntimeError(
                    f"Dependency resolution stalled. Remaining: {sorted(pending)}"
                )

            done, _ = wait(in_progress.keys(), return_when=FIRST_COMPLETED)
            for future in done:
                key = in_progress.pop(future)
                try:
                    future.result()
                except Exception as e:
                    logger.error("Failed while starting %s: %s", key, e)
                    process_handler.shutdown(exit_code=1)
                completed.add(key)


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
    mount_paths = _collect_mount_paths(config)
    _apply_mount_waits(config, mount_paths)
    _apply_prowlarr_waits(config, _collect_arr_ping_waits(config))
    _apply_waits_to_service(config, "tautulli", _build_plex_wait_entries(config))
    _apply_waits_to_service(config, "seerr", _build_media_wait_entries(config))
    _preinstall_enabled_services(process_handler, config)

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
        if config.get("traefik", {}).get("enabled"):
            start_configured_process(config.get("traefik", {}), updater, "traefik")

        grouped_keys = [
            "zurg",
            "radarr",
            "sonarr",
            "lidarr",
            "whisparr",
            "prowlarr",
            "huntarr",
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
        _enable_huntarr_if_needed(config)
        dependency_map = _build_dependency_map(config)
        _start_processes_with_dependencies(
            process_handler, updater, config, grouped_keys, dependency_map
        )

    except Exception as e:
        logger.error(e)
        process_handler.shutdown(exit_code=1)

    start_ffprobe_monitor(process_handler, logger)

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
